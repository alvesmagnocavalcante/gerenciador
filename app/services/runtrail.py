import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.progress import progress_values
from app.core.security import bounded_log, sanitize_output
from app.models.execution_step import ExecutionStep


RUNTRAIL_PREFIX = "RUNTRAIL_EVENT:"
VALID_EVENTS = {"started", "completed", "failed"}


@dataclass(frozen=True)
class RunTrailEvent:
    event: str
    event_id: str
    name: str
    timestamp: datetime
    attempt: int
    duration_seconds: float | None = None
    error: str | None = None


def extract_runtrail_events(text: str) -> tuple[str, list[RunTrailEvent]]:
    log_lines: list[str] = []
    events: list[RunTrailEvent] = []
    for line in (text or "").splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if not content.startswith(RUNTRAIL_PREFIX):
            log_lines.append(line)
            continue
        try:
            payload = json.loads(content[len(RUNTRAIL_PREFIX):])
            event = payload["event"]
            event_id = str(uuid.UUID(payload["event_id"]))
            name = sanitize_output(payload["name"]).strip()[:200]
            timestamp = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
            attempt = payload.get("attempt", 0)
            duration = payload.get("duration_seconds")
            error = payload.get("error")
            if event not in VALID_EVENTS or not name:
                raise ValueError("evento inválido")
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
                raise ValueError("tentativa inválida")
            if duration is not None:
                duration = float(duration)
                if not math.isfinite(duration) or duration < 0:
                    raise ValueError("duração inválida")
            if error is not None and not isinstance(error, str):
                raise ValueError("erro inválido")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OverflowError):
            # Mantém eventos inválidos visíveis para diagnóstico.
            log_lines.append(line)
            continue
        events.append(RunTrailEvent(
            event=event,
            event_id=event_id,
            name=name,
            timestamp=timestamp,
            attempt=attempt,
            duration_seconds=duration,
            error=sanitize_output(error or "").strip()[:2000] or None,
        ))
    return bounded_log("".join(log_lines)), events


def process_execution_output(db: Session, execution_id: int, text: str) -> dict:
    clean_logs, events = extract_runtrail_events(text)
    event_ids = {event.event_id for event in events}
    steps_by_event = {
        step.event_id: step
        for step in db.query(ExecutionStep).filter(
            ExecutionStep.execution_id == execution_id,
            ExecutionStep.event_id.in_(event_ids),
        ).all()
    } if event_ids else {}
    for event in events:
        step = steps_by_event.get(event.event_id)
        if step is None:
            started_at = event.timestamp
            if event.event != "started" and event.duration_seconds is not None:
                from datetime import timedelta
                started_at = event.timestamp - timedelta(seconds=event.duration_seconds)
            db.execute(
                sqlite_insert(ExecutionStep).values(
                    execution_id=execution_id,
                    event_id=event.event_id,
                    name=event.name,
                    status="RUNNING",
                    attempt=event.attempt,
                    started_at=started_at,
                ).on_conflict_do_nothing(index_elements=["execution_id", "event_id"])
            )
            step = db.query(ExecutionStep).filter(
                ExecutionStep.execution_id == execution_id,
                ExecutionStep.event_id == event.event_id,
            ).one()
            steps_by_event[event.event_id] = step
        if event.event == "started":
            step.name = event.name
            step.started_at = event.timestamp
        else:
            step.status = "SUCCESS" if event.event == "completed" else "FAILED"
            step.finished_at = event.timestamp
            step.duration_seconds = event.duration_seconds
            step.error_message = event.error
    db.flush()
    return progress_values(clean_logs)


def finalize_running_steps(db: Session, execution_id: int, execution_status: str,
                           finished_at: datetime) -> None:
    terminal_status = "CANCELLED" if execution_status == "CANCELLED" else "FAILED"
    message = (
        "Etapa interrompida pelo cancelamento da execução."
        if terminal_status == "CANCELLED"
        else "Etapa interrompida sem evento de conclusão."
    )
    for step in db.query(ExecutionStep).filter(
        ExecutionStep.execution_id == execution_id,
        ExecutionStep.status == "RUNNING",
    ).all():
        started = step.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        step.status = terminal_status
        step.finished_at = finished_at
        step.duration_seconds = round(max(0, (finished_at - started).total_seconds()), 6)
        step.error_message = message
