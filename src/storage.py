"""Состояние заявки и блокировка единственного процесса."""

import fcntl
import json
import logging
import os
from contextlib import contextmanager

from .diagnostics import error_fields, event


@contextmanager
def exclusive_lock(directory):
    with (directory / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            event("lock_busy", level=logging.ERROR, state_dir=str(directory),
                  message="Другой экземпляр уже использует эту папку состояния")
            raise ValueError("Другой экземпляр уже использует эту папку состояния") from None
        event("lock_acquired", state_dir=str(directory))
        yield


def read_state(path):
    if not path.exists():
        return None
    state = json.loads(path.read_text())
    if not isinstance(state, dict) or not isinstance(state.get("status"), str):
        raise ValueError("Некорректный файл состояния; проверьте предыдущую заявку вручную")
    return state


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    details = {"state_file": str(path), "status": state["status"], "request_id": state.get("request_id")}
    event("state_save_started", **details)
    operation = "open_temporary"
    try:
        with temporary.open("w", encoding="utf-8") as file:
            operation = "set_permissions"
            os.chmod(temporary, 0o600)
            operation = "write"
            json.dump(state, file, ensure_ascii=False, indent=2)
            file.write("\n")
            operation = "flush"
            file.flush()
            operation = "file_fsync"
            os.fsync(file.fileno())
            operation = "close_temporary"
        operation = "replace"
        temporary.replace(path)
        operation = "open_directory"
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            operation = "directory_fsync"
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, TypeError, ValueError) as error:
        event("state_save_failed", level=logging.ERROR, operation=operation,
              **details, **error_fields(error))
        raise
    event("state_saved", **details)
