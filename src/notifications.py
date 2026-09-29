"""Необязательное уведомление о результате, без участия в оплате."""

import logging
import os
import time

import httpx

from .diagnostics import error_fields, event

STATUS_LABELS = {
    "AWAITING_PAYMENT": "получена ссылка на оплату",
    "ACCEPTED_WITHOUT_PAYMENT_LINK": "заявка принята без ссылки; проверьте результат у ресторана",
    "SUBMITTING": "отправка была прервана; проверьте результат у ресторана",
    "UNKNOWN": "результат отправки неизвестен; проверьте у ресторана",
    "REJECTED": "заявка отклонена",
    "NO_SLOTS": "подходящих мест не найдено",
    "READ_FAILED": "не удалось получить свободные места",
    "EXPIRED": "время запуска пропущено",
}


def validate_settings():
    if bool(os.getenv("TELEGRAM_BOT_TOKEN")) != bool(os.getenv("TELEGRAM_CHAT_ID")):
        event("notification_config_invalid", level=logging.ERROR, reason="both_env_vars_required")
        raise ValueError("Для уведомлений задайте оба TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")


def format_message(state):
    label = STATUS_LABELS.get(state["status"], state["status"])
    text = f"Birch: {label}\nДата: {state['date']}"
    if state.get("payment_url"):
        text += (
            f"\n{state['time']}, гостей: {state['guests']}, {state['room']}"
            f"\nПредоплата по заявке: {state['deposit_rub']} ₽"
            f"\nОплатите самостоятельно: {state['payment_url']}"
            "\nУдержание места до оплаты не подтверждено."
        )
    return text


def notify(state):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        event("notification_skipped", reason="not_configured")
        return
    message = format_message(state)
    started = time.monotonic()
    response = None
    reason = "transport"
    api_error_code = None
    event("notification_started")
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": message, "disable_web_page_preview": True},
            timeout=10,
        )
        reason = "http_status"
        response.raise_for_status()
        reason = "invalid_json"
        result = response.json()
        reason = "invalid_response"
        if isinstance(result, dict) and type(result.get("error_code")) is int:
            api_error_code = result["error_code"]
        if isinstance(result, dict) and result.get("ok") is False:
            reason = "api_rejected"
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError("Notification failed")
    except (httpx.HTTPError, ValueError) as error:
        details = {"http_status": response.status_code if response is not None else None,
                   **error_fields(error)}
        event("notification_failed", level=logging.ERROR, reason=reason, api_error_code=api_error_code,
              elapsed_ms=round((time.monotonic() - started) * 1000, 1),
              message="Уведомление Telegram не доставлено; результат сохранён в файле состояния", **details)
        return
    event("notification_delivered", elapsed_ms=round((time.monotonic() - started) * 1000, 1))
