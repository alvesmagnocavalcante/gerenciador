import logging
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.events import (
    EVENT_JOB_EXECUTED,
    EVENT_JOB_ERROR,
    EVENT_JOB_MISSED,
)

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.models.automation import Automation

logger = logging.getLogger("pyautomation.scheduler")

jobstores = {
    "default": SQLAlchemyJobStore(engine=engine, tablename="apscheduler_jobs")
}

scheduler = BackgroundScheduler(jobstores=jobstores, timezone=settings.TIMEZONE)
_scheduler_lock = None

def _job_listener(event):
    """Atualiza a coluna next_run_at no banco de dados após a execução de um agendamento."""
    job_id = getattr(event, "job_id", None)
    if not job_id or not job_id.startswith("auto_job_"):
        return
    try:
        automation_id = int(job_id.replace("auto_job_", ""))
        job = scheduler.get_job(job_id)
        next_run = job.next_run_time if job else None
        with SessionLocal() as db:
            auto = db.query(Automation).filter(Automation.id == automation_id).first()
            if auto:
                auto.next_run_at = next_run.astimezone(timezone.utc) if next_run else None
                db.commit()
    except Exception as e:
        logger.debug(f"Aviso ao atualizar next_run_at no listener para {job_id}: {e}")

scheduler.add_listener(
    _job_listener,
    EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED,
)

def _lock_scheduler():
    global _scheduler_lock
    with SessionLocal() as db:
        db_path = Path(db.get_bind().url.database)
    handle = open(db_path.with_suffix(".scheduler.lock"), "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError("Já existe um agendador para este banco. Desative RUN_SCHEDULER_IN_WEB ao usar worker separado.") from exc
    _scheduler_lock = handle


def start_scheduler():
    if scheduler.running:
        return
    _lock_scheduler()
    try:
        scheduler.start(paused=True)
        sync_all_automations()
        scheduler.add_job(sync_all_automations, "interval", seconds=2,
                          id="reconcile_automations", replace_existing=True,
                          max_instances=1, coalesce=True)
        scheduler.resume()
    except Exception:
        shutdown_scheduler()
        raise


def shutdown_scheduler():
    global _scheduler_lock
    try:
        if scheduler.running:
            scheduler.shutdown(wait=True)
    finally:
        if _scheduler_lock:
            _scheduler_lock.close()
            _scheduler_lock = None


def get_job_id(automation_id: int) -> str:
    return f"auto_job_{automation_id}"

def sync_automation_job(automation: Automation) -> Optional[datetime]:
    """Cria, atualiza ou remove o job no APScheduler de acordo com a configuração da automação."""
    job_id = get_job_id(automation.id)

    # Remover job existente se desativado ou sem agendamento
    if automation.status in ["disabled", "paused"] or automation.trigger_type in ["none", None]:
        if scheduler.running and scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            logger.info(f"Job {job_id} removido do APScheduler.")
        return None

    trigger = None
    if automation.trigger_type == "interval" and automation.interval_seconds and automation.interval_seconds > 0:
        trigger = IntervalTrigger(seconds=automation.interval_seconds, timezone=settings.TIMEZONE)
    elif automation.trigger_type == "cron" and automation.cron_expression:
        try:
            trigger = CronTrigger.from_crontab(automation.cron_expression, timezone=settings.TIMEZONE)
        except Exception as e:
            logger.error(f"Expressão cron inválida para automação {automation.id}: {e}")
            return None
    elif automation.trigger_type == "daily":
        trigger = CronTrigger(hour=0, minute=0, timezone=settings.TIMEZONE)
    elif automation.trigger_type == "weekly":
        trigger = CronTrigger(day_of_week="mon", hour=0, minute=0, timezone=settings.TIMEZONE)

    if not trigger:
        return None

    # A web sem scheduler calcula a previsão; o processo proprietário reconcilia o banco.
    if not scheduler.running:
        next_run = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
        return next_run.astimezone(timezone.utc) if next_run else None
    existing = scheduler.get_job(job_id)
    if existing and str(existing.trigger) == str(trigger):
        if existing.name != automation.name:
            existing.modify(name=automation.name)
        return existing.next_run_time.astimezone(timezone.utc) if existing.next_run_time else None

    # Adicionar ou substituir job
    from app.services.executor import run_automation_job

    job = scheduler.add_job(
        func=run_automation_job,
        trigger=trigger,
        args=[automation.id, "scheduled"],
        id=job_id,
        name=automation.name,
        replace_existing=True,
        max_instances=1,  # Evita instâncias simultâneas do mesmo agendamento
    )

    next_run = job.next_run_time
    logger.info(f"Job {job_id} configurado. Próxima execução: {next_run}")
    return next_run.astimezone(timezone.utc) if next_run else None

def pause_automation_job(automation_id: int):
    job_id = get_job_id(automation_id)
    if scheduler.get_job(job_id):
        scheduler.pause_job(job_id)
        logger.info(f"Job {job_id} pausado.")

def resume_automation_job(automation_id: int):
    job_id = get_job_id(automation_id)
    if scheduler.get_job(job_id):
        scheduler.resume_job(job_id)
        logger.info(f"Job {job_id} retomado.")

def remove_automation_job(automation_id: int):
    job_id = get_job_id(automation_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info(f"Job {job_id} removido.")

def sync_all_automations():
    """Reconcilia alterações da web sem compartilhar schedulers entre processos."""
    from app.services.executor import cleanup_orphaned_executions
    cleanup_orphaned_executions()
    with SessionLocal() as db:
        automations = db.query(Automation).all()
        known = {get_job_id(auto.id) for auto in automations}
        for job in scheduler.get_jobs():
            if job.id.startswith("auto_job_") and job.id not in known:
                scheduler.remove_job(job.id)
        for auto in automations:
            try:
                next_run = sync_automation_job(auto)
                auto.next_run_at = next_run
            except (ValueError, TypeError) as exc:
                logger.error("Erro ao sincronizar automação %s: %s", auto.id, exc)
        db.commit()
