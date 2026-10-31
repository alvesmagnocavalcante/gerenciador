import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import text

from app.core.config import settings
from app.core.database import SessionLocal, init_db
from app.core.security import hash_password, verify_password
from app.models.automation import Automation
from app.models.execution import Execution
from app.models.user import User
from app.services.scheduler import get_job_id


ROOT = Path(__file__).resolve().parents[1]


def test_worker_reconciles_changes_from_another_process(db_session):
    # O processo filho não compartilha scheduler nem memória com a aplicação web.
    stop_path = settings.DATA_DIR / "scheduler.stop"
    script = "\n".join([
        "import time", "from pathlib import Path",
        "from app.services.scheduler import start_scheduler, shutdown_scheduler",
        "start_scheduler()", "print('READY',flush=True)",
        f"stop = Path({str(stop_path)!r})", "deadline = time.monotonic() + 30",
        "while not stop.exists() and time.monotonic() < deadline:", "    time.sleep(0.1)",
        "shutdown_scheduler()",
    ])
    log_path = settings.DATA_DIR / "scheduler-process.log"
    with log_path.open("w", encoding="utf-8") as output:
        child = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT,
                                 stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 10
            while "READY" not in log_path.read_text(encoding="utf-8"):
                assert child.poll() is None, log_path.read_text(encoding="utf-8")
                assert time.monotonic() < deadline, "Worker não iniciou."
                time.sleep(0.1)
            auto = Automation(name="Agendamento externo", target_worker="integration-worker",
                              trigger_type="interval", interval_seconds=1)
            db_session.add(auto)
            db_session.commit()
            job_id = get_job_id(auto.id)

            def stored_job():
                with SessionLocal() as db:
                    return db.execute(text("SELECT job_state FROM apscheduler_jobs WHERE id=:id"), {"id": job_id}).scalar()

            deadline = time.monotonic() + 8
            while not stored_job():
                assert time.monotonic() < deadline, log_path.read_text(encoding="utf-8")
                time.sleep(0.1)

            # Outra instância deve recusar a propriedade do mesmo agendador.
            duplicate = subprocess.run([sys.executable, "-c", "from app.services.scheduler import start_scheduler; start_scheduler()"],
                                       cwd=ROOT, capture_output=True, timeout=10)
            assert duplicate.returncode != 0
            assert b"RUN_SCHEDULER_IN_WEB" in duplicate.stderr

            deadline = time.monotonic() + 8
            while True:
                db_session.expire_all()
                execution = db_session.query(Execution).filter_by(automation_id=auto.id).first()
                if execution:
                    assert execution.status == "PENDING"
                    assert execution.trigger_type == "scheduled"
                    break
                assert time.monotonic() < deadline, log_path.read_text(encoding="utf-8")
                time.sleep(0.1)

            # Alterações de pausa/exclusão também chegam ao proprietário.
            db_session.refresh(auto)
            auto.status = "paused"
            db_session.commit()
            deadline = time.monotonic() + 8
            while stored_job():
                assert time.monotonic() < deadline, log_path.read_text(encoding="utf-8")
                time.sleep(0.1)
        finally:
            stop_path.touch()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=10)
                raise


def test_migrations_and_automatic_upgrade_are_compatible():
    path = settings.DATA_DIR / "migration.db"
    env = {**os.environ, "DB_PATH": str(path)}

    def run(*args):
        completed = subprocess.run([sys.executable, *args], cwd=ROOT, env=env,
                                   capture_output=True, text=True, timeout=20)
        assert completed.returncode == 0, completed.stderr

    run("-m", "alembic", "upgrade", "8a9b0c1d2e3f")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO automations (id,name,source_type,status,entrypoint,timeout_seconds,max_retries,retry_delay_seconds,trigger_type,cleanup_mode,target_worker) VALUES (1,'legacy','local','active','main.py',30,0,0,'none','none','legacy-worker')")
        db.execute("INSERT INTO executions (automation_id,trigger_type,status,started_at,logs,retry_count) VALUES (1,'manual','PENDING','2020-01-01','',0)")
    # Simula startup antes da execução explícita da próxima migração.
    run("-c", "from app.core.database import init_db; init_db(); init_db()")
    run("-m", "alembic", "upgrade", "head")
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(executions)")}
        assert {"owner_id", "heartbeat_at", "cancel_requested"} <= columns
        assert db.execute("SELECT owner_id FROM executions").fetchone()[0] == "remote:legacy-worker"
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "d4e8a71c9f20"
        assert "execution_steps" in {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {row[1] for row in db.execute("PRAGMA index_list(executions)")}
        assert "uq_execution_active_automation" in indexes


def test_bootstrap_rotates_only_legacy_default_password(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_USERNAME", "legacy-admin")
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "new-strong-password-123")
    user = User(username="legacy-admin", password_hash=hash_password("admin123"))
    db_session.add(user)
    db_session.commit()
    init_db()
    db_session.refresh(user)
    assert verify_password(user.password_hash, "new-strong-password-123")
    user.password_hash = hash_password("existing-custom-password")
    db_session.commit()
    init_db()
    db_session.refresh(user)
    assert verify_password(user.password_hash, "existing-custom-password")
