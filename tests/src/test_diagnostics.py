from datetime import datetime, timedelta

import pytest

from src import diagnostics


def test_stage_logs_failure_without_exception_text_and_restores_context(log_events):
    with diagnostics.context(run_id="test-run", stage="outer"):
        with pytest.raises(ValueError):
            with diagnostics.stage("submit", request_id=42):
                raise ValueError("private-contact private-token")
        diagnostics.event("after_failure")

    started, failed, after = log_events()
    assert started["stage"] == failed["stage"] == "submit"
    assert failed["event"] == "stage_failed"
    assert failed["run_id"] == "test-run"
    assert failed["request_id"] == 42
    assert failed["error_type"] == "ValueError"
    assert failed["elapsed_ms"] >= 0
    assert any("test_stage_logs_failure" in location for location in failed["error_locations"])
    assert "private-contact" not in str(log_events())
    assert "private-token" not in str(log_events())
    assert after["stage"] == "outer"
    assert "request_id" not in after
    assert datetime.fromisoformat(failed["timestamp"]).utcoffset() == timedelta(0)


def test_event_escapes_newlines_in_a_single_json_record(caplog, log_events):
    diagnostics.event("example", message="line one\nline two")

    assert log_events()[0]["message"] == "line one\nline two"
    assert "\n" not in caplog.records[-1].getMessage()
