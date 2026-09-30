import hashlib
from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest

from src import remarked, rules


def test_reserve_payload_matches_widget_without_payment_credentials(booking_config, make_slots):
    slot = rules.select_slots(make_slots("19:00"), booking_config["booking"])[0]

    payload = remarked.reserve_payload(booking_config, slot)

    assert payload == {
        "date": "2026-10-29", "guests_count": 2, "name": "Иван", "surname": "Иванов",
        "phone": "+79991234567", "email": "ivan@example.com", "telegram_username": "@username",
        "slot_id": "1", "time": "19:00", "deposit_sum": 19000,
        "deposit_status": "not_paid", "eventTags": [2283],
    }


def test_verify_widget_rejects_changed_source():
    def respond(request):
        if str(request.url) == remarked.SITE:
            scripts = ''.join(f'<script src="{url}"></script>' for url in remarked.ASSETS)
            return httpx.Response(200, text=scripts)
        return httpx.Response(200, content=b"changed widget")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="изменились"):
            remarked.RemarkedClient(client).verify_widget()


@pytest.mark.parametrize("header, seconds", [("5", 5), ("0", 1), (None, 1), ("invalid", 1)])
def test_retry_after_handles_seconds_and_invalid_headers(header, seconds):
    assert remarked.retry_after(header) == seconds


@pytest.mark.parametrize("offset, seconds", [(-10, 1), (5, 5)])
def test_retry_after_handles_http_date(offset, seconds, opening, clock):
    header = format_datetime(opening + timedelta(seconds=offset))

    assert remarked.retry_after(header) == seconds


@pytest.mark.parametrize("token", [None, "", 123])
def test_authenticate_rejects_invalid_token(token, log_events):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"token": token}))
    with httpx.Client(transport=transport) as client:
        api = remarked.RemarkedClient(client)
        with pytest.raises(ValueError, match="токен"):
            api.authenticate()
        assert api.token is None
    failure, = log_events("api_response_invalid")
    assert failure["method"] == "GetToken"
    assert failure["reason"] == "missing_or_invalid_token"


@pytest.mark.parametrize("response", [{"status": "error"}, {"status": "success"}])
def test_slots_rejects_failure_or_missing_slots(response, booking_config, log_events):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ValueError, match="чтение слотов"):
            remarked.RemarkedClient(client).slots(booking_config["booking"])
    failure, = log_events("api_response_invalid")
    assert failure["method"] == "GetBookingsSlots"
    assert failure["reason"] == ("request_rejected" if response["status"] == "error" else "missing_slots")


def test_verify_widget_rejects_missing_script():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html></html>"))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ValueError, match="подключение виджета"):
            remarked.RemarkedClient(client).verify_widget()


def test_create_reserve_logs_rejection_code_without_raw_message(booking_config, make_slots, log_events):
    message = "Rejected for private-contact, token=secret-token"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "status": "error", "error_code": 42, "message": message,
    }))
    slot = rules.select_slots(make_slots("19:00"), booking_config["booking"])[0]

    with httpx.Client(transport=transport) as client:
        result = remarked.RemarkedClient(client).create_reserve(
            remarked.reserve_payload(booking_config, slot), 123,
        )

    assert result == {"status": "REJECTED"}
    record, = log_events("reservation_rejected")
    assert record["reason"] == "server_rejected"
    assert record["request_id"] == 123
    assert record["api_error_code"] == 42
    assert record["message_sha256"] == hashlib.sha256(message.encode()).hexdigest()
    assert "private-contact" not in str(log_events())
    assert "secret-token" not in str(log_events())


@pytest.mark.parametrize("response, status, error", [
    (httpx.Response(429, headers={"Retry-After": "3"}), 429, remarked.RateLimited),
    (httpx.Response(503), 503, httpx.HTTPStatusError),
    (httpx.Response(200, text="private malformed content"), 200, ValueError),
])
def test_call_logs_http_and_parsing_failures(response, status, error, log_events):
    transport = httpx.MockTransport(lambda request: response)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(error):
            remarked.RemarkedClient(client).call("GetToken", request_id=123)

    started, = log_events("api_request_started")
    failed, = log_events("api_request_failed")
    assert started["request_id"] == failed["request_id"] == 123
    assert failed["http_status"] == status
    assert failed["method"] == "GetToken"
    if status == 429:
        assert failed["retry_after_seconds"] == 3
    assert "private malformed content" not in str(log_events())
