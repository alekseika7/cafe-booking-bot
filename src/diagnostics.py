"""Структурированный журнал с контекстом запуска, без содержимого запросов."""

import json
import logging
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

import httpx

LOG = logging.getLogger("birch")
CONTEXT = ContextVar("log_context", default={})


@contextmanager
def context(**fields):
    token = CONTEXT.set({**CONTEXT.get(), **fields})
    try:
        yield
    finally:
        CONTEXT.reset(token)


def event(name, *, level=logging.INFO, **fields):
    if LOG.isEnabledFor(level):
        record = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": logging.getLevelName(level),
            **CONTEXT.get(), "event": name, **fields,
        }
        LOG.log(level, json.dumps(record, ensure_ascii=False))


def error_fields(error):
    # Текст исключения, URL и локальные переменные могут содержать контакты/токены.
    fields = {"error_type": type(error).__name__}
    frames = traceback.extract_tb(error.__traceback__)
    fields["error_locations"] = [
        f"{Path(frame.filename).name}:{frame.name}:{frame.lineno}" for frame in frames
    ]
    if isinstance(error, OSError):
        fields["errno"] = error.errno
    if isinstance(error, httpx.HTTPStatusError):
        fields["http_status"] = error.response.status_code
    return fields


@contextmanager
def stage(name, **fields):
    with context(stage=name, **fields):
        started = time.monotonic()
        event("stage_started")
        try:
            yield
        except Exception as error:
            event("stage_failed", level=logging.ERROR,
                  elapsed_ms=round((time.monotonic() - started) * 1000, 1), **error_fields(error))
            raise
        else:
            event("stage_completed", elapsed_ms=round((time.monotonic() - started) * 1000, 1))
