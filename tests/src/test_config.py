from datetime import timedelta

import pytest

from src import config as configuration
from src import rules


def test_load_config_reads_guest_from_dotenv_next_to_config(
    write_config, write_env, guest_environment, clock, monkeypatch, tmp_path,
):
    path = write_config(live=True)
    guest = {**guest_environment, "first_name": "Иван ${NAME}", "last_name": "О'Нил",
             "phone": "8 (999) 123-45-67", "email": "guest@test.invalid", "telegram": "@test_guest"}
    write_env({f"GUEST_{key.upper()}": value for key, value in guest.items()})
    monkeypatch.setenv("NAME", "must-not-expand")
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text("GUEST_FIRST_NAME=wrong-directory\n")
    monkeypatch.chdir(other)

    config = configuration.load_config(path, live=True)

    assert config["guest"] == {**guest, "phone": "+79991234567"}
    assert "[guest]" not in path.read_text()


def test_load_config_prefers_exported_environment_to_dotenv(write_config, write_env, clock, monkeypatch):
    path = write_config()
    write_env({"GUEST_PHONE": "+79991111111"})
    monkeypatch.setenv("GUEST_PHONE", "+79992222222")

    config = configuration.load_config(path)

    assert config["guest"]["phone"] == "+79992222222"


@pytest.mark.parametrize("key", ["FIRST_NAME", "LAST_NAME", "PHONE", "EMAIL", "TELEGRAM"])
@pytest.mark.parametrize("value", [None, ""])
def test_load_config_rejects_missing_or_empty_guest_environment(
    key, value, write_config, clock, monkeypatch,
):
    path = write_config()
    if value is None:
        monkeypatch.delenv(f"GUEST_{key}")
    else:
        monkeypatch.setenv(f"GUEST_{key}", value)

    with pytest.raises(ValueError, match=f"GUEST_{key}"):
        configuration.load_config(path)


def test_load_config_rejects_legacy_guest_section_without_logging_values(write_config, clock, log_events):
    path = write_config()
    path.write_text(path.read_text() + '\n[guest]\nemail = "private@example.invalid"\n')

    with pytest.raises(ValueError, match=r"\[guest\]"):
        configuration.load_config(path)

    assert "private@example.invalid" not in str(log_events())


@pytest.mark.parametrize("explicit_auto", [False, True])
def test_load_config_resolves_automatic_visit_date_once(
    explicit_auto, write_config, clock, opening, log_events,
):
    path = write_config({"booking": {"visit_date": "auto"}})
    if not explicit_auto:
        path.write_text(path.read_text().replace('visit_date = "auto"\n', ""))
    clock.at = opening - timedelta(seconds=30)

    config = configuration.load_config(path)
    clock.at += timedelta(days=1)

    assert config["booking"]["visit_date"] == "2026-10-29"
    assert rules.release_at(config) == opening
    selected, = log_events("visit_date_selected")
    assert selected["visit_date"] == "2026-10-29"
    assert selected["source"] == "widget_calendar_rule"
    assert selected["date_mode"] == "auto"
    assert "mode" not in selected


def test_load_config_keeps_explicit_date_and_legacy_release_setting(write_config, clock):
    path = write_config({
        "booking": {"visit_date": "2026-11-03"}, "release": {"days_before_visit": 30},
    })

    config = configuration.load_config(path)

    assert config["booking"]["visit_date"] == "2026-11-03"


@pytest.mark.parametrize("phone", ["+7 (999) 123-45-67", "89991234567", "9991234567"])
def test_normalize_phone_accepts_supported_formats(phone):
    assert configuration.normalize_phone(phone) == "+79991234567"


@pytest.mark.parametrize("phone", [
    "+779991234567", "+7999123456", "123", "abc", "+1 999 1234567", "999+1234567",
])
def test_normalize_phone_rejects_invalid_numbers(phone):
    with pytest.raises(ValueError):
        configuration.normalize_phone(phone)


@pytest.mark.parametrize("consents, error", [(False, "consents"), (True, "пример")])
def test_load_config_live_requires_consents_and_real_contacts(
    consents, error, config_path, opening, tmp_path, monkeypatch,
):
    monkeypatch.setattr(rules, "now", lambda: opening)
    source = config_path.read_text()
    if consents:
        source = source.replace("privacy = false", "privacy = true")
        source = source.replace("restaurant_rules = false", "restaurant_rules = true")
    path = tmp_path / "config.toml"
    path.write_text(source)

    with pytest.raises(ValueError, match=error):
        configuration.load_config(path, live=True)


@pytest.mark.parametrize("limit, accepted", [(18999, False), (19000, True), (19001, True)])
def test_load_config_enforces_deposit_limit(limit, accepted, write_config, clock):
    path = write_config({"booking": {"max_deposit_rub": limit}}, live=True)

    if accepted:
        config = configuration.load_config(path, live=True)
        assert rules.deposit(config["booking"]) == 19000
        assert config["booking"]["max_deposit_rub"] == limit
    else:
        with pytest.raises(ValueError, match="max_deposit_rub"):
            configuration.load_config(path, live=True)


@pytest.mark.parametrize("section, key, value, error", [
    ("guest", "first_name", " ", "GUEST_FIRST_NAME"),
    ("guest", "email", "bad-address", "GUEST_EMAIL"),
    ("guest", "telegram", "https://t.me/username", "GUEST_TELEGRAM"),
    ("booking", "visit_date", "2026-09-27", "Дата визита"),
    ("booking", "visit_date", "2026-10-26", "Дата визита"),
    ("booking", "guests", 0, "1–7"),
    ("booking", "guests", 8, "1–7"),
    ("booking", "guests", True, "1–7"),
    ("booking", "time_from", "22:00", "Окно времени"),
    ("booking", "preferred_time", "17:59", "Окно времени"),
    ("booking", "time_to", "25:00", None),
    ("booking", "allowed_rooms", ["Unknown room"], "allowed_rooms"),
    ("booking", "allow_common_table", "false", "allow_common_table"),
    ("release", "timezone", "UTC", "Europe/Moscow"),
    ("release", "days_before_visit", 31, "days_before_visit"),
    ("release", "opens_at", "13:59:59", "14:00"),
    ("release", "poll_interval_ms", 49, "poll_interval_ms"),
    ("release", "max_poll_seconds", 601, "max_poll_seconds"),
    ("release", "prepare_seconds", 9, "prepare_seconds"),
])
def test_load_config_rejects_invalid_settings(section, key, value, error, write_config, clock):
    path = write_config({section: {key: value}})

    with pytest.raises(ValueError, match=error):
        configuration.load_config(path)


@pytest.mark.parametrize("consent", ["privacy", "restaurant_rules"])
def test_load_config_requires_each_live_consent(consent, write_config, clock):
    path = write_config({"consents": {consent: False}}, live=True)

    with pytest.raises(ValueError, match="consents"):
        configuration.load_config(path, live=True)
