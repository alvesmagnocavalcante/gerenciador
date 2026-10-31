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
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple
import httpx
from sqlalchemy import or_, and_, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.config import BASE_DIR, settings
from app.core.database import SessionLocal
from app.core.security import sanitize_output, resolve_safe_path, bounded_log
from app.core.progress import PROGRESS_PREFIX, extract_progress
from app.services.runtrail import RUNTRAIL_PREFIX, finalize_running_steps, process_execution_output
from app.models.automation import Automation
from app.models.execution import Execution

logger = logging.getLogger("pyautomation.executor")

# Controle de processos em execução: {execution_id: subprocess.Popen}
_active_processes: Dict[int, subprocess.Popen] = {}
_worker_threads: set[threading.Thread] = set()
_running_automations: set[int] = set()
_cancelled_executions: set[int] = set()
_lock = threading.Lock()
_semaphore = threading.Semaphore(settings.MAX_CONCURRENT_EXECUTIONS)
_owner_id = f"local:{uuid.uuid4().hex}"
_maintenance_stop = threading.Event()
_maintenance_thread: Optional[threading.Thread] = None

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
    """Recupera somente reservas expiradas; tarefas remotas na fila não são órfãs."""
    count = 0
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        cutoff = now - timedelta(seconds=settings.EXECUTION_LEASE_SECONDS)
        stale = or_(Execution.heartbeat_at < cutoff,
                    and_(Execution.heartbeat_at.is_(None), Execution.started_at < cutoff))
        waiting_remote = and_(Execution.status == "PENDING", Execution.owner_id.like("remote:%"))
        orphans = db.query(Execution).filter(
            Execution.status.in_(["PENDING", "RUNNING"]), stale,
            or_(Execution.owner_id.is_(None), ~waiting_remote),
        ).all()
        for ex in orphans:
            with _lock:
                if ex.id in _active_processes or ex.automation_id in _running_automations:
                    continue
            values = {"status": "CANCELLED" if ex.cancel_requested else "FAILED", "finished_at": now,
                      "error_message": "Reserva de execução expirada: executor sem heartbeat.",
                      "logs": bounded_log((ex.logs or "") + "\n[EXECUÇÃO INTERROMPIDA POR REINICIALIZAÇÃO OU PERDA DE HEARTBEAT]")}
            if ex.started_at:
                started = ex.started_at if ex.started_at.tzinfo else ex.started_at.replace(tzinfo=timezone.utc)
                values["duration_seconds"] = round((now - started).total_seconds(), 2)
            # Um heartbeat recebido após a consulta impede a recuperação indevida.
            updated = db.query(Execution).filter(Execution.id == ex.id,
                Execution.status.in_(["PENDING", "RUNNING"]), stale).update(values, synchronize_session=False)
            if updated:
                finalize_running_steps(db, ex.id, values["status"], now)
                count += updated
        if count > 0:
            db.commit()
            logger.info(f"{count} execução(ões) órfã(s) marcada(s) como FAILED na inicialização.")
    return count


def start_executor() -> None:
    global _maintenance_thread
    if _maintenance_thread and _maintenance_thread.is_alive():
        return
    _maintenance_stop.clear()
    def maintain():
        while not _maintenance_stop.is_set():
            try:
                with SessionLocal() as db:
                    with _lock:
                        live_ids = list(_running_automations)
                    db.query(Execution).filter(Execution.owner_id == _owner_id,
                        Execution.automation_id.in_(live_ids),
                        Execution.status.in_(["PENDING", "RUNNING"])).update(
                            {"heartbeat_at": datetime.now(timezone.utc)}, synchronize_session=False)
                    db.commit()
                cleanup_orphaned_executions()
            except SQLAlchemyError as exc:
                logger.error("Falha na manutenção das reservas: %s", sanitize_output(str(exc)))
            _maintenance_stop.wait(settings.HEARTBEAT_INTERVAL_SECONDS)
    _maintenance_thread = threading.Thread(target=maintain, name="execution-heartbeat", daemon=True)
    _maintenance_thread.start()


