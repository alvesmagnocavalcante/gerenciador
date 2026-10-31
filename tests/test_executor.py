from datetime import datetime, timezone, timedelta
import time
from pathlib import Path
from app.core.security import sanitize_output, resolve_safe_path
from app.services.executor import (
    run_automation_job,
    cancel_execution,
    get_automation_dir,
    cleanup_orphaned_executions,
    delete_automation_files,
    _active_processes,
)
from app.models.automation import Automation
from app.models.execution import Execution

def test_sanitize_output():
    raw = "Error in https://user:secret123@github.com/repo, token=ghp_123456789012345678901234567890123456, Bearer eyJhbGciOi"
    sanitized = sanitize_output(raw)
    assert "ghp_" not in sanitized
    assert "[TOKEN_REDACTED]" in sanitized
    assert "secret123" not in sanitized
    assert "[CREDENTIALS_REDACTED]" in sanitized

def test_execute_local_python_script(db_session):
    auto = Automation(
        name="Teste Local Python",
        source_type="local",
        status="active",
        entrypoint="test_script.py",
        timeout_seconds=10,
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    # Criar script na pasta da automação
    auto_dir = get_automation_dir(auto.id)
    script_file = auto_dir / "test_script.py"
    script_file.write_text("print('HELLO_FROM_TEST')\n", encoding="utf-8")

    # Disparar execução
    exec_id = run_automation_job(auto.id, trigger_type="manual")
    assert exec_id is not None

    # Aguardar término
    for _ in range(50):
        time.sleep(0.2)
        exec_record = db_session.query(Execution).filter(Execution.id == exec_id).first()
        db_session.refresh(exec_record)
        if exec_record.status not in ["PENDING", "RUNNING"]:
            break

    assert exec_record.status == "SUCCESS"
    assert "HELLO_FROM_TEST" in exec_record.logs
    assert exec_record.exit_code == 0
    assert exec_record.duration_seconds is not None

def test_execute_script_timeout(db_session):
    auto = Automation(
        name="Teste Timeout",
        source_type="local",
        status="active",
        entrypoint="sleep_script.py",
        timeout_seconds=1,  # 1 segundo de timeout
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    auto_dir = get_automation_dir(auto.id)
    script_file = auto_dir / "sleep_script.py"
    script_file.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")

    exec_id = run_automation_job(auto.id, trigger_type="manual")
    assert exec_id is not None

    for _ in range(50):
        time.sleep(0.2)
        exec_record = db_session.query(Execution).filter(Execution.id == exec_id).first()
        db_session.refresh(exec_record)
        if exec_record.status not in ["PENDING", "RUNNING"]:
            break

    assert exec_record.status == "TIMEOUT"
    assert "Tempo limite" in exec_record.error_message

def test_prevent_duplicate_concurrent_run(db_session):
    auto = Automation(
        name="Teste Bloqueio Duplicata",
        source_type="local",
        status="active",
        entrypoint="busy.py",
        timeout_seconds=5,
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    auto_dir = get_automation_dir(auto.id)
    script_file = auto_dir / "busy.py"
    script_file.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")

    first_exec = run_automation_job(auto.id, trigger_type="manual")
    assert first_exec is not None

    # Tentar rodar a mesma automação enquanto o primeiro ainda roda
    second_exec = run_automation_job(auto.id, trigger_type="manual")
    assert second_exec is None  # Deve bloquear para evitar concorrência descontrolada!

    # Aguardar o primeiro terminar
    for _ in range(50):
        time.sleep(0.2)
        exec_record = db_session.query(Execution).filter(Execution.id == first_exec).first()
        db_session.refresh(exec_record)
        if exec_record.status not in ["PENDING", "RUNNING"]:
            break

    assert exec_record.status == "SUCCESS"

def test_auto_install_pyproject_dependencies(db_session, monkeypatch):
    auto = Automation(
        name="Teste Pyproject Deps",
        source_type="local",
        status="active",
        entrypoint="test_dep.py",
        timeout_seconds=30,
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    auto_dir = get_automation_dir(auto.id)
    # Instalação real de wheel local, sem dependência de rede.
    wheel = auto_dir / "review_dependency-1.0-py3-none-any.whl"
    import zipfile
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("review_dependency/__init__.py", "VALUE = 42\n")
        prefix = "review_dependency-1.0.dist-info/"
        archive.writestr(prefix + "METADATA", "Metadata-Version: 2.1\nName: review-dependency\nVersion: 1.0\n")
        archive.writestr(prefix + "WHEEL", "Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(prefix + "RECORD", "")
    (auto_dir / "pyproject.toml").write_text(
        '[project]\nname="test-pkg"\nversion="0.1.0"\ndependencies=["review-dependency @ ' + wheel.as_uri() + '"]\n', encoding="utf-8")
    (auto_dir / "test_dep.py").write_text("import review_dependency\nassert review_dependency.VALUE == 42\nprint('LOCAL_DEP_INSTALLED')\n", encoding="utf-8")
    monkeypatch.setenv("UV_OFFLINE", "true")

    exec_id = run_automation_job(auto.id, trigger_type="manual")
    assert exec_id is not None

    for _ in range(50):
        time.sleep(0.2)
        exec_record = db_session.query(Execution).filter(Execution.id == exec_id).first()
        db_session.refresh(exec_record)
        if exec_record.status not in ["PENDING", "RUNNING"]:
            break

    assert exec_record.status == "SUCCESS"
    assert "LOCAL_DEP_INSTALLED" in exec_record.logs
    # Verifica que o arquivo de cache de hash das dependências foi criado
    assert (auto_dir / ".venv" / ".deps_hash").is_file()

def test_parse_and_inject_env_vars(db_session):
    from app.services.executor import parse_env_vars

    # Teste de parsing padrão .env
    raw_env = """
    # Comentário que deve ser ignorado
    API_KEY=secret_key_123
    DATABASE_URL="postgresql://user:pass@host/db"
    DEBUG='true'
    export TIMEOUT=60
    """
    parsed = parse_env_vars(raw_env)
    assert parsed["API_KEY"] == "secret_key_123"
    assert parsed["DATABASE_URL"] == "postgresql://user:pass@host/db"
    assert parsed["DEBUG"] == "true"
    assert parsed["TIMEOUT"] == "60"

    # Teste de execução física injetando no os.environ e criando arquivo .env
    auto = Automation(
        name="Teste Env Vars",
        source_type="local",
        status="active",
        entrypoint="test_env.py",
        env_vars=raw_env,
        timeout_seconds=10,
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    auto_dir = get_automation_dir(auto.id)
    script_file = auto_dir / "test_env.py"
    script_file.write_text("""import os
from pathlib import Path

# Verifica se o arquivo .env físico foi criado
env_file_exists = (Path(__file__).parent / ".env").is_file()
print("ENV_FILE_CREATED=" + str(env_file_exists))

# Verifica se a variável foi injetada no os.environ
api_key = os.getenv("API_KEY")
timeout_val = os.getenv("TIMEOUT")
print("API_KEY=" + str(api_key))
print("TIMEOUT_VAL=" + str(timeout_val))
""", encoding="utf-8")

    exec_id = run_automation_job(auto.id, trigger_type="manual")
    assert exec_id is not None

    for _ in range(50):
        time.sleep(0.2)
        exec_record = db_session.query(Execution).filter(Execution.id == exec_id).first()
        db_session.refresh(exec_record)
        if exec_record.status not in ["PENDING", "RUNNING"]:
            break

    assert exec_record.status == "SUCCESS"
    assert "ENV_FILE_CREATED=True" in exec_record.logs
    assert "TIMEOUT_VAL=60" in exec_record.logs
    assert "API_KEY=[REDACTED]" in exec_record.logs

def test_clean_workspace(db_session):
    from app.services.executor import clean_automation_workspace

    auto = Automation(
        name="Teste Clean Workspace",
        source_type="git",
        status="active",
        entrypoint="main.py",
        git_url="https://github.com/example/repo.git",
        cleanup_mode="delete_repo",
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    auto_dir = get_automation_dir(auto.id)
    repo_dir = auto_dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "dummy.txt").write_text("dummy", encoding="utf-8")

    assert repo_dir.exists()
    ok, msg = clean_automation_workspace(auto.id)
    assert ok is True
    assert not repo_dir.exists()

def test_custom_entrypoint(db_session):
    from app.services.executor import safe_rmtree

    auto = Automation(
        name="Teste Custom Entrypoint",
        source_type="local",
        status="active",
        entrypoint="custom_runner.py",
        timeout_seconds=10,
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    auto_dir = get_automation_dir(auto.id)
    script_file = auto_dir / "custom_runner.py"
    script_file.write_text("print('CUSTOM_ENTRYPOINT_EXECUTED_OK')\n", encoding="utf-8")

    exec_id = run_automation_job(auto.id, trigger_type="manual")
    assert exec_id is not None

    for _ in range(50):
        time.sleep(0.2)
        exec_record = db_session.query(Execution).filter(Execution.id == exec_id).first()
        db_session.refresh(exec_record)
        if exec_record.status not in ["PENDING", "RUNNING"]:
            break

    assert exec_record.status == "SUCCESS"
    assert "CUSTOM_ENTRYPOINT_EXECUTED_OK" in exec_record.logs

    # Test safe_rmtree removes dir safely
    test_sub = auto_dir / "temp_folder"
    test_sub.mkdir(parents=True, exist_ok=True)
    (test_sub / "file.txt").write_text("test", encoding="utf-8")
    safe_rmtree(test_sub)
    assert not test_sub.exists()

def test_normalize_git_url():
    from app.services.git_service import normalize_git_url
    assert normalize_git_url("https://github.com/user/repo.git") == "github.com/user/repo"
    assert normalize_git_url("https://token@github.com/user/repo") == "github.com/user/repo"
    assert normalize_git_url("http://github.com/user/repo/") == "github.com/user/repo"

def test_path_traversal_blocked_local(db_session):
    auto = Automation(
        name="Teste Path Traversal Local",
        source_type="local",
        status="active",
        entrypoint="../outside.py",
        timeout_seconds=5,
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    exec_id = run_automation_job(auto.id, trigger_type="manual")
    assert exec_id is not None

    for _ in range(30):
        time.sleep(0.1)
        exec_record = db_session.query(Execution).filter(Execution.id == exec_id).first()
        db_session.refresh(exec_record)
        if exec_record.status not in ["PENDING", "RUNNING"]:
            break

    assert exec_record.status == "FAILED"
    assert "Path traversal detectado" in exec_record.error_message

def test_cancel_execution_prevents_retry_and_keeps_cancelled_status(db_session):
    auto = Automation(
        name="Teste Cancelamento Sem Retry",
        source_type="local",
        status="active",
        entrypoint="long_job.py",
        timeout_seconds=20,
        max_retries=2,
        retry_delay_seconds=1,
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    auto_dir = get_automation_dir(auto.id)
    (auto_dir / "long_job.py").write_text("import time\nprint('STARTING_LONG_JOB', flush=True)\ntime.sleep(10)\n", encoding="utf-8")

    exec_id = run_automation_job(auto.id, trigger_type="manual")
    assert exec_id is not None

    # Aguardar processo iniciar de fato
    for _ in range(50):
        time.sleep(0.2)
        exec_record = db_session.query(Execution).filter(Execution.id == exec_id).first()
        db_session.refresh(exec_record)
        if exec_record.status == "RUNNING" and exec_record.process_pid is not None:
            break

    cancelled = cancel_execution(exec_id)
    assert cancelled is True

    # Aguardar a thread encerrar e verificar que continua CANCELLED (e não FAILED ou em retry)
    time.sleep(1.0)
    db_session.refresh(exec_record)
    assert exec_record.status == "CANCELLED"
    assert exec_record.retry_count == 0
    assert "[EXECUÇÃO CANCELADA PELO USUÁRIO]" in exec_record.logs

def test_cleanup_orphaned_executions_and_delete_files(db_session):
    auto = Automation(
        name="Teste Órfãs e Exclusão",
        source_type="local",
        status="active",
        entrypoint="main.py",
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    auto_dir = get_automation_dir(auto.id)
    (auto_dir / "main.py").write_text("print(1)\n", encoding="utf-8")

    orphan = Execution(
        automation_id=auto.id,
        trigger_type="scheduled",
        status="RUNNING",
        started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        logs="Iniciando antes de queda...",
    )
    db_session.add(orphan)
    db_session.commit()
    db_session.refresh(orphan)

    cleaned = cleanup_orphaned_executions()
    assert cleaned >= 1

    db_session.refresh(orphan)
    assert orphan.status == "FAILED"
    assert "expirada" in orphan.error_message

    assert auto_dir.exists()
    delete_automation_files(auto.id)
    assert not auto_dir.exists()
