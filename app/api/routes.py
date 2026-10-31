from datetime import datetime, timezone
from typing import List, Optional, Dict
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from sqlalchemy import asc, desc

from app.core.config import BASE_DIR, settings
from app.core.database import get_db
from app.core.security import sanitize_output, resolve_safe_path, bounded_log
from app.services.runtrail import finalize_running_steps, process_execution_output
from app.models.automation import Automation
from app.models.execution import Execution
from app.models.user import User
from app.services.executor import (
    run_automation_job,
    cancel_execution,
    get_automation_dir,
    parse_env_vars,
)
from app.web.auth import get_api_user

router = APIRouter(prefix="/api", tags=["api"])
RUNTRAIL_RUNTIME_SOURCE = (BASE_DIR / "runtime" / "runtrail" / "__init__.py").read_text(encoding="utf-8")

class AutomationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: Optional[str]
    source_type: str
    status: str
    trigger_type: str
    target_worker: str
    next_run_at: Optional[str]

class ExecutionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    automation_id: int
    trigger_type: str
    status: str
    started_at: Optional[str]
    duration_seconds: Optional[float]
    exit_code: Optional[int]

class WorkerHeartbeatPayload(BaseModel):
    logs: Optional[str] = None
    process_pid: Optional[int] = None
    retry_count: Optional[int] = None

class WorkerCompletePayload(BaseModel):
    status: str  # 'SUCCESS', 'FAILED', 'TIMEOUT', 'CANCELLED'
    exit_code: Optional[int] = None
    logs: Optional[str] = ""
    error_message: Optional[str] = None
    commit_sha: Optional[str] = None
    retry_count: Optional[int] = 0

@router.get("/automations")
def list_automations(
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_api_user),
):
    automations = db.query(Automation).all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "source_type": a.source_type,
            "status": a.status,
            "trigger_type": a.trigger_type,
            "target_worker": a.target_worker or "local",
            "next_run_at": a.next_run_at.isoformat() if a.next_run_at else None,
        }
        for a in automations
    ]

@router.post("/automations/{automation_id}/run")
def trigger_automation(
    automation_id: int,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_api_user),
):
    auto = db.query(Automation).filter(Automation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automação não encontrada")

    execution_id = run_automation_job(auto.id, trigger_type="manual")
    if not execution_id:
        raise HTTPException(status_code=400, detail="Não foi possível iniciar a automação (já em execução ou desativada)")

    return {"message": "Automação iniciada", "execution_id": execution_id}

@router.get("/executions/{execution_id}")
def get_execution(
    execution_id: int,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_api_user),
):
    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execução não encontrada")

    return {
        "id": execution.id,
        "automation_id": execution.automation_id,
        "trigger_type": execution.trigger_type,
        "status": execution.status,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
        "duration_seconds": execution.duration_seconds,
        "exit_code": execution.exit_code,
        "logs": execution.logs,
        "error_message": execution.error_message,
        "progress_percent": execution.progress_percent,
        "progress_message": execution.progress_message,
        "progress_updated_at": execution.progress_updated_at.isoformat() if execution.progress_updated_at else None,
        "steps": [
            {
                "id": step.id,
                "name": step.name,
                "status": step.status,
                "attempt": step.attempt,
                "started_at": step.started_at.isoformat(),
                "finished_at": step.finished_at.isoformat() if step.finished_at else None,
                "duration_seconds": step.duration_seconds,
                "error_message": step.error_message,
            }
            for step in execution.steps
        ],
    }

@router.post("/executions/{execution_id}/cancel")
def cancel_job(
    execution_id: int,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_api_user),
):
    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execução não encontrada")

    cancelled = cancel_execution(execution_id)
    return {"message": "Comando de cancelamento enviado", "success": cancelled}

# ----------------- ROTAS DO AGENTE / WORKER REMOTO (GO) -----------------

@router.post("/worker/poll")
def worker_poll_next_job(
    worker_name: str = Query(..., description="Identificador da máquina/worker (ex: pc-financeiro)"),
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_api_user),
):
    """Busca a próxima execução PENDING destinada especificamente a este worker remoto."""
    clean_worker = worker_name.strip()
    if not clean_worker or clean_worker.lower() == "local":
        raise HTTPException(status_code=400, detail="Nome do worker remoto inválido.")

    execution = (
        db.query(Execution)
        .join(Automation, Execution.automation_id == Automation.id)
        .filter(
            Execution.status == "PENDING",
            Automation.target_worker == clean_worker,
        )
        .order_by(asc(Execution.id))
        .first()
    )

    if not execution:
        return {"job": None}

    now = datetime.now(timezone.utc)
    claimed = db.query(Execution).filter(Execution.id == execution.id,
        Execution.status == "PENDING", Execution.cancel_requested.is_(False)).update(
            {"status": "RUNNING", "started_at": now, "heartbeat_at": now,
             "owner_id": f"remote:{clean_worker}",
             "logs": f"[AGENTE REMOTO '{clean_worker}' INICIOU A EXECUÇÃO]\n"}, synchronize_session=False)
    db.commit()
    if not claimed:
        return {"job": None}
    db.refresh(execution)
    auto = execution.automation

    # Se for automação local, enviar o conteúdo do script para que o agente Go não precise de cópia manual
    local_script_content = None
    if auto.source_type == "local":
        try:
            auto_dir = get_automation_dir(auto.id)
            script_path = resolve_safe_path(auto_dir, auto.entrypoint)
            if script_path.is_file():
                local_script_content = script_path.read_text(encoding="utf-8")
        except Exception:
            pass

    parsed_env = parse_env_vars(auto.env_vars) if auto.env_vars else {}

    return {
        "job": {
            "execution_id": execution.id,
            "automation_id": auto.id,
            "name": auto.name,
            "source_type": auto.source_type,
            "git_url": auto.git_url,
            "git_branch": auto.git_branch or "main",
            "git_token": auto.git_token,
            "entrypoint": auto.entrypoint,
            "arguments": auto.arguments or "",
            "local_script_content": local_script_content,
            "env_vars": parsed_env,
            "timeout_seconds": auto.timeout_seconds,
            "max_retries": auto.max_retries,
            "retry_delay_seconds": auto.retry_delay_seconds,
            "cleanup_mode": auto.cleanup_mode or "none",
            "max_log_size_bytes": settings.MAX_LOG_SIZE_BYTES,
            "http_url": auto.http_url,
            "http_method": auto.http_method or "GET",
            "http_headers": auto.http_headers,
            "http_body": auto.http_body,
            "runtrail_runtime_source": RUNTRAIL_RUNTIME_SOURCE,
        }
    }

