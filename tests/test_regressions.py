import hashlib
import hmac
import io
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.orm import Query

from app.api.routes import worker_poll_next_job
from app.core.config import Settings, settings
from app.core.database import SessionLocal
from app.core.security import bounded_log, sign_session, verify_session
from app.models.automation import Automation
from app.models.execution import Execution
from app.services import executor


def create_auto(db, **kwargs):
    auto = Automation(name="Regressão", **kwargs)
    db.add(auto)
    db.commit()
    db.refresh(auto)
    return auto


def wait_finished(db, execution_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db.expire_all()
        ex = db.get(Execution, execution_id)
        if ex.status not in ["PENDING", "RUNNING"]:
            return ex
        time.sleep(0.05)
    pytest.fail(f"Execução #{execution_id} não terminou: {ex.status}, {ex.error_message}")


def test_concurrent_dispatch_reserves_one_execution(db_session):
    auto = create_auto(db_session, target_worker="race-worker")
    barrier = threading.Barrier(2, timeout=5)

    def before_flush(session, *_):
        if any(isinstance(obj, Execution) and obj.automation_id == auto.id for obj in session.new):
            barrier.wait()

    event.listen(SessionLocal.class_, "before_flush", before_flush)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: executor.run_automation_job(auto.id), range(2)))
    finally:
        event.remove(SessionLocal.class_, "before_flush", before_flush)
    assert sum(value is not None for value in results) == 1
    assert db_session.query(Execution).filter_by(automation_id=auto.id).count() == 1


def test_two_pollers_cannot_claim_the_same_execution(db_session):
    auto = create_auto(db_session, target_worker="claim-worker")
    execution_id = executor.run_automation_job(auto.id)
    barrier = threading.Barrier(2, timeout=5)

    class RacingQuery(Query):
        def first(self):
            result = super().first()
            if isinstance(result, Execution):
                barrier.wait()
            return result

    def poll(_):
        with SessionLocal(query_cls=RacingQuery) as db:
            return worker_poll_next_job(worker_name="claim-worker", db=db, user=None)["job"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(poll, range(2)))
    assert [job["execution_id"] for job in jobs if job] == [execution_id]


def test_retry_works_with_one_slot(db_session, monkeypatch):
    monkeypatch.setattr(executor, "_semaphore", threading.Semaphore(1))
    monkeypatch.setattr(executor, "ensure_venv", lambda *args, **kwargs: Path(sys.executable))
    auto = create_auto(db_session, max_retries=1, retry_delay_seconds=0)
    (executor.get_automation_dir(auto.id) / "main.py").write_text(
        "from pathlib import Path\np=Path('attempt')\n"
        "if not p.exists():\n p.touch()\n raise SystemExit(1)\nprint('RETRY_OK')\n", encoding="utf-8")
    ex = wait_finished(db_session, executor.run_automation_job(auto.id))
    assert ex.status == "SUCCESS", ex.error_message
    assert ex.retry_count == 1
    assert "RETRY_OK" in ex.logs


def test_database_enforces_global_limit_across_executor_owners(db_session, monkeypatch):
    monkeypatch.setattr(settings, "MAX_CONCURRENT_EXECUTIONS", 1)
    autos = [create_auto(db_session) for _ in range(2)]
    executions = [Execution(automation_id=auto.id, owner_id=f"local:process-{i}") for i, auto in enumerate(autos)]
    db_session.add_all(executions)
    db_session.commit()
    assert executor._try_claim_local(executions[0].id)
    assert not executor._try_claim_local(executions[1].id)
    db_session.expire_all()
    assert executions[1].status == "PENDING"
    for execution in executions:
        execution.status = "CANCELLED"
    db_session.commit()
    # Uma vaga liberada volta a permitir o claim de outra tarefa.
    executions[1].status = "PENDING"
    db_session.commit()
    assert executor._try_claim_local(executions[1].id)
    db_session.expire_all()
    executions[1].status = "CANCELLED"
    db_session.commit()


def test_startup_cleanup_preserves_live_and_queued_remote_jobs(db_session):
    now = datetime.now(timezone.utc)
    autos = [create_auto(db_session, target_worker=f"lease-{i}") for i in range(3)]
    live = Execution(automation_id=autos[0].id, status="RUNNING", owner_id="remote:live", heartbeat_at=now)
    queued = Execution(automation_id=autos[1].id, status="PENDING", owner_id="remote:queued", started_at=now-timedelta(days=2))
    expired = Execution(automation_id=autos[2].id, status="RUNNING", owner_id="remote:dead", heartbeat_at=now-timedelta(hours=1))
    db_session.add_all([live, queued, expired])
    db_session.commit()
    executor.cleanup_orphaned_executions()
    db_session.expire_all()
    assert live.status == "RUNNING"
    assert queued.status == "PENDING"
    assert expired.status == "FAILED"


def test_cancelled_queued_http_job_never_sends_request(db_session, monkeypatch):
    semaphore = threading.Semaphore(0)
    monkeypatch.setattr(executor, "_semaphore", semaphore)
    client = Mock()
    monkeypatch.setattr(executor.httpx, "Client", client)
    auto = create_auto(db_session, source_type="http", http_url="https://example.invalid")
    execution_id = executor.run_automation_job(auto.id)
    assert executor.cancel_execution(execution_id)
    semaphore.release()
    deadline = time.monotonic() + 5
    while auto.id in executor._running_automations and time.monotonic() < deadline:
        time.sleep(0.05)
    assert auto.id not in executor._running_automations
    client.assert_not_called()
    assert wait_finished(db_session, execution_id).status == "CANCELLED"