def stop_executor() -> None:
    _maintenance_stop.set()
    if _maintenance_thread:
        _maintenance_thread.join(timeout=10)
    with SessionLocal() as db:
        ids = [row[0] for row in db.query(Execution.id).filter(
            Execution.owner_id == _owner_id, Execution.status.in_(["PENDING", "RUNNING"])).all()]
    for execution_id in ids:
        cancel_execution(execution_id)
    with _lock:
        threads = list(_worker_threads)
    for thread in threads:
        thread.join(timeout=10)

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
        proc = _run_preparation_command(cmd)
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

    # 1. Instalar a partir de pyproject.toml
    if "pyproject" in dep_files:
        pyproj_path = dep_files["pyproject"]
        logger.info(f"Instalando dependências de {pyproj_path} com uv...")
        cmd = ["uv", "pip", "install", "--python", str(python_bin), "-r", str(pyproj_path)]
        proc = _run_preparation_command(cmd, cwd=working_dir)
        if proc.returncode != 0:
            raise RuntimeError(f"Falha ao instalar dependências de pyproject.toml: {sanitize_output(proc.stderr)}")

    # 2. Instalar a partir de requirements.txt
    if "requirements" in dep_files:
        req_path = dep_files["requirements"]
        logger.info(f"Instalando dependências de {req_path} com uv...")
        cmd = ["uv", "pip", "install", "--python", str(python_bin), "-r", str(req_path)]
        proc = _run_preparation_command(cmd, cwd=working_dir)
        if proc.returncode != 0:
            raise RuntimeError(f"Falha ao instalar dependências de requirements.txt: {sanitize_output(proc.stderr)}")

    hash_file.write_text(current_hash, encoding="utf-8")

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
        logger.error("Erro ao reconstruir venv da automação %s: %s", automation_id, sanitize_output(str(e)))
        return False, f"Falha na reconstrução da venv: {sanitize_output(str(e))}"

def clean_automation_workspace(automation_id: int) -> Tuple[bool, str]:
    """Remove a pasta clonada do repositório Git para desocupar espaço no disco."""
    auto_dir = get_automation_dir(automation_id)
    repo_dir = auto_dir / "repo"
    if repo_dir.exists():
        safe_rmtree(repo_dir)
        return True, "Repositório Git removido do disco com sucesso!"
    return True, "Nenhum arquivo de repositório pendente no disco."

def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Falha ao encerrar árvore de processos: %s", sanitize_output(str(exc)))
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    if proc.poll() is None:
        proc.kill()


def cancel_execution(execution_id: int) -> bool:
    """Solicita cancelamento sem liberar a reserva antes de o executor parar."""
    with SessionLocal() as db:
        execution = db.get(Execution, execution_id)
        if not execution or execution.status not in ["PENDING", "RUNNING"]:
            return False
        changed = db.query(Execution).filter(Execution.id == execution_id,
            Execution.status.in_(["PENDING", "RUNNING"])).update(
                {"cancel_requested": True}, synchronize_session=False)
        if not changed:
            return False
        # Se um poller já assumiu a tarefa, manter RUNNING até a confirmação.
        db.query(Execution).filter(Execution.id == execution_id, Execution.status == "PENDING").update(
            {"status": "CANCELLED", "finished_at": datetime.now(timezone.utc),
             "error_message": "Execução cancelada pelo usuário.",
             "logs": bounded_log((execution.logs or "") + "\n[EXECUÇÃO CANCELADA PELO USUÁRIO]")}, synchronize_session=False)
        db.commit()
    with _lock:
        proc = _active_processes.get(execution_id)
    if proc:
        _terminate_process(proc)
    return True


def _is_cancelled(execution_id: int) -> bool:
    with SessionLocal() as db:
        execution = db.get(Execution, execution_id)
        return (execution is None or execution.cancel_requested
                or execution.status not in ["PENDING", "RUNNING"])


