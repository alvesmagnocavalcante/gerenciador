import logging
from datetime import datetime, timezone
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.models.automation import Automation

logger = logging.getLogger("pyautomation.scheduler")

jobstores = {
    "default": SQLAlchemyJobStore(engine=engine, tablename="apscheduler_jobs")
}

scheduler = BackgroundScheduler(jobstores=jobstores, timezone=settings.TIMEZONE)

def _job_listener(event):
    """Atualiza a coluna next_run_at no banco de dados quando um agendamento é disparado ou alterado."""
    pass

def start_scheduler():
    """Inicia o agendador se ainda não estiver em execução."""
    if not scheduler.running:
        logger.info("Iniciando motor APScheduler...")
        scheduler.start()
        # Sincronizar automações ativas com o banco
        sync_all_automations()

def shutdown_scheduler():
    """Finaliza o agendador graciosamente."""
    if scheduler.running:
        logger.info("Encerrando motor APScheduler...")
        scheduler.shutdown(wait=False)

def get_job_id(automation_id: int) -> str:
    return f"auto_job_{automation_id}"

def sync_automation_job(automation: Automation) -> Optional[datetime]:
    """Cria, atualiza ou remove o job no APScheduler de acordo com a configuração da automação."""
    job_id = get_job_id(automation.id)

    # Remover job existente se desativado ou sem agendamento
    if automation.status in ["disabled", "paused"] or automation.trigger_type in ["none", None]:
        if scheduler.get_job(job_id):
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
    return next_run

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
    """Garante que todas as automações ativas com agendamento estejam no APScheduler."""
    with SessionLocal() as db:
        automations = db.query(Automation).filter(Automation.status == "active").all()
        for auto in automations:
            try:
                next_run = sync_automation_job(auto)
                if next_run:
                    auto.next_run_at = next_run
            except Exception as e:
                logger.error(f"Erro ao sincronizar job para automação {auto.id}: {e}")
        db.commit()
