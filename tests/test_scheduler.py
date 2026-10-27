from datetime import datetime
from app.models.automation import Automation
from app.services.scheduler import (
    scheduler,
    sync_automation_job,
    pause_automation_job,
    resume_automation_job,
    remove_automation_job,
    get_job_id,
)

def test_scheduler_sync_interval(db_session):
    if not scheduler.running:
        scheduler.start(paused=True)

    auto = Automation(
        id=991,
        name="Job Intervalo",
        source_type="local",
        status="active",
        trigger_type="interval",
        interval_seconds=60,
    )
    
    next_run = sync_automation_job(auto)
    assert next_run is not None

    job = scheduler.get_job(get_job_id(auto.id))
    assert job is not None
    assert job.name == "Job Intervalo"

    # Pausar
    pause_automation_job(auto.id)
    # Retomar
    resume_automation_job(auto.id)
    # Remover
    remove_automation_job(auto.id)
    assert scheduler.get_job(get_job_id(auto.id)) is None

def test_scheduler_sync_cron(db_session):
    if not scheduler.running:
        scheduler.start(paused=True)

    auto = Automation(
        id=992,
        name="Job Cron",
        source_type="local",
        status="active",
        trigger_type="cron",
        cron_expression="*/30 * * * *",
    )

    next_run = sync_automation_job(auto)
    assert next_run is not None

    job = scheduler.get_job(get_job_id(auto.id))
    assert job is not None

    # Remover
    remove_automation_job(auto.id)
    assert scheduler.get_job(get_job_id(auto.id)) is None
