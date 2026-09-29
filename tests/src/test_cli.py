import fcntl
import json
import signal
import subprocess
import sys
from datetime import timedelta

import httpx
import pytest

from src import cli, diagnostics, remarked


@pytest.mark.parametrize("command", ["check", "run"])
@pytest.mark.parametrize("operation", ["verify_widget", "authenticate"])
def test_main_retries_temporary_preparation_errors(
    command, operation, write_config, run_cli, make_api_handler, with_widget, mock_http,
    clock, opening, log_events,
):
    clock.at = opening - timedelta(seconds=20)
    handler, calls = make_api_handler()
    base = with_widget(handler)
    attempts = []

    def respond(request):
        matches = (str(request.url) == remarked.SITE if operation == "verify_widget" else
                   str(request.url) == remarked.API and json.loads(request.content)["method"] == "GetToken")
        if matches:
            attempts.append(clock.at)
            if len(attempts) < 3:
                return httpx.Response(503)
        return base(request)

    mock_http(respond)

    assert run_cli(write_config(), command=command) == 0

    assert len(attempts) == 3
    assert [entry["wait_seconds"] for entry in log_events("preparation_retry")] == [0.25, 0.5]
    assert all(call["method"] != "CreateReserveAfterPayment" for call in calls)


def test_main_loads_telegram_settings_from_dotenv(
    write_config, write_env, run_cli, make_api_handler, with_widget, mock_http, log_events,
):
    path = write_config(live=True)
    write_env({"TELEGRAM_BOT_TOKEN": "test-dotenv-token", "TELEGRAM_CHAT_ID": "1234"})
    handler, _ = make_api_handler()
    messages = []

    def respond(request):
        if request.url.host == "api.telegram.org":
            messages.append(json.loads(request.content))
            assert request.url.path == "/bottest-dotenv-token/sendMessage"
            return httpx.Response(200, json={"ok": True})
        return handler(request)

    mock_http(with_widget(respond))

    assert run_cli(path, live=True) == 0

    assert len(messages) == 1
    assert messages[0]["chat_id"] == "1234"
    assert "test-dotenv-token" not in str(log_events())


def test_main_auto_date_waits_for_opening_ignores_old_slots_and_blocks_repeat(
    write_config, run_cli, make_api_handler, make_slots, with_widget, mock_http,
    clock, opening, tmp_path,
):
    clock.at = opening - timedelta(seconds=20)
    handler, calls = make_api_handler()
    observed = []
    old_slots = make_slots("19:00")
    old_slots["3075230942"]["slots"][0]["start_datetime"] = "2026-10-28 19:00:00"

    def respond(request):
        response = handler(request)
        method = json.loads(request.content)["method"]
        observed.append((method, clock.at))
        if method == "GetBookingsSlots" and sum(name == method for name, _ in observed) == 1:
            return httpx.Response(200, json={"status": "success", "slots": old_slots})
        return response

    mock_http(with_widget(respond))
    path = write_config({"booking": {"visit_date": "auto"}}, live=True)

    assert run_cli(path, live=True) == 0
    assert run_cli(path, live=True) == 0

    assert [call["method"] for call in calls] == [
        "GetToken", "GetBookingsSlots", "GetBookingsSlots", "CreateReserveAfterPayment",
    ]
    assert all(at >= opening for method, at in observed if method != "GetToken")
    assert calls[-1]["reserve"]["date"] == "2026-10-29"
    state = json.loads((tmp_path / "live-2026-10-29.json").read_text())
    assert state["status"] == "AWAITING_PAYMENT"


def test_main_auto_date_rejects_closed_monday_without_choosing_older_date(
    write_config, run_cli, mock_http, clock, opening, log_events, tmp_path,
):
    path = write_config({"booking": {"visit_date": "auto"}}, live=True)
    clock.at = opening - timedelta(days=3)
    mock_http(lambda request: pytest.fail("No new date must not make HTTP requests"))

    assert run_cli(path, live=True) == 1

    assert "Сегодня новая дата не открывается" in log_events("config_invalid")[0]["message"]
    assert not list(tmp_path.glob("live-*.json"))


def test_main_auto_date_does_not_roll_to_next_day_after_missed_release(
    write_config, run_cli, mock_http, clock, opening, tmp_path,
):
    path = write_config({"booking": {"visit_date": "auto"}})
    clock.at = opening + timedelta(minutes=2)
    mock_http(lambda request: pytest.fail("Expired run must not make HTTP requests"))

    assert run_cli(path) == 0

    state = json.loads((tmp_path / "dry-run-2026-10-29.json").read_text())
    assert state == {"status": "EXPIRED", "date": "2026-10-29"}


