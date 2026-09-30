"""Подготовка, ожидание открытия и безопасная попытка бронирования."""

import logging
import time
from datetime import timedelta

import httpx

from . import rules
from .diagnostics import context, error_fields, event, stage
from .remarked import RateLimited, reserve_payload, retry_after
from .storage import read_state, save_state


def _submission_state(config, slot):
    return {
        "status": "SUBMITTING", "date": config["booking"]["visit_date"],
        "guests": config["booking"]["guests"], "slot_id": slot["slot_id"],
        "time": slot["time"], "room": slot["room"],
        "deposit_rub": rules.deposit(config["booking"]),
        "request_id": time.time_ns() // 1_000_000, "updated_at": rules.now().isoformat(),
    }


def submit(api, config, slot, state_path):
    state = _submission_state(config, slot)
    # Запись до HTTP-запроса не даёт повторить заявку после аварийного завершения.
    with context(request_id=state["request_id"]):
        with stage("persist_before_submission"):
            save_state(state_path, state)
        with stage("submit"):
            try:
                result = api.create_reserve(reserve_payload(config, slot), state["request_id"])
                state.update(result)
            except (httpx.HTTPError, ValueError, TypeError, RateLimited) as error:
                # Ответ мог потеряться после создания заявки; повтор запрещён.
                state.update(status="UNKNOWN", error=type(error).__name__)
                event("submission_uncertain", level=logging.ERROR, retry_allowed=False,
                      **error_fields(error))
            event("submission_result", status=state["status"], retry_allowed=state["status"] == "SLOT_TAKEN")
        state["updated_at"] = rules.now().isoformat()
        with stage("persist_after_submission"):
            save_state(state_path, state)
    return state


def wait_until(target, stop):
    event("wait_started", target=target.isoformat())
    while not stop.is_set():
        remaining = (target - rules.now()).total_seconds()
        if remaining <= 0:
            return True
        stop.wait(min(remaining, 30))
    return False


def prepare_request(action, stop, deadline):
    """До трёх попыток только для безопасных запросов подготовки."""
    for attempt in range(1, 4):
        if stop.is_set() or rules.now() >= deadline:
            return False
        try:
            with stage(action.__name__, preparation_attempt=attempt):
                action()
            return True
        except (httpx.HTTPError, RateLimited) as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            if status is not None and status not in (408, 429) and not 500 <= status < 600:
                raise
            if attempt == 3:
                event("preparation_retries_exhausted", level=logging.ERROR,
                      operation=action.__name__, attempts=attempt, **error_fields(error))
                raise
            delay = 0.25 * 2 ** (attempt - 1)
            if isinstance(error, RateLimited):
                delay = error.seconds
            elif status == 429:
                delay = retry_after(error.response.headers.get("Retry-After"))
            delay = min(delay, max(0, (deadline - rules.now()).total_seconds()))
            event("preparation_retry", level=logging.WARNING, operation=action.__name__,
                  attempt=attempt, wait_seconds=delay, **error_fields(error))
            stop.wait(delay)
    return False


def _prepare(api, config, release, stop, deadline):
    preparation = release - timedelta(seconds=config["release"]["prepare_seconds"])
    if not wait_until(preparation, stop):
        return False
    if not prepare_request(api.verify_widget, stop, deadline):
        return False
    # Запас на задержки авторизации; в замерах соединение переживало паузу 15 секунд.
    if not wait_until(release - timedelta(seconds=15), stop):
        return False
    if not prepare_request(api.authenticate, stop, deadline):
        return False
    return wait_until(release, stop)


def _previous_result(path):
    state = read_state(path)
    if state is not None and state["status"] != "SLOT_TAKEN":
        event("repeat_blocked", level=logging.WARNING, status=state["status"], state_file=str(path),
              request_id=state.get("request_id"))
        return state
    return None


def _save_result(path, config, status, **details):
    state = {"status": status, "date": config["booking"]["visit_date"], **details}
    save_state(path, state)
    return state


