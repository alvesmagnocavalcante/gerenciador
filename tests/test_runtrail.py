import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.models.automation import Automation
from app.models.execution import Execution
from app.models.execution_step import ExecutionStep
from app.services import executor
from app.services.runtrail import RUNTRAIL_PREFIX, extract_runtrail_events
from runtime.runtrail import step


def wait_finished(db, execution_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db.expire_all()
        execution = db.get(Execution, execution_id)
        if execution.status not in ["PENDING", "RUNNING"]:
            return execution
        time.sleep(0.05)
    raise AssertionError("Execução não terminou.")


def test_step_decorator_supports_sync_async_and_failures(capsys):
    @step
    def somar(a, b):
        return a + b

    @step("Etapa assíncrona")
    async def async_value():
        return 7

    @step("Etapa com falha")
    def fail():
        raise ValueError("inválido")

    assert somar(2, 3) == 5
    assert asyncio.run(async_value()) == 7
    try:
        fail()
    except ValueError:
        pass

    logs, events = extract_runtrail_events(capsys.readouterr().out)
    assert logs == ""
    assert [event.event for event in events] == [
        "started", "completed", "started", "completed", "started", "failed"
    ]
    assert events[-1].error == "ValueError: inválido"


def test_invalid_runtrail_event_remains_in_logs():
    text = RUNTRAIL_PREFIX + json.dumps({"event": "started"}) + "\n"
    logs, events = extract_runtrail_events(text)
    assert not events
    assert RUNTRAIL_PREFIX in logs


def test_local_runtrail_steps_are_persisted_and_rendered(auth_client, db_session, monkeypatch):
    monkeypatch.setattr(executor, "ensure_venv", lambda *args, **kwargs: Path(sys.executable))
    automation = Automation(name="RunTrail local", entrypoint="main.py", timeout_seconds=10)
    db_session.add(automation)
    db_session.commit()
    db_session.refresh(automation)
    executor.get_automation_dir(automation.id).joinpath("main.py").write_text(
        "from runtrail import step\n"
        "@step('Coletar dados')\n"
        "def coletar(): print('LOG_NORMAL', flush=True)\n"
        "@step('Validar dados')\n"
        "def validar(): raise ValueError('dado inválido')\n"
        "coletar()\n"
        "try:\n    validar()\nexcept ValueError:\n    pass\n",
        encoding="utf-8",
    )

    execution_id = executor.run_automation_job(automation.id)
    execution = wait_finished(db_session, execution_id)
    assert execution.status == "SUCCESS"
    steps = db_session.query(ExecutionStep).filter_by(execution_id=execution_id).order_by(ExecutionStep.id).all()
    assert [(item.name, item.status) for item in steps] == [
        ("Coletar dados", "SUCCESS"),
        ("Validar dados", "FAILED"),
    ]
    assert "dado inválido" in steps[1].error_message
    assert RUNTRAIL_PREFIX not in execution.logs
    assert "LOG_NORMAL" in execution.logs

    response = auth_client.get(f"/executions/{execution_id}/logs")
    assert response.status_code == 200
    assert "Trilha da execução" in response.text
    assert "Coletar dados" in response.text
    assert "Validar dados" in response.text

    api = auth_client.get(f"/api/executions/{execution_id}").json()
    assert [item["status"] for item in api["steps"]] == ["SUCCESS", "FAILED"]


def test_remote_heartbeat_persists_runtrail_steps(auth_client, db_session):
    automation = Automation(name="RunTrail remoto", target_worker="runtrail-worker")
    db_session.add(automation)
    db_session.commit()
    db_session.refresh(automation)
    execution_id = executor.run_automation_job(automation.id)
    job = auth_client.post("/api/worker/poll?worker_name=runtrail-worker").json()["job"]
    assert "def step" in job["runtrail_runtime_source"]

    event_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    events = [
        {"event": "started", "event_id": event_id, "name": "Consultar serviço", "timestamp": now, "attempt": 0},
        {"event": "completed", "event_id": event_id, "name": "Consultar serviço", "timestamp": now,
         "attempt": 0, "duration_seconds": 0.25},
    ]
    logs = "log remoto\n" + "\n".join(
        RUNTRAIL_PREFIX + json.dumps(item) for item in events
    ) + "\n"
    response = auth_client.post(
        f"/api/worker/executions/{execution_id}/heartbeat",
        json={"logs": logs},
    )
    assert response.status_code == 200
    db_session.expire_all()
    step_record = db_session.query(ExecutionStep).filter_by(execution_id=execution_id).one()
    assert step_record.name == "Consultar serviço"
    assert step_record.status == "SUCCESS"
    assert RUNTRAIL_PREFIX not in db_session.get(Execution, execution_id).logs
    complete = auth_client.post(
        f"/api/worker/executions/{execution_id}/complete",
        json={"status": "SUCCESS", "exit_code": 0, "logs": logs},
    )
    assert complete.status_code == 200