def test_main_rejects_second_process_for_same_job(project_root, write_config, tmp_path):
    # Дата в будущем позволяет тесту оставаться воспроизводимым после визита из примера.
    path = write_config({"booking": {"visit_date": "2099-10-29"}})
    with (tmp_path / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [sys.executable, str(project_root / "bot.py"),
             "--config", str(path), "--state-dir", str(tmp_path), "run"],
            capture_output=True, text=True, timeout=5,
        )

    assert result.returncode == 1
    assert "Другой экземпляр" in result.stderr
    records = [json.loads(line) for line in result.stderr.splitlines()]
    assert records[0]["event"] == "run_started"
    assert records[-1]["event"] == "run_finished"
    assert records[-1]["exit_code"] == 1
    assert all(record["run_id"] == records[0]["run_id"] for record in records)


@pytest.mark.parametrize("live", [False, True], ids=["dry-run", "live"])
def test_main_runs_booking_and_notifies_only_in_live_mode(
    live, write_config, run_cli, make_api_handler, with_widget, mock_http,
    telegram_environment, tmp_path,
):
    handler, calls = make_api_handler()
    messages = []

    def respond(request):
        if request.url.host == "api.telegram.org":
            messages.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return handler(request)

    mock_http(with_widget(respond))
    path = write_config(live=True)

    assert run_cli(path, live=live) == 0

    prefix = "live" if live else "dry-run"
    state = json.loads((tmp_path / f"{prefix}-2026-10-29.json").read_text())
    assert state["status"] == ("AWAITING_PAYMENT" if live else "DRY_RUN_MATCH")
    expected = ["GetToken", "GetBookingsSlots"] + (["CreateReserveAfterPayment"] if live else [])
    assert [call["method"] for call in calls] == expected
    assert len(messages) == int(live)
    if live:
        assert state["payment_url"] in messages[0]["text"]
    before = len(calls)
    assert run_cli(path, live=live) == 0
    assert len(calls) == before


@pytest.mark.parametrize("changes", [
    {"booking": {"max_deposit_rub": 18999}},
    {"consents": {"privacy": False}},
    {"consents": {"restaurant_rules": False}},
], ids=["deposit-limit", "privacy", "restaurant-rules"])
def test_main_rejects_unsafe_live_config_before_network(changes, write_config, run_cli, mock_http, tmp_path):
    mock_http(lambda request: pytest.fail("Invalid config must not make HTTP requests"))
    path = write_config(changes, live=True)

    assert run_cli(path, live=True) == 1
    assert not list(tmp_path.glob("live-*.json"))


@pytest.mark.parametrize("changes", [
    {"guest": {"email": "private-invalid-contact"}},
    {"booking": {"visit_date": "private-invalid-date"}},
])
def test_main_logs_config_failure_without_invalid_field_values(changes, write_config, run_cli, log_events):
    assert run_cli(write_config(changes)) == 1

    failure, = log_events("stage_failed")
    assert failure["stage"] == "load_config"
    assert failure["error_type"] == "ValueError"
    assert "private-invalid" not in str(log_events())
    if "guest" in changes:
        assert "GUEST_EMAIL" in log_events("config_invalid")[0]["message"]


@pytest.mark.parametrize("before_opening", [False, True])
def test_main_check_reads_slots_only_after_opening(
    before_opening, write_config, run_cli, make_api_handler, with_widget, mock_http,
    clock, opening, capsys, tmp_path,
):
    clock.at = opening - timedelta(seconds=int(before_opening))
    handler, calls = make_api_handler()
    mock_http(with_widget(handler))

    assert run_cli(write_config(), command="check") == 0

    output = capsys.readouterr().out
    if before_opening:
        assert opening.isoformat() in output
        assert [call["method"] for call in calls] == ["GetToken"]
    else:
        assert [slot["time"] for slot in json.loads(output)["matching_slots"]] == ["19:00", "20:00"]
        assert [call["method"] for call in calls] == ["GetToken", "GetBookingsSlots"]
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("exists", [False, True])
def test_main_status_reads_correct_state_even_for_past_date(
    live, exists, write_config, run_cli, mock_http, tmp_path, capsys,
):
    mock_http(lambda request: pytest.fail("Status must not make HTTP requests"))
    path = write_config({"booking": {"visit_date": "2000-01-03"}})
    prefix = "live" if live else "dry-run"
    if exists:
        (tmp_path / f"{prefix}-2000-01-03.json").write_text('{"status": "EXPIRED"}')
    other_prefix = "dry-run" if live else "live"
    (tmp_path / f"{other_prefix}-2000-01-03.json").write_text('{"status": "UNKNOWN"}')

    assert run_cli(path, command="status", live=live) == 0

    output = capsys.readouterr().out
    if exists:
        assert json.loads(output) == {"status": "EXPIRED"}
    else:
        assert "ещё не выполнялась" in output


