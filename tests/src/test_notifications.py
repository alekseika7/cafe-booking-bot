import json

import httpx
import pytest

from src import notifications


@pytest.mark.parametrize("token, chat, valid", [
    (None, None, True), ("test-token", "1234", True),
    ("test-token", None, False), (None, "1234", False),
])
def test_validate_settings_requires_both_telegram_variables(token, chat, valid, monkeypatch):
    for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat)):
        if value is not None:
            monkeypatch.setenv(name, value)

    if valid:
        assert notifications.validate_settings() is None
    else:
        with pytest.raises(ValueError, match="оба"):
            notifications.validate_settings()


def test_format_message_includes_payment_details_and_hold_limitation(payment_state):
    message = notifications.format_message(payment_state)

    for value in ("2026-10-29", "19:00", "гостей: 2", "Основной зал", "19000", payment_state["payment_url"]):
        assert value in message
    assert "Удержание места до оплаты не подтверждено" in message


def test_format_message_handles_status_without_payment_link():
    message = notifications.format_message({"status": "EXPIRED", "date": "2026-10-29"})

    assert "время запуска пропущено" in message
    assert "2026-10-29" in message
    assert "Оплатите" not in message


def test_notify_skips_http_when_not_configured(payment_state, mock_http):
    mock_http(lambda request: pytest.fail("Unconfigured notification must not use HTTP"))

    assert notifications.notify(payment_state) is None


def test_notify_sends_expected_message(payment_state, telegram_environment, mock_http):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    mock_http(respond)
    notifications.notify(payment_state)

    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.telegram.org/bottest-token/sendMessage"
    payload = json.loads(requests[0].content)
    assert payload["chat_id"] == "1234"
    assert payment_state["payment_url"] in payload["text"]
    assert payload["disable_web_page_preview"] is True


@pytest.mark.parametrize("failure", ["timeout", "http-500", "api-error", "bad-json", "wrong-shape"])
def test_notify_handles_delivery_errors_without_exposing_secrets(
    failure, payment_state, telegram_environment, mock_http, caplog, log_events,
):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private-contact test-token")
        if failure == "http-500":
            return httpx.Response(500)
        if failure == "bad-json":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json=[] if failure == "wrong-shape" else {
            "ok": False, "error_code": 400, "description": "private-contact test-token",
        })

    mock_http(respond)

    assert notifications.notify(payment_state) is None
    assert "не доставлено" in caplog.text
    assert "test-token" not in caplog.text
    assert "private-contact" not in caplog.text
    record, = log_events("notification_failed")
    assert record["reason"] == {
        "timeout": "transport", "http-500": "http_status", "api-error": "api_rejected",
        "bad-json": "invalid_json", "wrong-shape": "invalid_response",
    }[failure]
    if failure == "api-error":
        assert record["api_error_code"] == 400
    assert payment_state["payment_url"] not in str(log_events())