def test_http_completion_preserves_inflight_cancellation(db_session, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original_client = httpx.Client

    def handler(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, text="OK")

    monkeypatch.setattr(executor.httpx, "Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs))
    auto = create_auto(db_session, source_type="http", http_url="https://example.invalid", max_retries=1)
    execution_id = executor.run_automation_job(auto.id)
    assert entered.wait(5)
    try:
        assert executor.cancel_execution(execution_id)
        # A reserva permanece ativa enquanto a requisição está em curso.
        assert executor.run_automation_job(auto.id) is None
    finally:
        release.set()
    ex = wait_finished(db_session, execution_id)
    assert ex.status == "CANCELLED"
    assert ex.retry_count == 0


def test_remote_errors_are_sanitized_and_completion_is_idempotent(auth_client, db_session):
    auto = create_auto(db_session, target_worker="errors-worker")
    execution_id = executor.run_automation_job(auto.id)
    auth_client.post("/api/worker/poll?worker_name=errors-worker")
    response = auth_client.post(f"/api/worker/executions/{execution_id}/complete", json={
        "status": "FAILED", "logs": "https://my-private-credential@example.invalid/repo",
        "error_message": "password=private-value"})
    assert response.status_code == 200
    db_session.expire_all()
    ex = db_session.get(Execution, execution_id)
    assert "my-private-credential" not in ex.logs
    assert "private-value" not in ex.error_message
    response = auth_client.post(f"/api/worker/executions/{execution_id}/complete", json={"status": "SUCCESS"})
    assert response.json()["status"] == "FAILED"


def test_remote_cancellation_holds_reservation_until_acknowledged(auth_client, db_session):
    auto = create_auto(db_session, target_worker="cancel-worker")
    execution_id = executor.run_automation_job(auto.id)
    assert auth_client.post("/api/worker/poll?worker_name=cancel-worker").json()["job"]
    assert auth_client.post(f"/api/executions/{execution_id}/cancel").json()["success"]
    db_session.expire_all()
    assert db_session.get(Execution, execution_id).status == "RUNNING"
    assert executor.run_automation_job(auto.id) is None
    response = auth_client.post(f"/api/worker/executions/{execution_id}/heartbeat", json={})
    assert response.json()["cancel_requested"]
    response = auth_client.post(f"/api/worker/executions/{execution_id}/complete", json={"status": "SUCCESS"})
    assert response.json()["status"] == "CANCELLED"
    assert executor.run_automation_job(auto.id) is not None


def test_dependency_failure_is_not_reported_as_success(db_session, monkeypatch):
    auto = create_auto(db_session)
    (executor.get_automation_dir(auto.id) / "requirements.txt").write_text("missing-package\n", encoding="utf-8")
    monkeypatch.setattr(executor, "get_venv_python", lambda _: Path(sys.executable))
    monkeypatch.setattr(executor, "_run_preparation_command", lambda *args, **kwargs: CompletedProcess([], 1, "", "password=private-value"))
    success, message = executor.rebuild_automation_venv(auto.id)
    assert not success
    assert "private-value" not in message


def test_log_limits_are_bytes_and_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "MAX_LOG_SIZE_BYTES", 256)
    assert len(bounded_log("é" * 10000).encode("utf-8")) <= 256
    output = executor._OutputBuffer()
    output.read(io.BytesIO(b"x" * 2_000_000))
    assert len(output.data) <= 256 + 4096
    assert len(output.snapshot().encode("utf-8")) <= 256
    assert "TRUNCADO" in output.snapshot()


def test_log_stream_is_persisted_before_process_finishes(db_session, monkeypatch):
    monkeypatch.setattr(executor, "ensure_venv", lambda *args, **kwargs: Path(sys.executable))
    auto = create_auto(db_session)
    (executor.get_automation_dir(auto.id) / "main.py").write_text(
        "import time\nprint('LIVE_LOG', flush=True)\ntime.sleep(3)\n", encoding="utf-8")
    execution_id = executor.run_automation_job(auto.id)
    seen_live = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        db_session.expire_all()
        ex = db_session.get(Execution, execution_id)
        if "LIVE_LOG" in ex.logs and ex.status == "RUNNING":
            seen_live = True
            break
        time.sleep(0.05)
    assert seen_live
    assert wait_finished(db_session, execution_id).status == "SUCCESS"


def test_insecure_settings_and_legacy_sessions_are_rejected(monkeypatch):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SECRET_KEY="super-secret-key-change-in-production-1234567890")
    monkeypatch.delenv("SECRET_KEY")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    signature = hmac.new(settings.SECRET_KEY.encode(), b"testadmin", hashlib.sha256).hexdigest()
    assert verify_session(f"testadmin:{signature}") is None
    assert verify_session(sign_session("testadmin", timestamp=int(time.time()) + 3600)) is None


def test_scheduler_disabled_in_web_does_not_create_pending_jobs(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.services import scheduler as service
    stopped = BackgroundScheduler(timezone=settings.TIMEZONE)
    monkeypatch.setattr(service, "scheduler", stopped)
    auto = Automation(id=999901, name="Previsão", status="active", trigger_type="interval", interval_seconds=60)
    assert service.sync_automation_job(auto) is not None
    assert stopped.get_jobs() == []
