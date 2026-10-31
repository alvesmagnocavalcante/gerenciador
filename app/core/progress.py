import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.core.security import bounded_log, sanitize_output


PROGRESS_PREFIX = "PYAUTO_PROGRESS:"


@dataclass(frozen=True)
class ProgressUpdate:
    logs: str
    percent: Optional[int] = None
    message: Optional[str] = None
    updated_at: Optional[datetime] = None


def extract_progress(text: str) -> ProgressUpdate:
    """Extrai o último evento válido e remove eventos de controle dos logs."""
    log_lines: list[str] = []
    percent: Optional[int] = None
    message: Optional[str] = None
    found = False

    for line in (text or "").splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if not content.startswith(PROGRESS_PREFIX):
            log_lines.append(line)
            continue
        try:
            event = json.loads(content[len(PROGRESS_PREFIX):])
            raw_percent = event.get("percent")
            raw_message = event.get("message", "")
            if isinstance(raw_percent, bool) or not isinstance(raw_percent, (int, float)):
                raise ValueError("percent deve ser numérico")
            if not float(raw_percent).is_integer():
                raise ValueError("percent deve ser inteiro")
            numeric_percent = int(raw_percent)
            if not 0 <= numeric_percent <= 100:
                raise ValueError("percent deve estar entre 0 e 100")
            if not isinstance(raw_message, str):
                raise ValueError("message deve ser texto")
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            # Preserva eventos inválidos no console para facilitar o diagnóstico.
            log_lines.append(line)
            continue
        percent = numeric_percent
        message = sanitize_output(raw_message).strip()[:255] or None
        found = True

    return ProgressUpdate(
        logs=bounded_log("".join(log_lines)),
        percent=percent,
        message=message,
        updated_at=datetime.now(timezone.utc) if found else None,
    )


def progress_values(text: str) -> dict:
    update = extract_progress(text)
    values = {"logs": update.logs}
    if update.updated_at is not None:
        values.update(
            progress_percent=update.percent,
            progress_message=update.message,
            progress_updated_at=update.updated_at,
        )
    return values
