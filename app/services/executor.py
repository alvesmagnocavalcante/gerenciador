import sys
import os
import time
import json
import signal
import hashlib
import logging
import shutil
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple
import httpx

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.security import sanitize_output, resolve_safe_path
from app.models.automation import Automation
from app.models.execution import Execution

logger = logging.getLogger("pyautomation.executor")

# Controle de processos em execução: {execution_id: subprocess.Popen}
_active_processes: Dict[int, subprocess.Popen] = {}
_running_automations: set[int] = set()
_cancelled_executions: set[int] = set()
_lock = threading.Lock()
_semaphore = threading.Semaphore(settings.MAX_CONCURRENT_EXECUTIONS)

def get_automation_dir(automation_id: int) -> Path:
    """Retorna o diretório base para arquivos e venv da automação."""
    path = settings.AUTOMATIONS_DIR / f"auto_{automation_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path

def get_venv_dir(automation_id: int) -> Path:
    """Retorna o diretório da virtualenv da automação."""
    return get_automation_dir(automation_id) / ".venv"

def get_venv_python(venv_dir: Path) -> Path:
    """Retorna o caminho do executável python dentro da venv dependendo do SO."""
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"

def safe_rmtree(path: Path | str) -> None:
    """Remove um diretório com segurança, lidando com permissões somente leitura do Windows (.git)."""
    p = Path(path)
    if not p.exists():
        return
    import stat
    def _handle_readonly(func, subpath, exc_info):
        try:
            os.chmod(subpath, stat.S_IWRITE)
            func(subpath)
        except Exception:
            pass
    try:
        shutil.rmtree(p, onerror=_handle_readonly)
    except Exception:
        shutil.rmtree(p, ignore_errors=True)

def delete_automation_files(automation_id: int) -> None:
    """Cancela execuções ativas da automação e remove seu diretório físico do disco."""
    with SessionLocal() as db:
        active_execs = db.query(Execution).filter(
            Execution.automation_id == automation_id,
            Execution.status.in_(["PENDING", "RUNNING"])
        ).all()
        for ex in active_execs:
            cancel_execution(ex.id)

    auto_path = settings.AUTOMATIONS_DIR / f"auto_{automation_id}"
    if auto_path.exists():
        safe_rmtree(auto_path)

def cleanup_orphaned_executions() -> int:
    """Marca execuções PENDING/RUNNING órfãs como FAILED ao iniciar o servidor/worker."""
    count = 0
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        orphans = db.query(Execution).filter(Execution.status.in_(["PENDING", "RUNNING"])).all()
        for ex in orphans:
            with _lock:
                if ex.id in _active_processes or ex.automation_id in _running_automations:
                    continue
            ex.status = "FAILED"
            ex.finished_at = now
            if ex.started_at:
                started = ex.started_at if ex.started_at.tzinfo else ex.started_at.replace(tzinfo=timezone.utc)
                ex.duration_seconds = round((now - started).total_seconds(), 2)
            ex.error_message = "Execução interrompida por reinicialização do servidor."
            if ex.logs:
                ex.logs += "\n[EXECUÇÃO INTERROMPIDA POR REINICIALIZAÇÃO DO SERVIDOR]"
            else:
                ex.logs = "[EXECUÇÃO INTERROMPIDA POR REINICIALIZAÇÃO DO SERVIDOR]"
            count += 1
        if count > 0:
            db.commit()
            logger.info(f"{count} execução(ões) órfã(s) marcada(s) como FAILED na inicialização.")
    return count

