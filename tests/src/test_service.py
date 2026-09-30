import json
from datetime import timedelta

import httpx
import pytest

from src import remarked, rules, service, storage


def test_prepare_request_stops_after_three_transport_failures(clock, opening, log_events):
    attempts = []

    def unavailable():
        attempts.append(clock.at)
        raise httpx.ReadTimeout("private-token")

    with pytest.raises(httpx.ReadTimeout):
        service.prepare_request(unavailable, clock, opening + timedelta(seconds=10))

    assert len(attempts) == 3
    assert clock.elapsed == 0.75
    assert [entry["wait_seconds"] for entry in log_events("preparation_retry")] == [0.25, 0.5]
    assert log_events("preparation_retries_exhausted")[0]["attempts"] == 3
    assert "private-token" not in str(log_events())


@pytest.mark.parametrize("failure", ["widget_changed", "bad_token", 400, 401, 403, 404])
def test_prepare_request_does_not_retry_permanent_failure(failure, clock, opening):
    attempts = []

    def rejected():
        attempts.append(clock.at)
        if isinstance(failure, str):
            raise ValueError(failure)
        response = httpx.Response(failure, request=httpx.Request("GET", remarked.SITE))
        response.raise_for_status()

    error = ValueError if isinstance(failure, str) else httpx.HTTPStatusError
    with pytest.raises(error):
        service.prepare_request(rejected, clock, opening + timedelta(seconds=10))

    assert len(attempts) == 1
    assert clock.elapsed == 0


@pytest.mark.parametrize("failure", [408, 429, 503, "rate_limited"])
def test_prepare_request_recovers_and_respects_retry_after(failure, clock, opening):
    attempts = []

    def intermittent():
        attempts.append(clock.at)
        if len(attempts) > 1:
            return
        if failure == "rate_limited":
            raise remarked.RateLimited(3)
        response = httpx.Response(failure, headers={"Retry-After": "3"},
                                  request=httpx.Request("GET", remarked.SITE))
        response.raise_for_status()

    assert service.prepare_request(intermittent, clock, opening + timedelta(seconds=10)) is True

    assert len(attempts) == 2
    assert clock.elapsed == (3 if failure in (429, "rate_limited") else 0.25)


@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "cancel"])
def test_prepare_request_stops_retrying_at_deadline_or_cancellation(cancel, clock, opening):
    attempts = []

    def limited():
        attempts.append(clock.at)
        if cancel:
            clock.stopped = True
        raise remarked.RateLimited(120)

    assert service.prepare_request(limited, clock, opening + timedelta(seconds=1)) is False

    assert len(attempts) == 1
    assert clock.elapsed <= 1


def test_run_stops_when_token_retry_exceeds_preparation_deadline(
    make_api_handler, run_service, clock, opening, booking_config,
):
    handler, calls = make_api_handler()

    def limited(request):
        response = handler(request)
        if json.loads(request.content)["method"] == "GetToken":
            return httpx.Response(429, headers={"Retry-After": "120"})
        return response

    state, _ = run_service(limited)

    assert state["status"] == "READ_FAILED"
    assert clock.elapsed == booking_config["release"]["max_poll_seconds"]
    assert [call["method"] for call in calls] == ["GetToken"]


def test_run_dry_run_never_submits_or_sends_personal_data(make_api_handler, run_service):
    handler, calls = make_api_handler()

    state, _ = run_service(handler, live=False)

    assert state["status"] == "DRY_RUN_MATCH"
    assert [call["method"] for call in calls] == ["GetToken", "GetBookingsSlots"]
    assert "reserve" not in calls[-1]
    assert calls[-1]["period"] == {"from": "2026-10-29", "to": "2026-10-29"}


def test_run_saves_payment_link_and_blocks_repeat_submission(
    make_api_handler, run_service, state_path, booking_config, clock,
):
    handler, calls = make_api_handler()

    state, api = run_service(handler)

    assert state["status"] == "AWAITING_PAYMENT"
    assert json.loads(state_path.read_text())["payment_url"] == "https://payments.example/order"
    assert state_path.stat().st_mode & 0o777 == 0o600
    before = len(calls)
    assert service.run(api, booking_config, state_path, True, clock) == state
    assert len(calls) == before


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError], ids=lambda error: error.__name__)
def test_run_does_not_retry_network_failure(
    failure, make_api_handler, run_service, state_path, booking_config, clock,
):
    handler, calls = make_api_handler([failure("request failed")])

    state, api = run_service(handler)

    assert state["status"] == "UNKNOWN"
    assert service.run(api, booking_config, state_path, True, clock)["status"] == "UNKNOWN"
    assert sum(call["method"] == "CreateReserveAfterPayment" for call in calls) == 1


