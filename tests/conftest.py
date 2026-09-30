import errno
import hashlib
import json
import logging
import os
import stat
import sys
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src import config as configuration
from src import cli, remarked, rules, service


@pytest.fixture(autouse=True)
def clear_telegram_environment(monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        # Регистрируем откат и для значений, которые позже загрузит dotenv.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)


@pytest.fixture(autouse=True)
def guest_environment(monkeypatch):
    guest = {"first_name": "Иван", "last_name": "Иванов", "phone": "+79991234567",
             "email": "ivan@example.com", "telegram": "@username"}
    for key, value in guest.items():
        monkeypatch.setenv(f"GUEST_{key.upper()}", value)
    return guest


@pytest.fixture
def log_events(caplog):
    caplog.set_level(logging.INFO, logger="birch")

    def events(name=None):
        records = [json.loads(record.getMessage()) for record in caplog.records if record.name == "birch"]
        return [record for record in records if name is None or record["event"] == name]

    return events


@pytest.fixture
def runtime_signals(monkeypatch):
    handlers = {}
    monkeypatch.setattr(cli.signal, "signal", lambda number, handler: handlers.update({number: handler}))
    monkeypatch.setattr(cli.os, "umask", lambda mask: None)
    return handlers


@pytest.fixture
def project_root():
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def config_path(tmp_path):
    # Настройки тестов не зависят от рабочего config.toml и личного .env.
    path = tmp_path / "config.toml"
    path.write_text('''\
[booking]
visit_date = "auto"
guests = 2
time_from = "18:00"
time_to = "21:00"
preferred_time = "19:00"
allowed_rooms = ["Основной зал", "Открытая кухня"]
allow_common_table = false
allow_chefs_counter = false
max_deposit_rub = 19000

[release]
timezone = "Europe/Moscow"
opens_at = "14:00:00"
prepare_seconds = 120
poll_interval_ms = 50
max_poll_seconds = 60

[consents]
privacy = false
restaurant_rules = false
''', encoding="utf-8")
    return path


@pytest.fixture
def opening():
    return datetime(2026, 9, 29, 14, tzinfo=rules.MOSCOW)


@pytest.fixture
def booking_config(config_path, opening, monkeypatch):
    with monkeypatch.context() as context:
        context.setattr(rules, "now", lambda: opening)
        return configuration.load_config(config_path)


@pytest.fixture
def make_slots():
    def make(*times):
        return {"3075230942": {"slots": [
            {"id": index + 1, "start_datetime": f"2026-10-29 {time}:00",
             "is_common": 0, "tables": ["296465501247312"]}
            for index, time in enumerate(times)
        ]}}

    return make


@pytest.fixture
def clock(opening, monkeypatch):
    clock = SimpleNamespace(at=opening, elapsed=0.0, stopped=False)
    clock.is_set = lambda: clock.stopped

    def advance(seconds):
        clock.at += timedelta(seconds=seconds)
        clock.elapsed += seconds

    clock.wait = advance
    monkeypatch.setattr(rules, "now", lambda: clock.at)
    monkeypatch.setattr(service.time, "monotonic", lambda: clock.elapsed)
    return clock


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "state.json"


@pytest.fixture
def make_api_handler(make_slots):
    def make(submissions=None, groups=None):
        calls = []
        responses = iter(submissions if submissions is not None else [
            {"status": "success", "form_url": "https://payments.example/order"},
        ])
        available_slots = groups if groups is not None else make_slots("19:00", "20:00")

        def respond(request):
            payload = json.loads(request.content)
            calls.append(payload)
            method = payload["method"]
            if method == "GetToken":
                return httpx.Response(200, json={"token": "test-token"})
            if method == "GetBookingsSlots":
                return httpx.Response(200, json={"status": "success", "slots": available_slots})
            if method == "CreateReserveAfterPayment":
                response = next(responses)
                if isinstance(response, Exception):
                    raise response
                return httpx.Response(200, json=response)
            pytest.fail(f"Unexpected method: {method}")

        return respond, calls

    return make


@pytest.fixture
def run_service(booking_config, clock, state_path, monkeypatch):
    with ExitStack() as clients:
        def run(handler, live=True):
            client = clients.enter_context(httpx.Client(transport=httpx.MockTransport(handler)))
            api = remarked.RemarkedClient(client)
            monkeypatch.setattr(api, "verify_widget", lambda: None)
            state = service.run(api, booking_config, state_path, live, clock)
            return state, api

        yield run


@pytest.fixture
def write_config(booking_config, tmp_path, monkeypatch):
    def write(changes=None, live=False):
        config = deepcopy(booking_config)
        if live:
            config["guest"].update(email="guest@test.invalid", telegram="@test_guest")
            config["consents"].update(privacy=True, restaurant_rules=True)
        for section, values in (changes or {}).items():
            config[section].update(values)
        for key, value in config.pop("guest").items():
            monkeypatch.setenv(f"GUEST_{key.upper()}", value)
        text = "\n\n".join(
            f"[{section}]\n" + "\n".join(
                f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in values.items()
            ) for section, values in config.items()
        )
        path = tmp_path / "config.toml"
        path.write_text(text)
        return path

    return write


@pytest.fixture
def write_env(tmp_path, monkeypatch):
    def write(values):
        path = tmp_path / ".env"
        path.write_text("\n".join(
            f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in values.items()
        ) + "\n")
        for name in values:
            monkeypatch.delenv(name, raising=False)
        return path

    return write


@pytest.fixture
def mock_http(monkeypatch):
    original_client = httpx.Client

    def install(handler):
        def client(*args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            return original_client(*args, **kwargs)

        def post(url, **kwargs):
            with client() as connection:
                return connection.post(url, **kwargs)

        monkeypatch.setattr(httpx, "Client", client)
        monkeypatch.setattr(httpx, "post", post)

    return install


@pytest.fixture
def with_widget(monkeypatch):
    sources = {url: f"fixture-{index}".encode() for index, url in enumerate(remarked.ASSETS)}
    monkeypatch.setattr(remarked, "ASSETS", {
        url: hashlib.sha256(source).hexdigest() for url, source in sources.items()
    })

    def wrap(handler):
        def respond(request):
            url = str(request.url)
            if url == remarked.SITE:
                scripts = ''.join(f'<script src="{url}"></script>' for url in sources)
                return httpx.Response(200, text=scripts)
            if url in sources:
                return httpx.Response(200, content=sources[url])
            return handler(request)

        return respond

    return wrap


@pytest.fixture
def run_cli(clock, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "configure_runtime", lambda: clock)

    def run(config_path, command="run", live=False):
        argv = ["bot.py", "--config", str(config_path), "--state-dir", str(tmp_path), command]
        if live:
            argv.append("--live")
        monkeypatch.setattr(sys, "argv", argv)
        return cli.main()

    return run


@pytest.fixture
def telegram_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1234")


@pytest.fixture
def payment_state():
    return {
        "status": "AWAITING_PAYMENT", "date": "2026-10-29", "time": "19:00",
        "guests": 2, "room": "Основной зал", "deposit_rub": 19000,
        "payment_url": "https://payments.example/order",
    }


@pytest.fixture
def fail_state_io(state_path, monkeypatch):
    def install(stage, occurrence=1):
        temporary = state_path.with_suffix(".tmp")
        if stage in ("write", "replace"):
            target, name = Path, "open" if stage == "write" else "replace"
            matches = lambda path, *args, **kwargs: path == temporary
        else:
            target, name = os, "fsync"
            directory = stage == "directory_fsync"
            matches = lambda fd: stat.S_ISDIR(os.fstat(fd).st_mode) == directory
        original = getattr(target, name)
        count = 0

        def fail(*args, **kwargs):
            nonlocal count
            if matches(*args, **kwargs):
                count += 1
                if count == occurrence:
                    raise OSError(errno.ENOSPC, "simulated state write failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(target, name, fail)

    return install
