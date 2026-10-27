import time
from pathlib import Path
from app.core.security import sanitize_output
from app.services.executor import (
    run_automation_job,
    cancel_execution,
    get_automation_dir,
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

def test_auto_install_pyproject_dependencies(db_session):
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
    # Criar um pyproject.toml com uma dependência leve pura python
    pyproject = auto_dir / "pyproject.toml"
    pyproject.write_text("""[project]
name = "test-pkg"
version = "0.1.0"
dependencies = [
    "markupsafe>=2.0.0",
]
""", encoding="utf-8")

    script_file = auto_dir / "test_dep.py"
    script_file.write_text("import markupsafe\nprint('MARKUPSAFE_INSTALLED')\n", encoding="utf-8")

    exec_id = run_automation_job(auto.id, trigger_type="manual")
    assert exec_id is not None

    for _ in range(50):
        time.sleep(0.2)
        exec_record = db_session.query(Execution).filter(Execution.id == exec_id).first()
        db_session.refresh(exec_record)
        if exec_record.status not in ["PENDING", "RUNNING"]:
            break

    assert exec_record.status == "SUCCESS"
    assert "MARKUPSAFE_INSTALLED" in exec_record.logs

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