@pytest.mark.parametrize("status_code", [429, 500])
def test_run_does_not_retry_http_failure(status_code, make_api_handler, run_service):
    handler, calls = make_api_handler()

    def fail_submission(request):
        result = handler(request)
        if json.loads(request.content)["method"] == "CreateReserveAfterPayment":
            return httpx.Response(status_code, headers={"Retry-After": "1"})
        return result

    state, _ = run_service(fail_submission)

    assert state["status"] == "UNKNOWN"
    assert sum(call["method"] == "CreateReserveAfterPayment" for call in calls) == 1


@pytest.mark.parametrize("response, expected", [
    ({"status": "success", "form_url": "http://payments.example/order"}, "UNKNOWN"),
    ({"status": "success", "form_url": 123}, "UNKNOWN"),
    ({"status": "success", "form_url": "https://[invalid/order"}, "UNKNOWN"),
    ({"status": "success"}, "ACCEPTED_WITHOUT_PAYMENT_LINK"),
], ids=["unsafe-link", "wrong-link-type", "malformed-link", "missing-link"])
def test_run_does_not_confirm_booking_without_safe_payment_link(
    response, expected, make_api_handler, run_service, log_events, booking_config, state_path, clock,
):
    handler, calls = make_api_handler([response])

    state, api = run_service(handler)

    assert state["status"] == expected
    assert service.run(api, booking_config, state_path, True, clock) == state
    assert sum(call["method"] == "CreateReserveAfterPayment" for call in calls) == 1
    if expected == "UNKNOWN":
        assert log_events("payment_link_invalid")


def test_submit_persists_state_before_request_and_blocks_restart(
    booking_config, make_slots, state_path, clock,
):
    slot = rules.select_slots(make_slots("19:00"), booking_config["booking"])[0]

    def crash(request):
        assert json.loads(state_path.read_text())["status"] == "SUBMITTING"
        raise SystemExit("simulated process termination")

    with httpx.Client(transport=httpx.MockTransport(crash)) as client:
        with pytest.raises(SystemExit):
            service.submit(remarked.RemarkedClient(client), booking_config, slot, state_path)

    assert service.run(None, booking_config, state_path, True, clock)["status"] == "SUBMITTING"


def test_run_tries_next_slot_after_confirmed_conflict(make_api_handler, run_service):
    handler, calls = make_api_handler([
        {"status": "error", "message": "Slot is not free"},
        {"status": "success", "form_url": "https://payments.example/order"},
    ])

    state, _ = run_service(handler)

    assert state["status"] == "AWAITING_PAYMENT"
    submitted = [call["reserve"]["slot_id"] for call in calls if call["method"] == "CreateReserveAfterPayment"]
    assert submitted == ["1", "2"]


def test_run_blocks_restart_after_persisted_submission(booking_config, state_path, clock):
    storage.save_state(state_path, {"status": "SUBMITTING", "date": booking_config["booking"]["visit_date"]})

    state = service.run(None, booking_config, state_path, True, clock)

    assert state["status"] == "SUBMITTING"


@pytest.mark.parametrize("content", ["null", "[]", "{}", '"text"', "{"])
def test_run_blocks_restart_with_invalid_state(content, booking_config, state_path, clock):
    state_path.write_text(content)

    with pytest.raises(ValueError):
        service.run(None, booking_config, state_path, True, clock)

    assert state_path.read_text() == content


def test_run_stops_at_deadline_during_rate_limit(make_api_handler, run_service):
    handler, calls = make_api_handler()

    def limited(request):
        result = handler(request)
        if json.loads(request.content)["method"] == "GetBookingsSlots":
            return httpx.Response(429, headers={"Retry-After": "120"})
        return result

    state, _ = run_service(limited)

    assert state["status"] == "READ_FAILED"
    assert len(calls) == 2


def test_run_does_not_submit_after_missed_release(make_api_handler, run_service, clock, opening):
    clock.at = opening + timedelta(minutes=2)
    handler, calls = make_api_handler()

    state, _ = run_service(handler)

    assert state["status"] == "EXPIRED"
    assert calls == []


