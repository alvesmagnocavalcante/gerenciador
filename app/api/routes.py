from datetime import datetime, timezone
from typing import List, Optional, Dict
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from sqlalchemy import asc, desc

from app.core.config import settings
from app.core.database import get_db
from app.core.security import sanitize_output, resolve_safe_path
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

    auto = execution.automation
    now = datetime.now(timezone.utc)
    execution.status = "RUNNING"
    execution.started_at = now
    execution.logs = f"[AGENTE REMOTO '{clean_worker}' INICIOU A EXECUÇÃO]\n"
    db.commit()

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
            "http_url": auto.http_url,
            "http_method": auto.http_method or "GET",
            "http_headers": auto.http_headers,
            "http_body": auto.http_body,
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

    cancel_requested = (execution.status == "CANCELLED")

    if payload.process_pid is not None:
        execution.process_pid = payload.process_pid
    if payload.retry_count is not None:
        execution.retry_count = payload.retry_count
    if payload.logs is not None and not cancel_requested:
        raw_logs = payload.logs
        if len(raw_logs.encode("utf-8")) > settings.MAX_LOG_SIZE_BYTES:
            raw_logs = raw_logs[:settings.MAX_LOG_SIZE_BYTES] + "\n[LOG TRUNCADO: LIMITE EXCEDIDO]"
        execution.logs = sanitize_output(raw_logs)

    db.commit()
    return {"ok": True, "cancel_requested": cancel_requested}

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

    now = datetime.now(timezone.utc)
    execution.finished_at = now
    if execution.started_at:
        started = execution.started_at if execution.started_at.tzinfo else execution.started_at.replace(tzinfo=timezone.utc)
        execution.duration_seconds = round((now - started).total_seconds(), 2)

    execution.exit_code = payload.exit_code
    if payload.retry_count is not None:
        execution.retry_count = payload.retry_count

    raw_logs = payload.logs or ""
    if len(raw_logs.encode("utf-8")) > settings.MAX_LOG_SIZE_BYTES:
        raw_logs = raw_logs[:settings.MAX_LOG_SIZE_BYTES] + "\n[LOG TRUNCADO: LIMITE EXCEDIDO]"
    sanitized = sanitize_output(raw_logs)

    if execution.status == "CANCELLED" or payload.status == "CANCELLED":
        cancel_msg = "[EXECUÇÃO CANCELADA PELO USUÁRIO]"
        if cancel_msg not in sanitized:
            sanitized = f"{sanitized}\n{cancel_msg}".strip()
        execution.status = "CANCELLED"
        execution.error_message = "Execução cancelada pelo usuário."
    else:
        valid_statuses = {"SUCCESS", "FAILED", "TIMEOUT", "CANCELLED"}
        execution.status = payload.status if payload.status in valid_statuses else "FAILED"
        execution.error_message = payload.error_message

    execution.logs = sanitized

    if payload.commit_sha and execution.automation:
        execution.automation.current_commit = payload.commit_sha[:40]

    db.commit()
    return {"ok": True, "status": execution.status}
