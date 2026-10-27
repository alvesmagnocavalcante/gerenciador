from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.core.database import get_db
from app.models.automation import Automation
from app.models.execution import Execution
from app.services.executor import run_automation_job, cancel_execution

router = APIRouter(prefix="/api", tags=["api"])

class AutomationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: Optional[str]
    source_type: str
    status: str
    trigger_type: str
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

@router.get("/automations")
def list_automations(db: Session = Depends(get_db)):
    automations = db.query(Automation).all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "source_type": a.source_type,
            "status": a.status,
            "trigger_type": a.trigger_type,
            "next_run_at": a.next_run_at.isoformat() if a.next_run_at else None,
        }
        for a in automations
    ]

@router.post("/automations/{automation_id}/run")
def trigger_automation(automation_id: int, db: Session = Depends(get_db)):
    auto = db.query(Automation).filter(Automation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automação não encontrada")

    execution_id = run_automation_job(auto.id, trigger_type="manual")
    if not execution_id:
        raise HTTPException(status_code=400, detail="Não foi possível iniciar a automação (já em execução ou desativada)")

    return {"message": "Automação iniciada", "execution_id": execution_id}

@router.get("/executions/{execution_id}")
def get_execution(execution_id: int, db: Session = Depends(get_db)):
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
def cancel_job(execution_id: int, db: Session = Depends(get_db)):
    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execução não encontrada")

    cancelled = cancel_execution(execution_id)
    return {"message": "Comando de cancelamento enviado", "success": cancelled}