def _can_poll(stop, deadline):
    return not stop.is_set() and rules.now() < deadline


def _attempt_best_slot(api, config, path, slots, live, stop, deadline):
    if not slots or not _can_poll(stop, deadline):
        if slots:
            event("slot_attempt_skipped", reason="cancelled" if stop.is_set() else "deadline")
        return None
    slot = slots[0]
    event("slot_selected", slot_id=slot["slot_id"], visit_time=slot["time"], room=slot["room"],
          guests=config["booking"]["guests"], deposit_rub=rules.deposit(config["booking"]))
    if not live:
        return _save_result(path, config, "DRY_RUN_MATCH", **slot)
    return submit(api, config, slot, path)


def _poll_slots(api, config, path, live, stop, deadline):
    excluded = set()
    successful_reads = 0
    last_read_succeeded = False
    attempt = 0
    event("poll_started", deadline=deadline.isoformat(), interval_ms=config["release"]["poll_interval_ms"])
    while _can_poll(stop, deadline):
        attempt += 1
        delay = config["release"]["poll_interval_ms"] / 1000
        started = time.monotonic()
        with context(poll_attempt=attempt):
            if attempt == 1:
                event("first_slot_request", opening_lag_ms=round(
                    (rules.now() - rules.release_at(config)).total_seconds() * 1000, 1))
            try:
                slots = rules.select_slots(api.slots(config["booking"]), config["booking"], excluded)
                successful_reads += 1
                last_read_succeeded = True
                state = _attempt_best_slot(api, config, path, slots, live, stop, deadline)
                if state is not None:
                    if state["status"] != "SLOT_TAKEN":
                        event("poll_finished", reason=state["status"], attempts=attempt,
                              successful_reads=successful_reads)
                        return state
                    excluded.add(state["slot_id"])
            except RateLimited as error:
                last_read_succeeded = False
                delay = error.seconds + (time.monotonic() - started)
                event("poll_rate_limited", level=logging.WARNING, retry_after_seconds=error.seconds)
            except httpx.HTTPError as error:
                last_read_succeeded = False
                delay = max(delay, 1.0)
                event("poll_read_failed", level=logging.WARNING, **error_fields(error))
            remaining = min(delay - (time.monotonic() - started), (deadline - rules.now()).total_seconds())
            event("poll_wait", wait_ms=round(max(0, remaining) * 1000, 1))
            stop.wait(max(0, remaining))
    event("poll_finished", reason="cancelled" if stop.is_set() else "deadline",
          attempts=attempt, successful_reads=successful_reads,
          last_read_succeeded=last_read_succeeded)
    if stop.is_set():
        return None
    return _save_result(path, config, "NO_SLOTS" if last_read_succeeded else "READ_FAILED")


def run(api, config, path, live, stop):
    with stage("read_previous_result"):
        previous = _previous_result(path)
    if previous is not None:
        return previous
    release = rules.release_at(config)
    deadline = release + timedelta(seconds=config["release"]["max_poll_seconds"])
    event("booking_scheduled", opening=release.isoformat(), deadline=deadline.isoformat(),
          mode="live" if live else "dry-run", **{
              key: config["booking"][key] for key in (
                  "guests", "time_from", "time_to", "preferred_time", "allowed_rooms",
                  "allow_common_table", "allow_chefs_counter", "max_deposit_rub",
              )
          })
    if rules.now() >= deadline:
        event("booking_expired", level=logging.WARNING)
        return _save_result(path, config, "EXPIRED")
    with stage("prepare"):
        if not _prepare(api, config, release, stop, deadline):
            if stop.is_set():
                event("booking_cancelled")
                return None
            event("preparation_deadline_reached", level=logging.ERROR)
            return _save_result(path, config, "READ_FAILED")
    with stage("poll"):
        return _poll_slots(api, config, path, live, stop, deadline)