def parse_env_vars(text: Optional[str]) -> Dict[str, str]:
    """Interpreta variáveis de ambiente tanto no formato .env padrão (CHAVE=VALOR) quanto em JSON."""
    if not text or not text.strip():
        return {}

    text = text.strip()

    # Tentativa 1: JSON
    if text.startswith("{") and text.endswith("}"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass

    # Tentativa 2: Formato .env linha a linha
    env_dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" in line:
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            if key:
                env_dict[key] = val
    return env_dict

def find_dependency_files(working_dir: Path, auto_dir: Path) -> Dict[str, Path]:
    """Localiza requirements.txt e pyproject.toml na raiz ou no repositório da automação."""
    files = {}
    # 1. pyproject.toml
    for candidate in [working_dir / "pyproject.toml", auto_dir / "pyproject.toml"]:
        if candidate.is_file():
            files["pyproject"] = candidate
            break

    # 2. requirements.txt
    for candidate in [working_dir / "requirements.txt", auto_dir / "requirements.txt"]:
        if candidate.is_file():
            files["requirements"] = candidate
            break

    return files

def _compute_deps_hash(dep_files: Dict[str, Path]) -> str:
    """Calcula um hash SHA-256 dos arquivos de dependências para evitar reinstalações redundantes."""
    hasher = hashlib.sha256()
    for key in sorted(dep_files.keys()):
        path = dep_files[key]
        try:
            hasher.update(key.encode("utf-8"))
            hasher.update(path.read_bytes())
        except Exception:
            pass
    return hasher.hexdigest()

def ensure_venv(automation_id: int, working_dir: Path, force_reinstall: bool = False) -> Path:
    """Garante que o ambiente virtual (venv) da automação exista e tenha as dependências instaladas via uv."""
    venv_dir = get_venv_dir(automation_id)
    python_bin = get_venv_python(venv_dir)
    auto_dir = get_automation_dir(automation_id)

    if force_reinstall and venv_dir.exists():
        logger.info(f"Removendo venv existente da automação {automation_id} para reconstrução limpa...")
        safe_rmtree(venv_dir)

    venv_created = False
    if not python_bin.exists():
        logger.info(f"Criando venv para automação {automation_id} via uv...")
        cmd = ["uv", "venv", str(venv_dir)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"Erro ao criar virtualenv com uv: {proc.stderr}")
        venv_created = True

    # Localizar arquivos de dependências
    dep_files = find_dependency_files(working_dir, auto_dir)
    if not dep_files:
        return python_bin

    hash_file = venv_dir / ".deps_hash"
    current_hash = _compute_deps_hash(dep_files)

    if not force_reinstall and not venv_created and hash_file.is_file():
        try:
            if hash_file.read_text(encoding="utf-8").strip() == current_hash:
                return python_bin
        except Exception:
            pass

    all_ok = True

    # 1. Instalar a partir de pyproject.toml
    if "pyproject" in dep_files:
        pyproj_path = dep_files["pyproject"]
        logger.info(f"Instalando dependências de {pyproj_path} com uv...")
        cmd = ["uv", "pip", "install", "--python", str(python_bin), "-r", str(pyproj_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            all_ok = False
            logger.warning(f"Aviso ao instalar dependências de pyproject.toml: {proc.stderr}")

    # 2. Instalar a partir de requirements.txt
    if "requirements" in dep_files:
        req_path = dep_files["requirements"]
        logger.info(f"Instalando dependências de {req_path} com uv...")
        cmd = ["uv", "pip", "install", "--python", str(python_bin), "-r", str(req_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            all_ok = False
            logger.warning(f"Aviso ao instalar dependências de requirements.txt: {proc.stderr}")

    if all_ok:
        try:
            hash_file.write_text(current_hash, encoding="utf-8")
        except Exception:
            pass

    return python_bin

def rebuild_automation_venv(automation_id: int) -> Tuple[bool, str]:
    """Reconstrói a venv da automação do zero e reinstala todas as dependências."""
    auto_dir = get_automation_dir(automation_id)
    with SessionLocal() as db:
        automation = db.query(Automation).filter(Automation.id == automation_id).first()
        if not automation:
            return False, "Automação não encontrada."
        working_dir = auto_dir / "repo" if automation.source_type == "git" else auto_dir

    try:
        ensure_venv(automation_id, working_dir, force_reinstall=True)
        dep_files = find_dependency_files(working_dir, auto_dir)
        deps_found = list(dep_files.keys())
        detail = f"Dependências detectadas e instaladas: {', '.join(deps_found)}" if deps_found else "Nenhum arquivo requirements.txt ou pyproject.toml encontrado."
        return True, f"Ambiente virtual (.venv) reconstruído com sucesso! {detail}"
    except Exception as e:
        logger.error(f"Erro ao reconstruir venv da automação {automation_id}: {e}")
        return False, f"Falha na reconstrução da venv: {str(e)}"

def clean_automation_workspace(automation_id: int) -> Tuple[bool, str]:
    """Remove a pasta clonada do repositório Git para desocupar espaço no disco."""
    auto_dir = get_automation_dir(automation_id)
    repo_dir = auto_dir / "repo"
    if repo_dir.exists():
        safe_rmtree(repo_dir)
        return True, "Repositório Git removido do disco com sucesso!"
    return True, "Nenhum arquivo de repositório pendente no disco."

def cancel_execution(execution_id: int) -> bool:
    """Cancela um processo em execução imediatamente (em memória, via PID ou sinalizando worker remoto)."""
    killed = False
    with _lock:
        _cancelled_executions.add(execution_id)
        proc = _active_processes.get(execution_id)
        if proc:
            try:
                proc.terminate()
                time.sleep(0.3)
                if proc.poll() is None:
                    proc.kill()
                logger.info(f"Processo da execução {execution_id} encerrado.")
                killed = True
            except Exception as e:
                logger.error(f"Erro ao matar processo da execução {execution_id}: {e}")

    with SessionLocal() as db:
        execution = db.query(Execution).filter(Execution.id == execution_id).first()
        if not execution:
            return killed

        is_remote = bool(
            execution.automation
            and execution.automation.target_worker
            and execution.automation.target_worker.strip().lower() != "local"
        )

        # Caso o processo esteja rodando em um worker separado na mesma máquina, encerrar via PID
        if not killed and not is_remote and execution.status in ["PENDING", "RUNNING"] and execution.process_pid:
            try:
                os.kill(execution.process_pid, signal.SIGTERM)
                killed = True
                logger.info(f"Processo PID {execution.process_pid} da execução {execution_id} encerrado via sinal.")
            except Exception as e:
                logger.warning(f"Não foi possível encerrar PID {execution.process_pid}: {e}")

        if execution.status in ["PENDING", "RUNNING"]:
            now = datetime.now(timezone.utc)
            execution.status = "CANCELLED"
            execution.error_message = "Execução cancelada pelo usuário."
            execution.finished_at = now
            if execution.started_at:
                started = execution.started_at if execution.started_at.tzinfo else execution.started_at.replace(tzinfo=timezone.utc)
                execution.duration_seconds = round((now - started).total_seconds(), 2)
            cancel_msg = "[EXECUÇÃO CANCELADA PELO USUÁRIO]"
            if not execution.logs:
                execution.logs = cancel_msg
            elif cancel_msg not in execution.logs:
                execution.logs += f"\n{cancel_msg}"
            db.commit()
            killed = True

    return killed

def execute_http_automation(automation: Automation, execution: Execution, db) -> None:
    """Executa automação do tipo HTTP."""
    started_at = datetime.now(timezone.utc)
    execution.started_at = started_at
    execution.status = "RUNNING"
    db.commit()

    headers = {}
    if automation.http_headers:
        try:
            headers = json.loads(automation.http_headers)
        except Exception:
            pass

    log_lines = [f"[INÍCIO] Disparo HTTP: {automation.http_method} {automation.http_url}"]
    try:
        with httpx.Client(timeout=automation.timeout_seconds) as client:
            resp = client.request(
                method=automation.http_method or "GET",
                url=automation.http_url,
                headers=headers,
                content=automation.http_body,
            )
            log_lines.append(f"[RESPOSTA] Status: {resp.status_code}")
            log_lines.append(resp.text[:5000])
            execution.exit_code = resp.status_code
            execution.status = "SUCCESS" if resp.is_success else "FAILED"
    except Exception as e:
        log_lines.append(f"[ERRO HTTP] {str(e)}")
        execution.status = "FAILED"
        execution.error_message = str(e)
    finally:
        finished_at = datetime.now(timezone.utc)
        execution.finished_at = finished_at
        execution.duration_seconds = round((finished_at - started_at).total_seconds(), 2)
        execution.logs = sanitize_output("\n".join(log_lines))
        db.commit()

def run_automation_job(automation_id: int, trigger_type: str = "scheduled") -> Optional[int]:
    """Ponto de entrada chamado pelo agendador ou disparo manual assíncrono."""
    with SessionLocal() as db:
        automation = db.query(Automation).filter(Automation.id == automation_id).first()
        if not automation:
            logger.error(f"Automação {automation_id} não encontrada.")
            return None

        if automation.status == "disabled":
            logger.warning(f"Automação {automation_id} está desativada. Execução ignorada.")
            return None

        target_worker = (getattr(automation, "target_worker", None) or "local").strip()
        is_remote = target_worker.lower() != "local" and target_worker != ""

        # Verificar se já está em execução (em memória ou em outro processo/agente via banco)
        with _lock:
            if not is_remote and automation_id in _running_automations:
                logger.warning(f"Automação {automation_id} já está em execução. Nova execução impedida.")
                return None
            active_in_db = db.query(Execution).filter(
                Execution.automation_id == automation_id,
                Execution.status.in_(["PENDING", "RUNNING"])
            ).first()
            if active_in_db:
                logger.warning(f"Automação {automation_id} possui execução ativa no banco (#{active_in_db.id}). Nova execução impedida.")
                return None
            if not is_remote:
                _running_automations.add(automation_id)

        initial_logs = f"[AGUARDANDO AGENTE REMOTO: '{target_worker}']" if is_remote else ""

        # Criar registro de execução
        execution = Execution(
            automation_id=automation.id,
            trigger_type=trigger_type,
            status="PENDING",
            started_at=datetime.now(timezone.utc),
            logs=initial_logs,
        )
        db.add(execution)
        db.commit()
        db.refresh(execution)
        execution_id = execution.id

    if is_remote:
        logger.info(f"Execução #{execution_id} da automação {automation_id} enfileirada para o worker remoto '{target_worker}'.")
        return execution_id

    # Iniciar execução local em thread separada
    thread = threading.Thread(
        target=_worker_thread,
        args=(automation_id, execution_id),
        daemon=True,
    )
    thread.start()
    return execution_id

def _worker_thread(automation_id: int, execution_id: int, attempt: int = 0) -> None:
    """Thread que executa o processo respeitando limite de concorrência e timeout."""
    with _semaphore:
        with SessionLocal() as db:
            automation = db.query(Automation).filter(Automation.id == automation_id).first()
            execution = db.query(Execution).filter(Execution.id == execution_id).first()

            if not automation or not execution:
                with _lock:
                    _running_automations.discard(automation_id)
                    _cancelled_executions.discard(execution_id)
                return

            if automation.source_type == "http":
                try:
                    execute_http_automation(automation, execution, db)
                finally:
                    with _lock:
                        _running_automations.discard(automation_id)
                        _cancelled_executions.discard(execution_id)
                return

            # Execução de script Python (Local ou Git)
            auto_dir = get_automation_dir(automation_id)
            working_dir = auto_dir / "repo" if automation.source_type == "git" else auto_dir

            # Validar Path Traversal para todos os tipos (local e git)
            try:
                entrypoint_path = resolve_safe_path(working_dir, automation.entrypoint)
            except ValueError as ve:
                execution.status = "FAILED"
                execution.error_message = str(ve)
                execution.finished_at = datetime.now(timezone.utc)
                db.commit()
                with _lock:
                    _running_automations.discard(automation_id)
                    _cancelled_executions.discard(execution_id)
                return

            # Para automações Git, se o repositório foi apagado anteriormente (modo efêmero), clona sob demanda
            if automation.source_type == "git" and (not working_dir.exists() or not entrypoint_path.exists()):
                logger.info(f"Repositório não encontrado no disco para automação {automation_id}. Clonando sob demanda...")
                from app.services.git_service import clone_or_update_repo
                try:
                    commit_sha = clone_or_update_repo(
                        working_dir,
                        automation.git_url,
                        automation.git_branch or "main",
                        automation.git_token,
                    )
                    automation.current_commit = commit_sha
                    db.commit()
                except Exception as git_err:
                    execution.status = "FAILED"
                    execution.error_message = f"Falha ao clonar repositório Git: {str(git_err)}"
                    execution.finished_at = datetime.now(timezone.utc)
                    execution.logs = sanitize_output(str(git_err))
                    db.commit()
                    with _lock:
                        _running_automations.discard(automation_id)
                        _cancelled_executions.discard(execution_id)
                    return

            # Preparar ambiente
            try:
                python_bin = ensure_venv(automation_id, working_dir)
            except Exception as e:
                execution.status = "FAILED"
                execution.error_message = f"Falha na preparação do ambiente: {str(e)}"
                execution.finished_at = datetime.now(timezone.utc)
                execution.logs = sanitize_output(str(e))
                db.commit()
                with _lock:
                    _running_automations.discard(automation_id)
                    _cancelled_executions.discard(execution_id)
                return

            if not entrypoint_path.exists():
                execution.status = "FAILED"
                execution.error_message = f"Arquivo principal não encontrado: {automation.entrypoint}"
                execution.finished_at = datetime.now(timezone.utc)
                db.commit()
                with _lock:
                    _running_automations.discard(automation_id)
                    _cancelled_executions.discard(execution_id)
                return

            # Verificar se foi cancelada enquanto preparava o ambiente
            db.refresh(execution)
            with _lock:
                if execution_id in _cancelled_executions or execution.status == "CANCELLED":
                    _cancelled_executions.discard(execution_id)
                    _running_automations.discard(automation_id)
                    return

            # Preparar comando e variáveis de ambiente
            cmd = [str(python_bin), str(entrypoint_path)]
            if automation.arguments:
                import shlex
                cmd.extend(shlex.split(automation.arguments))

            custom_env = os.environ.copy()
            # Garante que o diretório de trabalho esteja no PYTHONPATH para imports locais do projeto
            curr_pythonpath = custom_env.get("PYTHONPATH", "")
            custom_env["PYTHONPATH"] = (
                f"{str(working_dir)}{os.pathsep}{curr_pythonpath}"
                if curr_pythonpath
                else str(working_dir)
            )

            # Processar variáveis de ambiente e gerar o arquivo .env no diretório da automação
            if automation.env_vars:
                try:
                    parsed_vars = parse_env_vars(automation.env_vars)
                    # 1. Escreve o arquivo .env no working_dir para scripts que usam dotenv / pydantic
                    env_file = working_dir / ".env"
                    env_lines = [f"{k}={v}" for k, v in parsed_vars.items()]
                    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

                    # 2. Injeta no os.environ do processo executado
                    for k, v in parsed_vars.items():
                        custom_env[str(k)] = str(v)
                except Exception as e:
                    logger.warning(f"Aviso ao carregar variáveis de ambiente: {e}")

            started_at = datetime.now(timezone.utc)
            execution.started_at = started_at
            execution.status = "RUNNING"
            execution.retry_count = attempt
            db.commit()

            timed_out = False

            try:
                # Execução segura sem shell=True
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(working_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=custom_env,
                )
                execution.process_pid = proc.pid
                db.commit()

                with _lock:
                    _active_processes[execution_id] = proc

                stdout_data, _ = proc.communicate(timeout=automation.timeout_seconds)
                exit_code = proc.returncode

            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                stdout_data, _ = proc.communicate()
                exit_code = -1
            except Exception as e:
                stdout_data = f"Erro na execução do processo: {str(e)}"
                exit_code = -1
            finally:
                with _lock:
                    _active_processes.pop(execution_id, None)

            # Limitar tamanho de log
            if len(stdout_data.encode("utf-8")) > settings.MAX_LOG_SIZE_BYTES:
                stdout_data = stdout_data[:settings.MAX_LOG_SIZE_BYTES] + "\n[LOG TRUNCADO: LIMITE EXCEDIDO]"

            # Verificar se a execução foi cancelada pelo usuário durante a execução
            db.refresh(execution)
            with _lock:
                was_cancelled = (execution_id in _cancelled_executions) or (execution.status == "CANCELLED")
                _cancelled_executions.discard(execution_id)

            finished_at = datetime.now(timezone.utc)
            execution.finished_at = finished_at
            execution.duration_seconds = round((finished_at - started_at).total_seconds(), 2)
            execution.exit_code = exit_code

            sanitized_logs = sanitize_output(stdout_data)
            if was_cancelled:
                cancel_msg = "[EXECUÇÃO CANCELADA PELO USUÁRIO]"
                if cancel_msg not in sanitized_logs:
                    sanitized_logs = f"{sanitized_logs}\n{cancel_msg}".strip()
                execution.logs = sanitized_logs
                execution.status = "CANCELLED"
                execution.error_message = "Execução cancelada pelo usuário."
            else:
                execution.logs = sanitized_logs
                if timed_out:
                    execution.status = "TIMEOUT"
                    execution.error_message = f"Tempo limite de {automation.timeout_seconds}s excedido."
                elif exit_code == 0:
                    execution.status = "SUCCESS"
                else:
                    execution.status = "FAILED"
                    execution.error_message = f"Processo encerrou com código de saída {exit_code}."

            db.commit()

            # Limpeza pós-execução configurada (Modo Efêmero)
            cleanup_mode = getattr(automation, "cleanup_mode", "none")
            if automation.source_type == "git":
                if cleanup_mode == "delete_repo" and working_dir.exists():
                    logger.info(f"Limpeza pós-execução ('delete_repo'): removendo pasta {working_dir}...")
                    safe_rmtree(working_dir)
                elif cleanup_mode == "delete_git":
                    git_folder = working_dir / ".git"
                    if git_folder.exists():
                        logger.info(f"Limpeza pós-execução ('delete_git'): removendo metadados .git de {working_dir}...")
                        safe_rmtree(git_folder)

            # Lógica de Retry (nunca retenta se tiver sido cancelada pelo usuário)
            should_retry = (
                not was_cancelled
                and execution.status in ["FAILED", "TIMEOUT"]
                and attempt < automation.max_retries
            )

            if should_retry:
                logger.info(f"Tentativa {attempt + 1}/{automation.max_retries} para automação {automation_id} em {automation.retry_delay_seconds}s...")
                time.sleep(automation.retry_delay_seconds)
                # Reexecutar
                _worker_thread(automation_id, execution_id, attempt=attempt + 1)
                return

            with _lock:
                _running_automations.discard(automation_id)
