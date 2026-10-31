import json
import sys
import time
from pathlib import Path

from app.core.progress import PROGRESS_PREFIX, extract_progress
from app.models.automation import Automation
from app.models.execution import Execution
from app.services import executor


def wait_finished(db, execution_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db.expire_all()
        execution = db.get(Execution, execution_id)
        if execution.status not in ["PENDING", "RUNNING"]:
            return execution
        time.sleep(0.05)
    raise AssertionError("Execução não terminou.")


def test_extract_progress_removes_control_lines_and_preserves_invalid_events():
    text = (
        "Início\n"
        + PROGRESS_PREFIX + json.dumps({"percent": 25, "message": "Baixando"}) + "\n"
        + PROGRESS_PREFIX + "inválido\n"
        + PROGRESS_PREFIX + json.dumps({"percent": 80, "message": "Gerando relatório"}) + "\n"
        + "Fim\n"
    )
    update = extract_progress(text)
    assert update.percent == 80
    assert update.message == "Gerando relatório"
    assert "Baixando" not in update.logs
    assert PROGRESS_PREFIX + "inválido" in update.logs
    assert "Início" in update.logs and "Fim" in update.logs


def test_extract_progress_validates_and_sanitizes_payload():
    invalid = extract_progress(PROGRESS_PREFIX + json.dumps({"percent": 101, "message": "fora"}))
    assert invalid.percent is None
    assert PROGRESS_PREFIX in invalid.logs
    fractional = extract_progress(PROGRESS_PREFIX + json.dumps({"percent": 42.5}))
    assert fractional.percent is None
    assert PROGRESS_PREFIX in fractional.logs
    valid = extract_progress(PROGRESS_PREFIX + json.dumps({
        "percent": 50,
        "message": "password=segredo",
    }))
    assert valid.percent == 50
    assert "segredo" not in valid.message


def test_local_progress_is_persisted_and_rendered(auth_client, db_session, monkeypatch):
    monkeypatch.setattr(executor, "ensure_venv", lambda *args, **kwargs: Path(sys.executable))
    auto = Automation(name="Progresso local", entrypoint="main.py", timeout_seconds=10)
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)
    auto_dir = executor.get_automation_dir(auto.id)
    auto_dir.joinpath("main.py").write_text(
        "import json, time\n"
        "print('PYAUTO_PROGRESS:' + json.dumps({'percent': 35, 'message': 'Processando'}), flush=True)\n"
        "print('LOG_VISIVEL', flush=True)\n"
        "time.sleep(1.3)\n"
        "print('PYAUTO_PROGRESS:' + json.dumps({'percent': 90, 'message': 'Finalizando'}), flush=True)\n",
        encoding="utf-8",
    )
    execution_id = executor.run_automation_job(auto.id)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        db_session.expire_all()
        execution = db_session.get(Execution, execution_id)
        if execution.progress_percent == 35:
            break
        time.sleep(0.05)
    assert execution.progress_percent == 35
    live = auth_client.get(f"/executions/{execution_id}/logs")
    assert live.status_code == 200
    assert "35%" in live.text
    assert "Processando" in live.text

    execution = wait_finished(db_session, execution_id)
    assert execution.status == "SUCCESS"
    assert execution.progress_percent == 100
    assert execution.progress_message == "Finalizando"
    assert PROGRESS_PREFIX not in execution.logs
    assert "LOG_VISIVEL" in execution.logs


def test_remote_progress_heartbeat_and_api(auth_client, db_session):
    auto = Automation(name="Progresso remoto", target_worker="progress-worker")
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)
    execution_id = executor.run_automation_job(auto.id)
    assert auth_client.post("/api/worker/poll?worker_name=progress-worker").json()["job"]
    logs = "remoto\n" + PROGRESS_PREFIX + json.dumps({"percent": 60, "message": "Enviando"}) + "\n"
    response = auth_client.post(
        f"/api/worker/executions/{execution_id}/heartbeat",
        json={"logs": logs},
    )
    assert response.status_code == 200
    db_session.expire_all()
    execution = db_session.get(Execution, execution_id)
    assert execution.progress_percent == 60
    assert execution.progress_message == "Enviando"
    assert PROGRESS_PREFIX not in execution.logs

    api_data = auth_client.get(f"/api/executions/{execution_id}").json()
    assert api_data["progress_percent"] == 60
    assert api_data["progress_message"] == "Enviando"

    response = auth_client.post(
        f"/api/worker/executions/{execution_id}/complete",
        json={"status": "SUCCESS", "exit_code": 0, "logs": logs},
    )
    assert response.json()["status"] == "SUCCESS"
    db_session.expire_all()
    assert db_session.get(Execution, execution_id).progress_percent == 100