def execute_http_automation(automation: Automation, execution: Execution, db) -> tuple:
    """Uma tentativa HTTP; a finalização e os retries pertencem ao worker."""
    if _is_cancelled(execution.id):
        return "CANCELLED", None, "", "Execução cancelada pelo usuário."
    headers = json.loads(automation.http_headers) if automation.http_headers else {}
    if not isinstance(headers, dict):
        raise ValueError("http_headers deve ser um objeto JSON.")
    log = f"[INÍCIO] Disparo HTTP: {automation.http_method} {automation.http_url}"
    try:
        # Streaming limita a memória mesmo que o servidor devolva um corpo enorme.
        with httpx.Client(timeout=automation.timeout_seconds) as client:
            with client.stream(automation.http_method or "GET", automation.http_url,
                               headers=headers, content=automation.http_body) as response:
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    data.extend(chunk[:max(0, 5000 - len(data))])
                    if len(data) >= 5000 or _is_cancelled(execution.id):
                        break
                log += f"\n[RESPOSTA] Status: {response.status_code}\n" + data.decode("utf-8", errors="replace")
                return ("SUCCESS" if response.is_success else "FAILED", response.status_code, log, None)
    except httpx.TimeoutException as exc:
        return "TIMEOUT", None, log, str(exc)
    except httpx.HTTPError as exc:
        return "FAILED", None, log, str(exc)


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

        initial_logs = f"[AGUARDANDO AGENTE REMOTO: '{target_worker}']" if is_remote else ""

        # Criar registro de execução
        execution = Execution(
            automation_id=automation.id,
            trigger_type=trigger_type,
            status="PENDING",
            started_at=datetime.now(timezone.utc),
            logs=initial_logs,
            owner_id=f"remote:{target_worker}" if is_remote else _owner_id,
            heartbeat_at=datetime.now(timezone.utc),
        )
        db.add(execution)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if db.query(Execution.id).filter(Execution.automation_id == automation_id,
                    Execution.status.in_(["PENDING", "RUNNING"])).first():
                return None
            raise
        db.refresh(execution)
        execution_id = execution.id

    if is_remote:
        logger.info(f"Execução #{execution_id} da automação {automation_id} enfileirada para o worker remoto '{target_worker}'.")
        return execution_id

    with _lock:
        _running_automations.add(automation_id)

    # Iniciar execução local em thread separada
    thread = threading.Thread(
        target=_worker_thread,
        args=(automation_id, execution_id),
        daemon=True,
    )
    with _lock:
        _worker_threads.add(thread)
    try:
        thread.start()
    except RuntimeError:
        with _lock:
            _worker_threads.discard(thread)
            _running_automations.discard(automation_id)
        with SessionLocal() as db:
            db.query(Execution).filter(Execution.id == execution_id).update(
                {"status": "FAILED", "error_message": "Não foi possível iniciar a thread do executor.",
                 "finished_at": datetime.now(timezone.utc)})
            db.commit()
        raise
    return execution_id

class _OutputBuffer:
    """Drena stdout continuamente, mantendo apenas uma quantidade limitada de bytes."""
    def __init__(self):
        self.data = bytearray()
        self.lock = threading.Lock()
        self.truncated = False
        self.progress_pending = ""
        self.latest_progress_event: Optional[str] = None
        self.runtrail_events: list[str] = []

    def _track_progress(self, chunk: bytes) -> None:
        text = self.progress_pending + chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self.progress_pending = lines.pop()
        if len(self.progress_pending.encode("utf-8")) > 4096:
            # Uma linha comum sem quebra não pode fazer o rastreador crescer sem limite.
            self.progress_pending = ""
        for line in lines:
            candidate = line.rstrip("\r")
            if not candidate.startswith(PROGRESS_PREFIX):
                if candidate.startswith(RUNTRAIL_PREFIX) and len(candidate.encode("utf-8")) <= 4096:
                    self.runtrail_events.append(candidate)
                    if len(self.runtrail_events) > 1000:
                        del self.runtrail_events[:len(self.runtrail_events) - 1000]
                continue
            update = extract_progress(candidate + "\n")
            if update.updated_at is not None:
                self.latest_progress_event = candidate

    def read(self, pipe):
        try:
            while chunk := pipe.read1(65536):
                with self.lock:
                    self._track_progress(chunk)
                    remaining = settings.MAX_LOG_SIZE_BYTES + 4096 - len(self.data)
                    self.data.extend(chunk[:max(0, remaining)])
                    self.truncated |= len(chunk) > remaining
        except OSError as exc:
            logger.warning("Falha ao ler stdout: %s", sanitize_output(str(exc)))
        finally:
            pipe.close()

    def snapshot(self):
        with self.lock:
            text = bytes(self.data).decode("utf-8", errors="replace")
            truncated = self.truncated
        if truncated:
            text += "\n[LOG TRUNCADO: LIMITE EXCEDIDO]"
        text = bounded_log(text)
        with self.lock:
            latest_progress_event = self.latest_progress_event
            runtrail_events = list(self.runtrail_events)
        if latest_progress_event:
            text += f"\n{latest_progress_event}\n"
        if runtrail_events:
            text += "\n" + "\n".join(runtrail_events) + "\n"
        return text