@pytest.mark.parametrize("status, exit_code", [
    ("UNKNOWN", 1), ("SUBMITTING", 1), ("REJECTED", 1), ("READ_FAILED", 1),
    ("NO_SLOTS", 0), ("EXPIRED", 0), ("ACCEPTED_WITHOUT_PAYMENT_LINK", 0),
    (None, 0),
])
def test_report_result_returns_expected_exit_code_without_dry_run_notification(
    status, exit_code, state_path, monkeypatch,
):
    monkeypatch.setattr(cli.notifications, "notify", lambda state: pytest.fail("Dry run must not notify"))
    state = {"status": status} if status else None

    assert cli.report_result(state, state_path, live=False) == exit_code


def test_main_reports_transport_failure_without_exposing_details(
    write_config, run_cli, with_widget, mock_http, caplog,
):
    def fail(request):
        raise httpx.ConnectError("private-token-and-contact")

    mock_http(with_widget(fail))

    assert run_cli(write_config()) == 1
    assert "ConnectError" in caplog.text
    assert "private-token-and-contact" not in caplog.text


def test_main_logs_unexpected_failure_without_raw_traceback(
    write_config, run_cli, with_widget, mock_http, log_events, capsys,
):
    def fail(request):
        raise RuntimeError("private-token-and-contact")

    mock_http(with_widget(fail))

    assert run_cli(write_config()) == 1
    assert log_events("run_failed")[0]["error_type"] == "RuntimeError"
    assert log_events("run_finished")[0]["exit_code"] == 1
    assert "private-token-and-contact" not in str(log_events())
    assert "private-token-and-contact" not in capsys.readouterr().err


def test_main_keeps_booking_result_when_notification_fails(
    write_config, run_cli, make_api_handler, with_widget, mock_http, telegram_environment, tmp_path,
):
    handler, calls = make_api_handler()

    def respond(request):
        if request.url.host == "api.telegram.org":
            return httpx.Response(500)
        return handler(request)

    mock_http(with_widget(respond))
    path = write_config(live=True)

    assert run_cli(path, live=True) == 0
    assert run_cli(path, live=True) == 0

    state = json.loads((tmp_path / "live-2026-10-29.json").read_text())
    assert state["payment_url"] == "https://payments.example/order"
    assert sum(call["method"] == "CreateReserveAfterPayment" for call in calls) == 1


def test_main_correlates_run_events_and_keeps_payment_link_out_of_logs(
    write_config, run_cli, make_api_handler, with_widget, mock_http, log_events,
):
    handler, calls = make_api_handler()
    mock_http(with_widget(handler))
    path = write_config(live=True)

    assert run_cli(path, live=True) == 0

    records = log_events()
    run_id = records[0]["run_id"]
    assert records[0]["event"] == "run_started"
    assert records[-1]["event"] == "run_finished"
    assert records[-1]["exit_code"] == 0
    assert all(record["run_id"] == run_id for record in records)
    assert "https://payments.example/order" not in str(records)
    assert "guest@test.invalid" not in str(records)
    assert "test-token" not in str(records)
    names = [record["event"] for record in records]
    assert names.index("slot_selected") < names.index("state_saved") < names.index("submission_result")

    assert run_cli(path, live=True) == 0

    assert log_events("run_started")[-1]["run_id"] != run_id
    assert log_events("repeat_blocked")[0]["request_id"] == log_events("submission_result")[0]["request_id"]
    assert sum(call["method"] == "CreateReserveAfterPayment" for call in calls) == 1


def test_configure_runtime_records_stop_signal_and_active_stage(runtime_signals, log_events):
    stop = cli.configure_runtime()

    with diagnostics.context(run_id="signal-test", stage="authenticate"):
        runtime_signals[signal.SIGTERM](signal.SIGTERM, None)

    assert stop.is_set()
    record, = log_events("stop_requested")
    assert record["signal"] == "SIGTERM"
    assert record["stage"] == "authenticate"
    assert record["run_id"] == "signal-test"
