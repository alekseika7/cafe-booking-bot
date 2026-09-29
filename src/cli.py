"""Команды check/run/status и настройка процесса."""

import argparse
import json
import logging
import os
import signal
import threading
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx

from . import notifications, rules, service
from .config import load_config
from .diagnostics import context, error_fields, event, stage
from .remarked import SITE, RemarkedClient
from .storage import exclusive_lock

FAILED_STATUSES = {"UNKNOWN", "SUBMITTING", "REJECTED", "READ_FAILED"}


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="One Birch booking job: prepare, wait for release, submit once, hand off payment.",
    )
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--state-dir", type=Path, default=Path("data"))
    parser.add_argument("command", choices=("check", "run", "status"))
    parser.add_argument("--live", action="store_true", help="Разрешить отправку заявки, без оплаты")
    return parser.parse_args()


def configure_runtime():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    os.umask(0o077)
    stop = threading.Event()

    def request_stop(signum, frame):
        stop.set()
        event("stop_requested", signal=signal.Signals(signum).name)

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, request_stop)
    return stop


def check(api, config, stop):
    deadline = rules.now() + timedelta(seconds=config["release"]["max_poll_seconds"])
    for action in (api.verify_widget, api.authenticate):
        if not service.prepare_request(action, stop, deadline):
            return False
    release = rules.release_at(config)
    if rules.now() < release:
        print(f"Проверка пройдена. Запись откроется {release.isoformat()}")
        return True
    slots = rules.select_slots(api.slots(config["booking"]), config["booking"])
    print(json.dumps({"matching_slots": slots}, ensure_ascii=False, indent=2))
    return True


def report_result(state, path, live):
    if state is None:
        return 0
    event("booking_result", status=state["status"], state_file=str(path),
          request_id=state.get("request_id"))
    if live:
        with stage("notification"):
            notifications.notify(state)
    return 1 if state["status"] in FAILED_STATUSES else 0


def _execute_api_command(api, config, args, path, stop):
    if args.command == "check":
        return 0 if check(api, config, stop) else 1
    with stage("validate_notifications"):
        notifications.validate_settings()
    state = service.run(api, config, path, args.live, stop)
    return report_result(state, path, args.live)


def execute(args, stop):
    with stage("load_config"):
        config = load_config(
            args.config, live=args.live and args.command == "run", allow_past=args.command == "status",
        )
    with stage("prepare_state_directory"):
        args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    prefix = "live" if args.live else "dry-run"
    path = args.state_dir / f"{prefix}-{config['booking']['visit_date']}.json"
    if args.command == "status":
        print(path.read_text() if path.exists() else "Задача ещё не выполнялась")
        return 0
    with context(visit_date=config["booking"]["visit_date"]), exclusive_lock(args.state_dir), httpx.Client(
        timeout=httpx.Timeout(5, connect=2), follow_redirects=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=60),
        headers={"Origin": "https://birchrestaurants.com", "Referer": SITE},
    ) as client:
        return _execute_api_command(RemarkedClient(client), config, args, path, stop)


def main():
    args = parse_arguments()
    stop = configure_runtime()
    with context(run_id=uuid4().hex, command=args.command, mode="live" if args.live else "dry-run"):
        started = time.monotonic()
        event("run_started")
        try:
            exit_code = execute(args, stop)
        except Exception as error:
            # На границе CLI журналируем и неожиданные ошибки без текста с секретами.
            event("run_failed", level=logging.ERROR, **error_fields(error))
            exit_code = 1
        event("run_finished", exit_code=exit_code, cancelled=stop.is_set(),
              elapsed_ms=round((time.monotonic() - started) * 1000, 1))
        return exit_code
