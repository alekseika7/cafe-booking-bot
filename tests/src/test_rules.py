from datetime import datetime

import pytest

from src import rules


@pytest.mark.parametrize("instant, expected", [
    ("2026-09-29T13:59:59+03:00", "2026-10-29"),
    ("2026-09-29T14:00:00+03:00", "2026-10-29"),
    ("2026-09-29T23:59:59+03:00", "2026-10-29"),
    ("2026-09-26T14:00:00+03:00", None),
    ("2026-09-27T14:00:00+03:00", "2026-10-27"),
    ("2026-09-28T20:59:59+00:00", "2026-10-28"),
    ("2026-09-28T21:00:00+00:00", "2026-10-29"),
    ("2026-12-02T14:00:00+03:00", "2027-01-01"),
])
def test_new_visit_date_uses_today_moscow_release_and_never_falls_back(instant, expected):
    visit = rules.new_visit_date(datetime.fromisoformat(instant))

    assert (visit.isoformat() if visit else None) == expected


def test_release_at_crosses_year_boundary(booking_config):
    booking_config["booking"]["visit_date"] = "2027-01-15"

    assert rules.release_at(booking_config) == datetime(2026, 12, 16, 14, tzinfo=rules.MOSCOW)


def test_select_slots_filters_date_time_and_seating_and_orders_preferences(booking_config, make_slots):
    booking = booking_config["booking"]
    groups = make_slots("17:59", "18:00", "19:00", "21:00", "21:01")
    groups["3075230942"]["slots"][2]["is_common"] = 1
    groups["3075230942"]["slots"].append(
        {"id": 100, "start_datetime": "2026-10-28 19:00:00", "is_common": 0, "tables": ["1"]})
    groups["3075230944"] = {"slots": [
        {"id": 200, "start_datetime": "2026-10-29 19:00:00", "is_common": 0,
         "tables": ["296465501247325", "296465501247323"]}]}

    selected = rules.select_slots(groups, booking)

    assert [slot["time"] for slot in selected] == ["18:00", "21:00"]
    booking["allow_chefs_counter"] = True
    assert rules.select_slots(groups, booking)[0]["slot_id"] == "200"
    booking["allow_common_table"] = True
    assert rules.select_slots(groups, booking)[0]["slot_id"] == "3"
    assert rules.select_slots(groups, booking, {"3", "200"})[0]["time"] == "18:00"


def test_select_slots_excludes_closed_monday(booking_config, make_slots):
    booking = booking_config["booking"]
    booking["visit_date"] = "2026-10-26"
    groups = make_slots("19:00")
    groups["3075230942"]["slots"][0]["start_datetime"] = "2026-10-26 19:00:00"

    assert rules.select_slots(groups, booking) == []
    assert rules.select_slots([], booking) == []


def test_select_slots_logs_counts_for_each_rejection_reason(booking_config, make_slots, log_events):
    groups = make_slots("19:00", "17:00", "19:00", "19:00", "19:00")
    slots = groups["3075230942"]["slots"]
    slots[2]["start_datetime"] = "2026-10-28 19:00:00"
    slots[3]["is_common"] = 1
    groups["unknown-room"] = {"slots": [slots[0]]}
    groups["3075230944"] = {"slots": [
        {"id": 100, "start_datetime": "2026-10-29 19:00:00", "is_common": 0,
         "tables": ["296465501247325"]},
    ]}

    selected = rules.select_slots(groups, booking_config["booking"], excluded={"5"})

    assert [slot["slot_id"] for slot in selected] == ["1"]
    record, = log_events("slots_filtered")
    assert record["received"] == 7
    assert record["matched"] == 1
    assert record["rejected"] == {
        "room": 1, "date": 1, "time_window": 1, "previous_conflict": 1,
        "common_table": 1, "chefs_counter": 1,
    }
