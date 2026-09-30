"""Правила Birch: даты открытия, предоплата и выбор посадки."""

import re
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .diagnostics import event

MOSCOW = ZoneInfo("Europe/Moscow")
RELEASE_DAYS = 30  # Правило календаря проверенного виджета Birch.
ROOMS = {"3075230942": "Основной зал", "3075230944": "Открытая кухня"}
CHEF_TABLES = {
    "296465501247325", "296465501247323", "296465501247326", "296465501247327",
}


def now():
    return datetime.now(MOSCOW)


def parse_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", value):
        raise ValueError("Время должно быть строкой HH:MM или HH:MM:SS")
    return datetime.strptime(value, "%H:%M:%S" if len(value) == 8 else "%H:%M").time()


def deposit(booking):
    visit = date.fromisoformat(booking["visit_date"])
    per_guest = 11000 if date(2026, 6, 3) <= visit <= date(2026, 6, 7) else 9500
    return per_guest * booking["guests"]


def new_visit_date(instant):
    """Дата, которая станет выбираемой сегодня в 14:00 МСК; понедельники закрыты."""
    visit = instant.astimezone(MOSCOW).date() + timedelta(days=RELEASE_DAYS)
    return visit if visit.weekday() != 0 else None


def release_at(config):
    release = config["release"]
    day = date.fromisoformat(config["booking"]["visit_date"])
    day -= timedelta(days=RELEASE_DAYS)
    return datetime.combine(day, parse_time(release["opens_at"]), MOSCOW)


def _seconds_since_midnight(value):
    return value.hour * 3600 + value.minute * 60 + value.second


def _seating_allowed(slot, room_id, booking):
    if slot["is_common"] not in (0, 1):
        raise ValueError("Неизвестный тип посадки")
    if slot["is_common"] and not booking["allow_common_table"]:
        return False
    tables = {str(table) for table in slot["tables"]}
    if not tables:
        raise ValueError("Слот без списка столов")
    at_chef_counter = (
        booking["guests"] == 2
        and str(room_id) == "3075230944"
        and tables <= CHEF_TABLES
    )
    return not at_chef_counter or booking["allow_chefs_counter"]


def _matching_slot(slot, room_id, booking, excluded, time_window, rejected):
    start, end, preferred = time_window
    instant = datetime.strptime(slot["start_datetime"], "%Y-%m-%d %H:%M:%S")
    if instant.date().isoformat() != booking["visit_date"] or instant.weekday() == 0:
        rejected["date"] += 1
        return None
    slot_id = str(slot["id"])
    if not slot_id.isdigit() or int(slot["id"]) <= 0:
        raise ValueError("Неизвестный идентификатор слота")
    if not start <= instant.time() <= end:
        rejected["time_window"] += 1
        return None
    if slot_id in excluded:
        rejected["previous_conflict"] += 1
        return None
    if not _seating_allowed(slot, room_id, booking):
        reason = "common_table" if slot["is_common"] and not booking["allow_common_table"] else "chefs_counter"
        rejected[reason] += 1
        return None
    return {
        "slot_id": slot_id,
        "time": instant.strftime("%H:%M"),
        "room": ROOMS[str(room_id)],
        "distance": abs(_seconds_since_midnight(instant) - _seconds_since_midnight(preferred)),
    }


def select_slots(groups, booking, excluded=()):
    if groups == []:  # API использует [] для пустого результата и объект для непустого.
        event("slots_filtered", received=0, matched=0, rejected={})
        return []
    if not isinstance(groups, dict):
        raise ValueError("Неизвестный формат slots")
    time_window = tuple(
        parse_time(booking[key]) for key in ("time_from", "time_to", "preferred_time")
    )
    candidates = []
    rejected = Counter()
    received = 0
    for room_id, group in groups.items():
        received += len(group["slots"])
        if ROOMS.get(str(room_id)) not in booking["allowed_rooms"]:
            rejected["room"] += len(group["slots"])
            continue
        for slot in group["slots"]:
            candidate = _matching_slot(slot, room_id, booking, excluded, time_window, rejected)
            if candidate is not None:
                candidates.append(candidate)
    event("slots_filtered", received=received, matched=len(candidates), rejected=dict(rejected))
    return sorted(candidates, key=lambda slot: (
        slot["distance"], booking["allowed_rooms"].index(slot["room"]),
        slot["time"], slot["slot_id"],
    ))
