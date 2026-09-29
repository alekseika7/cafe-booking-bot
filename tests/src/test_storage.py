import errno

import pytest

from src import storage


def test_exclusive_lock_releases_after_exception(tmp_path):
    with pytest.raises(RuntimeError, match="interrupted"):
        with storage.exclusive_lock(tmp_path):
            raise RuntimeError("interrupted")

    with storage.exclusive_lock(tmp_path):
        assert (tmp_path / ".lock").exists()


@pytest.mark.parametrize("stage", ["write", "file_fsync", "replace", "directory_fsync"])
def test_save_state_never_leaves_partial_json_after_io_failure(stage, state_path, fail_state_io, log_events):
    previous = {"status": "SUBMITTING"}
    updated = {"status": "AWAITING_PAYMENT", "payment_url": "https://payments.example/order"}
    storage.save_state(state_path, previous)
    fail_state_io(stage)

    with pytest.raises(OSError):
        storage.save_state(state_path, updated)

    assert storage.read_state(state_path) == (updated if stage == "directory_fsync" else previous)
    assert state_path.stat().st_mode & 0o777 == 0o600
    failure, = log_events("state_save_failed")
    assert failure["operation"] == ("open_temporary" if stage == "write" else stage)
    assert failure["errno"] == errno.ENOSPC
    assert failure["state_file"] == str(state_path)
    assert updated["payment_url"] not in str(log_events())
