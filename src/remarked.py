"""HTTP-контракт виджета ReMarked и преобразование его ответов."""

import hashlib
import html
import logging
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from . import rules
from .diagnostics import context, error_fields, event

SITE = "https://birchrestaurants.com/saintpetersburg"
API = "https://app.remarked.ru/api/v1/ApiReservesWidget"
ASSETS = {
    "https://remarked.ru/widget/new/points/270400/custom/reserve/widgetReMarked.min.js?v=10":
        "da74483214f709195e4b6718641e4b99177f935a2f11149b431d6054c0c605ce",
    "https://remarked.ru/widget/new/points/270400/custom/reserve/config.js?v=10":
        "0979aeb859b4ba071e3b3774ab4d97fb6fe783f9745594b90a33f26118580c4e",
}


class RateLimited(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


def retry_after(value):
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(1.0, (parsedate_to_datetime(value) - rules.now()).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 1.0


def reserve_payload(config, slot):
    guest, booking = config["guest"], config["booking"]
    return {
        "date": booking["visit_date"], "guests_count": booking["guests"],
        "name": guest["first_name"], "surname": guest["last_name"], "phone": guest["phone"],
        "email": guest["email"], "telegram_username": guest["telegram"],
        "slot_id": slot["slot_id"], "time": slot["time"],
        "deposit_sum": rules.deposit(booking), "deposit_status": "not_paid", "eventTags": [2283],
    }


def _reservation_result(response):
    if response.get("status") == "error":
        status = "SLOT_TAKEN" if response.get("message") == "Slot is not free" else "REJECTED"
        message = response.get("message")
        code = response.get("error_code")
        event("reservation_rejected", level=logging.WARNING,
              reason="slot_taken" if status == "SLOT_TAKEN" else "server_rejected",
              api_error_code=code if type(code) is int else None,
              message_sha256=hashlib.sha256(message.encode()).hexdigest() if isinstance(message, str) else None)
        return {"status": status}
    if response.get("status") != "success":
        event("reservation_unknown", level=logging.ERROR, reason="unexpected_api_status")
        return {"status": "UNKNOWN"}
    link = response.get("form_url")
    if not link:
        event("payment_link_missing", level=logging.WARNING)
        return {"status": "ACCEPTED_WITHOUT_PAYMENT_LINK"}
    try:
        if not isinstance(link, str):
            raise ValueError("Платёжная ссылка должна быть строкой")
        url = urlsplit(link)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("Некорректная платёжная ссылка")
    except ValueError:
        event("payment_link_invalid", level=logging.ERROR)
        raise
    return {"status": "AWAITING_PAYMENT", "payment_url": link}


class RemarkedClient:
    def __init__(self, client):
        self.client = client
        self.token = None

    def verify_widget(self):
        response = self.client.get(SITE)
        response.raise_for_status()
        scripts = {
            html.unescape(src)
            for src in re.findall(r'<script\b[^>]*\bsrc=["\']([^"\']+)', response.text)
        }
        for url, expected in ASSETS.items():
            if url not in scripts:
                event("widget_mismatch", level=logging.ERROR, asset=url, reason="script_missing")
                raise ValueError("Birch изменил подключение виджета; требуется проверка клиента")
            source = self.client.get(url)
            source.raise_for_status()
            actual = hashlib.sha256(source.content).hexdigest()
            if actual != expected:
                event("widget_mismatch", level=logging.ERROR, asset=url, reason="hash_changed",
                      expected_sha256=expected, actual_sha256=actual)
                raise ValueError("Код или условия виджета изменились; требуется проверка клиента")
            event("widget_asset_verified", asset=url)

    def call(self, method, *, request_id=None, **data):
        body = {"method": method, "request_id": request_id or time.time_ns() // 1_000_000, **data}
        if self.token:
            body["token"] = self.token
        with context(method=method, request_id=body["request_id"]):
            return self._post(body)

    def _post(self, body):
        started = time.monotonic()
        response = None
        event("api_request_started")
        # Transport retries выключены, особенно для создания заявки.
        try:
            response = self.client.post(API, json=body)
            if response.status_code == 429:
                raise RateLimited(retry_after(response.headers.get("Retry-After")))
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Неизвестный формат ответа API")
        except (httpx.HTTPError, ValueError, RateLimited) as error:
            details = {"http_status": response.status_code if response is not None else None,
                       **error_fields(error)}
            if isinstance(error, RateLimited):
                details["retry_after_seconds"] = error.seconds
            event("api_request_failed", level=logging.ERROR,
                  elapsed_ms=round((time.monotonic() - started) * 1000, 1), **details)
            raise
        event("api_request_completed", http_status=response.status_code,
              elapsed_ms=round((time.monotonic() - started) * 1000, 1))
        return result

    def authenticate(self):
        self.token = None
        result = self.call("GetToken", point=270400)
        if not isinstance(result.get("token"), str) or not result["token"]:
            event("api_response_invalid", level=logging.ERROR, method="GetToken", reason="missing_or_invalid_token")
            raise ValueError("Не удалось получить токен ReMarked")
        self.token = result["token"]

    def slots(self, booking):
        day = booking["visit_date"]
        result = self.call("GetBookingsSlots", period={"from": day, "to": day},
                           guests_count=booking["guests"])
        if result.get("status") != "success" or "slots" not in result:
            event("api_response_invalid", level=logging.ERROR, method="GetBookingsSlots",
                  reason="request_rejected" if result.get("status") != "success" else "missing_slots")
            raise ValueError("ReMarked отклонил чтение слотов")
        return result["slots"]

    def create_reserve(self, reserve, request_id):
        with context(request_id=request_id):
            response = self.call("CreateReserveAfterPayment", request_id=request_id,
                                 reserve=reserve, getPaymentLink=1)
            return _reservation_result(response)