def _run_preparation_command(cmd: list[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, **options)
    buffer = _OutputBuffer()
    reader = threading.Thread(target=buffer.read, args=(proc.stdout,), daemon=True)
    reader.start()
    try:
        proc.wait(timeout=300)
    except subprocess.TimeoutExpired:
        _terminate_process(proc)
        proc.wait(timeout=10)
        raise
    finally:
        reader.join(timeout=2)
    output = buffer.snapshot()
    return subprocess.CompletedProcess(cmd, proc.returncode, output, output if proc.returncode else "")


def _run_python_attempt(automation: Automation, execution: Execution, db) -> tuple:
    auto_dir = get_automation_dir(automation.id)
    working_dir = auto_dir / "repo" if automation.source_type == "git" else auto_dir
    if automation.source_type == "git" and not (working_dir / automation.entrypoint).is_file():
        from app.services.git_service import clone_or_update_repo
        automation.current_commit = clone_or_update_repo(
            working_dir, automation.git_url, automation.git_branch or "main", automation.git_token)
        db.commit()
    # Verificação feita depois do clone: resolve também links criados pelo Git.
    entrypoint = resolve_safe_path(working_dir, automation.entrypoint)
    if not entrypoint.is_file():
        raise ValueError(f"Arquivo principal não encontrado: {automation.entrypoint}")
    if _is_cancelled(execution.id):
        return "CANCELLED", None, "", "Execução cancelada pelo usuário."
    python_bin = ensure_venv(automation.id, working_dir)
    import shlex
    cmd = [str(python_bin), str(entrypoint), *shlex.split(automation.arguments or "")]
    env = os.environ.copy()
    python_paths = [str(BASE_DIR / "runtime"), str(working_dir)]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    env["PYAUTOMATION_EXECUTION_ID"] = str(execution.id)
    env["PYAUTOMATION_RETRY_COUNT"] = str(execution.retry_count or 0)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    parsed_vars = parse_env_vars(automation.env_vars)
    if automation.env_vars is not None:
        (working_dir / ".env").write_text("".join(f"{k}={v}\n" for k, v in parsed_vars.items()), encoding="utf-8")
    env.update(parsed_vars)
    if _is_cancelled(execution.id):
        return "CANCELLED", None, "", "Execução cancelada pelo usuário."
    # Revalidar imediatamente antes do subprocesso.
    cmd[1] = str(resolve_safe_path(working_dir, automation.entrypoint))
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=str(working_dir), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, **options)
    with _lock:
        _active_processes[execution.id] = proc
    buffer = _OutputBuffer()
    reader = threading.Thread(target=buffer.read, args=(proc.stdout,), daemon=True)
    reader.start()
    deadline = time.monotonic() + automation.timeout_seconds
    next_update = 0.0
    status = None
    try:
        db.query(Execution).filter(Execution.id == execution.id).update({"process_pid": proc.pid})
        db.commit()
        while proc.poll() is None:
            if _is_cancelled(execution.id):
                status = "CANCELLED"
                _terminate_process(proc)
                break
            if time.monotonic() >= deadline:
                status = "TIMEOUT"
                _terminate_process(proc)
                break
            if time.monotonic() >= next_update:
                snapshot_values = process_execution_output(db, execution.id, buffer.snapshot())
                db.query(Execution).filter(Execution.id == execution.id,
                    Execution.status == "RUNNING").update(snapshot_values, synchronize_session=False)
                db.commit()
                next_update = time.monotonic() + 1
            time.sleep(0.1)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            _terminate_process(proc)
        reader.join(timeout=2)
        with _lock:
            _active_processes.pop(execution.id, None)
    status = status or ("SUCCESS" if proc.returncode == 0 else "FAILED")
    error = None
    if status == "TIMEOUT":
        error = f"Tempo limite de {automation.timeout_seconds}s excedido."
    elif status == "FAILED":
        error = f"Processo encerrou com código de saída {proc.returncode}."
    return status, proc.returncode, buffer.snapshot(), error


def _try_claim_local(execution_id: int) -> bool:
    """Serializa a contagem e a reserva de vagas entre processos do mesmo SQLite."""
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        running = db.query(Execution.id).filter(Execution.status == "RUNNING",
            Execution.owner_id.like("local:%")).count()
        if running >= settings.MAX_CONCURRENT_EXECUTIONS:
            db.rollback()
            return False
        now = datetime.now(timezone.utc)
        claimed = db.query(Execution).filter(Execution.id == execution_id,
            Execution.status == "PENDING", Execution.cancel_requested.is_(False)).update(
                {"status": "RUNNING", "started_at": now, "heartbeat_at": now}, synchronize_session=False)
        db.commit()
        return bool(claimed)