@router.post("/worker/executions/{execution_id}/heartbeat")
def worker_execution_heartbeat(
    execution_id: int,
    payload: WorkerHeartbeatPayload,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_api_user),
):
    """Recebe logs parciais em tempo real do agente Go e informa se o usuário solicitou cancelamento."""
    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execução não encontrada")

    cancel_requested = execution.cancel_requested or execution.status not in ["PENDING", "RUNNING"]

    if execution.status not in ["PENDING", "RUNNING"]:
        return {"ok": True, "cancel_requested": True}

    values = {"heartbeat_at": datetime.now(timezone.utc)}
    if payload.process_pid is not None:
        values["process_pid"] = payload.process_pid
    if payload.retry_count is not None:
        if payload.retry_count > execution.retry_count:
            values.update(
                progress_percent=None,
                progress_message=None,
                progress_updated_at=None,
            )
        values["retry_count"] = payload.retry_count
    if payload.logs is not None and not cancel_requested:
        values.update(process_execution_output(db, execution.id, payload.logs))

    db.query(Execution).filter(Execution.id == execution_id,
        Execution.status.in_(["PENDING", "RUNNING"])).update(values, synchronize_session=False)
    db.commit()
    db.refresh(execution)
    return {"ok": True, "cancel_requested": execution.cancel_requested or execution.status not in ["PENDING", "RUNNING"]}

@router.post("/worker/executions/{execution_id}/complete")
def worker_execution_complete(
    execution_id: int,
    payload: WorkerCompletePayload,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_api_user),
):
    """Finaliza a execução reportada pelo agente Go remoto."""
    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execução não encontrada")

    if execution.status not in ["PENDING", "RUNNING"]:
        return {"ok": True, "status": execution.status}

    now = datetime.now(timezone.utc)
    status_value = "CANCELLED" if execution.cancel_requested or payload.status == "CANCELLED" else payload.status
    if status_value not in {"SUCCESS", "FAILED", "TIMEOUT", "CANCELLED"}:
        status_value = "FAILED"
    logs = payload.logs or ""
    error = payload.error_message
    if status_value == "CANCELLED":
        logs += "\n[EXECUÇÃO CANCELADA PELO USUÁRIO]"
        error = "Execução cancelada pelo usuário."
    values = {"status": status_value, "finished_at": now, "exit_code": payload.exit_code,
              "error_message": sanitize_output(error or "") or None,
              "heartbeat_at": now, "retry_count": payload.retry_count or 0}
    values.update(process_execution_output(db, execution.id, logs))
    finalize_running_steps(db, execution.id, status_value, now)
    if status_value == "SUCCESS" and (values.get("progress_percent") is not None or execution.progress_percent is not None):
        values["progress_percent"] = 100
        values["progress_message"] = values.get("progress_message") or execution.progress_message or "Concluído"
        values["progress_updated_at"] = now
    if execution.started_at:
        started = execution.started_at if execution.started_at.tzinfo else execution.started_at.replace(tzinfo=timezone.utc)
        values["duration_seconds"] = round((now - started).total_seconds(), 2)
    changed = db.query(Execution).filter(Execution.id == execution_id,
        Execution.status.in_(["PENDING", "RUNNING"]),
        Execution.cancel_requested == execution.cancel_requested).update(values, synchronize_session=False)
    if not changed:
        db.rollback()
        # Um cancelamento concorrente vence a conclusão enviada pelo agente.
        current = db.get(Execution, execution_id)
        if current and current.status in ["PENDING", "RUNNING"] and current.cancel_requested:
            values.update(status="CANCELLED", error_message="Execução cancelada pelo usuário.")
            values.update(process_execution_output(
                db, execution.id, logs + "\n[EXECUÇÃO CANCELADA PELO USUÁRIO]"
            ))
            finalize_running_steps(db, execution.id, "CANCELLED", now)
            db.query(Execution).filter(Execution.id == execution_id,
                Execution.status.in_(["PENDING", "RUNNING"]), Execution.cancel_requested.is_(True)).update(values, synchronize_session=False)
    if payload.commit_sha and execution.automation:
        execution.automation.current_commit = payload.commit_sha[:40]
    db.commit()
    db.expire_all()
    return {"ok": True, "status": db.get(Execution, execution_id).status}