def test_run_prepares_connection_and_waits_until_release(
    booking_config, make_api_handler, run_service, clock, opening,
):
    clock.at = opening - timedelta(seconds=150)
    handler, _ = make_api_handler()
    observed = []

    def record_time(request):
        observed.append((json.loads(request.content)["method"], clock.at))
        return handler(request)

    run_service(record_time)

    assert observed[0] == ("GetToken", opening - timedelta(seconds=15))
    assert all(at >= rules.release_at(booking_config) for method, at in observed if method != "GetToken")


@pytest.mark.parametrize("stage", ["write", "file_fsync", "replace", "directory_fsync"])
def test_submit_does_not_send_when_initial_state_cannot_be_saved(
    stage, booking_config, make_slots, make_api_handler, state_path, clock, fail_state_io,
):
    handler, calls = make_api_handler()
    slot = rules.select_slots(make_slots("19:00"), booking_config["booking"])[0]
    fail_state_io(stage)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OSError):
            service.submit(remarked.RemarkedClient(client), booking_config, slot, state_path)

    assert calls == []
    if state_path.exists():
        assert service.run(None, booking_config, state_path, True, clock)["status"] == "SUBMITTING"


@pytest.mark.parametrize("stage", ["write", "file_fsync", "replace", "directory_fsync"])
def test_submit_blocks_retry_when_final_state_cannot_be_saved(
    stage, booking_config, make_slots, make_api_handler, state_path, clock, fail_state_io, log_events,
):
    handler, calls = make_api_handler()
    slot = rules.select_slots(make_slots("19:00"), booking_config["booking"])[0]
    fail_state_io(stage, occurrence=2)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        api = remarked.RemarkedClient(client)
        with pytest.raises(OSError):
            service.submit(api, booking_config, slot, state_path)
        restored = service.run(api, booking_config, state_path, True, clock)

    expected = "AWAITING_PAYMENT" if stage == "directory_fsync" else "SUBMITTING"
    assert restored["status"] == expected
    assert [call["method"] for call in calls] == ["CreateReserveAfterPayment"]
    failure, = log_events("state_save_failed")
    assert failure["stage"] == "persist_after_submission"
    assert failure["request_id"] == restored["request_id"]


@pytest.mark.parametrize("interval_ms", [50, 500])
def test_run_finishes_without_booking_when_no_slots_exist(
    interval_ms, booking_config, make_api_handler, run_service, clock, state_path,
):
    booking_config["release"]["max_poll_seconds"] = 1
    booking_config["release"]["poll_interval_ms"] = interval_ms
    handler, calls = make_api_handler(groups=[])

    state, _ = run_service(handler)

    assert state == {"status": "NO_SLOTS", "date": "2026-10-29"}
    assert json.loads(state_path.read_text()) == state
    assert clock.elapsed == pytest.approx(1)
    assert [call["method"] for call in calls] == ["GetToken"] + ["GetBookingsSlots"] * (1000 // interval_ms)


@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "cancel"])
def test_run_does_not_submit_after_slow_slot_response(
    cancel, make_api_handler, run_service, clock, booking_config,
):
    handler, calls = make_api_handler()

    def slow_slots(request):
        response = handler(request)
        if json.loads(request.content)["method"] == "GetBookingsSlots":
            if cancel:
                clock.stopped = True
            else:
                clock.wait(booking_config["release"]["max_poll_seconds"])
        return response

    state, _ = run_service(slow_slots)

    if cancel:
        assert state is None
    else:
        assert state["status"] == "NO_SLOTS"
    assert [call["method"] for call in calls] == ["GetToken", "GetBookingsSlots"]


@pytest.mark.parametrize("status_code", [429, 503])
def test_run_reports_read_failure_if_api_stops_responding_after_empty_slots(
    status_code, booking_config, make_api_handler, run_service, state_path, log_events,
):
    booking_config["release"]["max_poll_seconds"] = 1
    handler, calls = make_api_handler(groups=[])

    def fail_after_first_read(request):
        response = handler(request)
        if sum(call["method"] == "GetBookingsSlots" for call in calls) > 1:
            return httpx.Response(status_code)
        return response

    state, _ = run_service(fail_after_first_read, live=False)

    assert state["status"] == "READ_FAILED"
    assert json.loads(state_path.read_text()) == state
    finished, = log_events("poll_finished")
    assert finished["successful_reads"] == 1
    assert finished["last_read_succeeded"] is False
    assert [call["method"] for call in calls] == ["GetToken", "GetBookingsSlots", "GetBookingsSlots"]