def _worker_thread(automation_id: int, execution_id: int) -> None:
    """Uma única reserva de semáforo durante todas as tentativas."""
    try:
        with _semaphore, SessionLocal() as db:
            automation = db.get(Automation, automation_id)
            execution = db.get(Execution, execution_id)
            if not automation or not execution or _is_cancelled(execution_id):
                return
            while not _try_claim_local(execution_id):
                if _is_cancelled(execution_id):
                    return
                time.sleep(0.2)
            db.refresh(execution)
            for attempt in range(automation.max_retries + 1):
                if _is_cancelled(execution_id):
                    result = ("CANCELLED", None, "", "Execução cancelada pelo usuário.")
                    break
                execution.retry_count = attempt
                if attempt > 0:
                    execution.progress_percent = None
                    execution.progress_message = None
                    execution.progress_updated_at = None
                db.commit()
                try:
                    result = (execute_http_automation(automation, execution, db)
                              if automation.source_type == "http"
                              else _run_python_attempt(automation, execution, db))
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    result = ("FAILED", None, str(exc), str(exc))
                status, code, logs, error = result
                if _is_cancelled(execution_id) or status in ["SUCCESS", "CANCELLED"] or attempt >= automation.max_retries:
                    break
                # Manter RUNNING durante backoff impede novas execuções concorrentes.
                retry_values = process_execution_output(
                    db, execution.id, logs + f"\n[RETRY {attempt + 1}/{automation.max_retries}]"
                )
                for key, value in retry_values.items():
                    setattr(execution, key, value)
                execution.error_message = sanitize_output(error or "") or None
                db.commit()
                deadline = time.monotonic() + automation.retry_delay_seconds
                while time.monotonic() < deadline and not _is_cancelled(execution_id):
                    time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            status, code, logs, error = result
            db.refresh(execution)
            if execution.status not in ["PENDING", "RUNNING"]:
                return
            if execution.cancel_requested:
                status, error = "CANCELLED", "Execução cancelada pelo usuário."
                logs += "\n[EXECUÇÃO CANCELADA PELO USUÁRIO]"
            if automation.source_type == "git":
                repo = get_automation_dir(automation_id) / "repo"
                if automation.cleanup_mode == "delete_repo":
                    safe_rmtree(repo)
                elif automation.cleanup_mode == "delete_git":
                    safe_rmtree(repo / ".git")
            finished = datetime.now(timezone.utc)
            started = execution.started_at.replace(tzinfo=timezone.utc) if execution.started_at.tzinfo is None else execution.started_at
            # CAS impede que um cancelamento simultâneo seja sobrescrito.
            values = {"status": status, "exit_code": code,
                      "error_message": sanitize_output(error or "") or None,
                      "finished_at": finished, "duration_seconds": round((finished - started).total_seconds(), 2)}
            values.update(process_execution_output(db, execution.id, logs))
            finalize_running_steps(db, execution.id, status, finished)
            if status == "SUCCESS" and (values.get("progress_percent") is not None or execution.progress_percent is not None):
                values["progress_percent"] = 100
                values["progress_message"] = values.get("progress_message") or execution.progress_message or "Concluído"
                values["progress_updated_at"] = finished
            updated = db.query(Execution).filter(Execution.id == execution_id,
                Execution.status == "RUNNING", Execution.cancel_requested == execution.cancel_requested).update(values, synchronize_session=False)
            db.commit()
            if not updated:
                _finish_cancelled(execution_id, logs)
    except Exception as exc:
        # Fronteira da thread: nenhuma falha pode deixar a reserva presa indefinidamente.
        logger.error("Falha no executor #%s: %s", execution_id, sanitize_output(str(exc)))
        with SessionLocal() as db:
            execution = db.get(Execution, execution_id)
            if execution and execution.status in ["PENDING", "RUNNING"]:
                execution.status = "CANCELLED" if execution.cancel_requested else "FAILED"
                execution.finished_at = datetime.now(timezone.utc)
                execution.error_message = sanitize_output(str(exc))
                failure_values = process_execution_output(
                    db, execution.id, (execution.logs or "") + "\n" + str(exc)
                )
                finalize_running_steps(db, execution.id, execution.status, execution.finished_at)
                for key, value in failure_values.items():
                    setattr(execution, key, value)
                db.commit()
    finally:
        with _lock:
            _running_automations.discard(automation_id)
            _cancelled_executions.discard(execution_id)
            _worker_threads.discard(threading.current_thread())


def _finish_cancelled(execution_id: int, logs: str) -> None:
    with SessionLocal() as db:
        values = {"status": "CANCELLED", "finished_at": datetime.now(timezone.utc),
                  "error_message": "Execução cancelada pelo usuário."}
        values.update(process_execution_output(
            db, execution_id, logs + "\n[EXECUÇÃO CANCELADA PELO USUÁRIO]"
        ))
        finalize_running_steps(db, execution_id, "CANCELLED", values["finished_at"])
        db.query(Execution).filter(Execution.id == execution_id, Execution.status == "RUNNING",
            Execution.cancel_requested.is_(True)).update(values, synchronize_session=False)
        db.commit()
