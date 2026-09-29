"""Чтение и проверка пользовательского конфига."""

import logging
import os
import re
import tomllib
from datetime import date

from dotenv import load_dotenv

from . import rules
from .diagnostics import event


def _invalid(message):
    # Сообщения заданы в коде; значения полей конфига сюда не передаются.
    event("config_invalid", level=logging.ERROR, message=message)
    raise ValueError(message)


def normalize_phone(value):
    if not isinstance(value, str):
        _invalid("GUEST_PHONE: нужен российский номер телефона")
    number = re.sub(r"[\s()-]", "", value)
    if re.fullmatch(r"(?:\+7|7|8)[1-9][0-9]{9}", number):
        return "+7" + number[-10:]
    if re.fullmatch(r"[1-9][0-9]{9}", number):
        return "+7" + number
    _invalid("GUEST_PHONE: ожидаются 10 цифр после +7")


def _validate_guest(guest):
    for key in ("first_name", "last_name", "email", "telegram"):
        if not isinstance(guest[key], str) or len(guest[key].strip()) < 2:
            _invalid(f"GUEST_{key.upper()}: заполните поле в .env или окружении")
        guest[key] = guest[key].strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", guest["email"]):
        _invalid("GUEST_EMAIL: неверный адрес")
    if not re.fullmatch(r"@?[A-Za-z0-9_]{1,32}", guest["telegram"]):
        _invalid("GUEST_TELEGRAM: ожидается логин Telegram, не ссылка")
    guest["phone"] = normalize_phone(guest["phone"])


def _validate_seating(booking):
    rooms = booking["allowed_rooms"]
    if not isinstance(rooms, list) or not rooms or any(room not in rules.ROOMS.values() for room in rooms):
        _invalid("booking.allowed_rooms: укажите известные залы Birch")
    for key in ("allow_common_table", "allow_chefs_counter"):
        if type(booking[key]) is not bool:
            _invalid(f"booking.{key}: ожидается true/false")


def _resolve_visit_date(booking):
    if booking.get("visit_date", "auto") != "auto":
        return
    visit = rules.new_visit_date(rules.now())
    if visit is None:
        _invalid("Сегодня новая дата не открывается: понедельники закрыты; старая дата не выбирается")
    booking["visit_date"] = visit.isoformat()
    event("visit_date_selected", date_mode="auto", visit_date=booking["visit_date"], source="widget_calendar_rule")


def _validate_booking(booking, allow_past):
    visit = date.fromisoformat(booking["visit_date"])
    if not allow_past and (visit.weekday() == 0 or visit < rules.now().date()):
        _invalid("Дата визита должна быть не в прошлом и не в понедельник")
    if type(booking["guests"]) is not int or not 1 <= booking["guests"] <= 7:
        _invalid("Поддерживается обычная бронь на 1–7 гостей")
    start, end, preferred = (
        rules.parse_time(booking[key]) for key in ("time_from", "time_to", "preferred_time")
    )
    if not start <= preferred <= end:
        _invalid("Окно времени некорректно или preferred_time вне окна")
    _validate_seating(booking)
    if type(booking["max_deposit_rub"]) is not int or booking["max_deposit_rub"] < rules.deposit(booking):
        _invalid("Сумма предоплаты по правилам виджета превышает max_deposit_rub")


def _validate_release(release):
    # Birch публикует даты по московскому календарю, независимо от часового пояса хоста.
    if release["timezone"] != "Europe/Moscow":
        _invalid("Для Birch требуется Europe/Moscow")
    # Старые конфиги принимаются, но горизонт задаётся контрактом виджета.
    if release.get("days_before_visit", rules.RELEASE_DAYS) != rules.RELEASE_DAYS:
        _invalid("days_before_visit: правило виджета Birch равно 30")
    if rules.parse_time(release["opens_at"]) < rules.parse_time("14:00:00"):
        _invalid("Начинать отправку заявок раньше открытия Birch в 14:00 нельзя")
    limits = (
        ("prepare_seconds", 10, 600),
        ("poll_interval_ms", 50, 10000),
        ("max_poll_seconds", 1, 600),
    )
    for key, minimum, maximum in limits:
        if type(release[key]) is not int or not minimum <= release[key] <= maximum:
            _invalid(f"release.{key}: допустимо {minimum}–{maximum}")


def _validate_live_mode(config):
    consents = config.get("consents", {})
    if not all(consents.get(key) is True for key in ("privacy", "restaurant_rules")):
        _invalid("Для --live подтвердите ознакомление с правилами в [consents]")
    guest = config["guest"]
    if guest["email"].lower().endswith("@example.com") or guest["telegram"] == "@username":
        _invalid("Для --live замените пример контактных данных")


def load_config(path, live=False, allow_past=False):
    with path.open("rb") as file:
        config = tomllib.load(file)
    if "guest" in config:
        _invalid("Перенесите секцию [guest] из TOML в переменные GUEST_* в .env")
    load_dotenv(path.with_name(".env"), override=False, interpolate=False)
    config["guest"] = {
        key: os.getenv(f"GUEST_{key.upper()}", "")
        for key in ("first_name", "last_name", "phone", "email", "telegram")
    }
    _validate_guest(config["guest"])
    _resolve_visit_date(config["booking"])
    _validate_booking(config["booking"], allow_past)
    _validate_release(config["release"])
    if live:
        _validate_live_mode(config)
    return config
