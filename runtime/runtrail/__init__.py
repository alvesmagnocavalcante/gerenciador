"""RunTrail: instrumentação mínima de etapas para automações Python."""

from __future__ import annotations

import functools
import inspect
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional, TypeVar, overload


__all__ = ["step"]
__version__ = "1.0.0"

_PREFIX = "RUNTRAIL_EVENT:"
_write_lock = threading.Lock()
F = TypeVar("F", bound=Callable)


def _emit(event: str, event_id: str, name: str, duration: Optional[float] = None,
          error: Optional[str] = None) -> None:
    payload = {
        "event": event,
        "event_id": event_id,
        "name": name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attempt": int(os.getenv("PYAUTOMATION_RETRY_COUNT", "0")),
    }
    if duration is not None:
        payload["duration_seconds"] = round(duration, 6)
    if error:
        payload["error"] = error
    line = _PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _write_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def _decorate(function: F, display_name: Optional[str]) -> F:
    name = (display_name or function.__name__.replace("_", " ")).strip()
    if not name:
        raise ValueError("A etapa precisa de um nome.")

    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def async_wrapper(*args, **kwargs):
            event_id = str(uuid.uuid4())
            started = time.perf_counter()
            _emit("started", event_id, name)
            try:
                result = await function(*args, **kwargs)
            except BaseException as exc:
                _emit("failed", event_id, name, time.perf_counter() - started,
                      f"{type(exc).__name__}: {exc}")
                raise
            _emit("completed", event_id, name, time.perf_counter() - started)
            return result
        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        event_id = str(uuid.uuid4())
        started = time.perf_counter()
        _emit("started", event_id, name)
        try:
            result = function(*args, **kwargs)
        except BaseException as exc:
            _emit("failed", event_id, name, time.perf_counter() - started,
                  f"{type(exc).__name__}: {exc}")
            raise
        _emit("completed", event_id, name, time.perf_counter() - started)
        return result
    return wrapper  # type: ignore[return-value]


@overload
def step(function: F) -> F: ...


@overload
def step(name: str) -> Callable[[F], F]: ...


@overload
def step() -> Callable[[F], F]: ...


def step(function_or_name=None):
    """Marca uma função síncrona ou assíncrona como etapa observável."""
    if callable(function_or_name):
        return _decorate(function_or_name, None)
    if function_or_name is not None and not isinstance(function_or_name, str):
        raise TypeError("Use @step, @step() ou @step('Nome da etapa').")

    def decorator(function: F) -> F:
        return _decorate(function, function_or_name)

    return decorator