@pytest.mark.parametrize("seconds_before", [150, 100, 1])
def test_run_can_be_cancelled_during_preparation(
    seconds_before, make_api_handler, run_service, clock, opening, state_path,
):
    clock.at = opening - timedelta(seconds=seconds_before)
    handler, calls = make_api_handler()
    advance = clock.wait

    def cancel_wait(seconds):
        advance(seconds)
        clock.stopped = True

    clock.wait = cancel_wait

    state, _ = run_service(handler)

    assert state is None
    assert not state_path.exists()
    assert all(call["method"] == "GetToken" for call in calls)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, 500], ids=["timeout", "http-500"])
def test_run_recovers_from_transient_slot_read_error(failure, make_api_handler, run_service, clock):
    handler, calls = make_api_handler()
    reads = 0

    def temporary_failure(request):
        nonlocal reads
        response = handler(request)
        if json.loads(request.content)["method"] == "GetBookingsSlots":
            reads += 1
            if reads == 1:
                if failure == 500:
                    return httpx.Response(500)
                raise failure("read failed")
        return response

    state, _ = run_service(temporary_failure)

    assert state["status"] == "AWAITING_PAYMENT"
    assert clock.elapsed == 1
    assert [call["method"] for call in calls] == [
        "GetToken", "GetBookingsSlots", "GetBookingsSlots", "CreateReserveAfterPayment",
    ]


def test_run_logs_submission_timeout_with_request_id_and_no_contacts(
    make_api_handler, run_service, state_path, booking_config, clock, log_events,
):
    booking_config["guest"]["email"] = "private@example.invalid"
    handler, calls = make_api_handler([httpx.ReadTimeout("private@example.invalid test-token")])

    def timeout(request):
        if json.loads(request.content)["method"] == "CreateReserveAfterPayment":
            assert log_events("api_request_started")[-1]["method"] == "CreateReserveAfterPayment"
            assert json.loads(state_path.read_text())["status"] == "SUBMITTING"
        return handler(request)

    state, api = run_service(timeout)

    assert service.run(api, booking_config, state_path, True, clock)["status"] == "UNKNOWN"
    failed, = log_events("api_request_failed")
    assert failed["request_id"] == state["request_id"]
    assert failed["method"] == "CreateReserveAfterPayment"
    assert failed["stage"] == "submit"
    assert failed["poll_attempt"] == 1
    assert failed["error_type"] == "ReadTimeout"
    assert failed["http_status"] is None
    assert failed["elapsed_ms"] >= 0
    assert log_events("submission_uncertain")[0]["retry_allowed"] is False
    assert sum(call["method"] == "CreateReserveAfterPayment" for call in calls) == 1
    assert "private@example.invalid" not in str(log_events())
    assert "test-token" not in str(log_events())


def test_run_logs_late_first_request_and_poll_deadline(
    booking_config, make_api_handler, run_service, clock, opening, log_events,
):
    booking_config["release"]["max_poll_seconds"] = 2
    booking_config["release"]["poll_interval_ms"] = 500
    clock.at = opening + timedelta(seconds=1)
    handler, _ = make_api_handler(groups=[])

    state, _ = run_service(handler)

    assert state["status"] == "NO_SLOTS"
    assert log_events("first_slot_request")[0]["opening_lag_ms"] == 1000
    finished, = log_events("poll_finished")
    assert finished["reason"] == "deadline"
    assert finished["attempts"] == finished["successful_reads"] == 2
    assert all(record["received"] == 0 for record in log_events("slots_filtered"))


@pytest.mark.parametrize("body, expected", [
    ('{"status": "error", "message": "Rejected"}', "REJECTED"),
    ('{}', "UNKNOWN"),
    ('[]', "UNKNOWN"),
    ('not json', "UNKNOWN"),
])
def test_run_never_retries_rejected_or_invalid_submission_response(
    body, expected, make_api_handler, run_service, booking_config, clock, state_path,
):
    handler, calls = make_api_handler()

    def invalid_submission(request):
        response = handler(request)
        if json.loads(request.content)["method"] == "CreateReserveAfterPayment":
            return httpx.Response(200, text=body)
        return response

    state, api = run_service(invalid_submission)

    assert state["status"] == expected
    assert service.run(api, booking_config, state_path, True, clock) == state
    assert sum(call["method"] == "CreateReserveAfterPayment" for call in calls) == 1
