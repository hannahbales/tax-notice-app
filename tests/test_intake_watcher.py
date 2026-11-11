import os

import pytest

import config
import intake_watcher
from file_utils import file_hash
from generate_test_batch import build_batch
from processing_history import get_record, initialize_database


@pytest.fixture(autouse=True)
def fast_watcher(monkeypatch):
    initialize_database()
    monkeypatch.setattr(intake_watcher, "STABLE_SECONDS", 0)
    monkeypatch.setattr(intake_watcher, "UNREADABLE_GRACE_SECONDS", 0)
    monkeypatch.setattr(intake_watcher, "MAX_WAIT_SECONDS", 3)
    monkeypatch.setattr(intake_watcher, "sleep", lambda _seconds: None)
    intake_watcher._known_files.clear()
    intake_watcher._settled.clear()


def _drop_in_intake(batch_name, filename=None):
    path = config.INTAKE_DIR / (filename or f"{batch_name}.pdf")
    with build_batch(batch_name) as doc:
        doc.save(path)
    return path


def test_successful_batch_is_archived_split_and_removed_from_intake():
    path = _drop_in_intake("batch_01_standard")
    file_id = file_hash(path)

    intake_watcher.process_file(path)

    assert get_record(file_id) == ("completed", 1)
    assert not path.exists()
    assert len(list(config.ARCHIVE_DIR.glob("*.pdf"))) == 1
    assert len(list(config.SPLIT_DIR.glob("*/notice_*.pdf"))) == 3


def test_rejected_batch_goes_straight_to_review(monkeypatch):
    path = _drop_in_intake("batch_07_blank_only")
    file_id = file_hash(path)

    calls = []
    real_split = intake_watcher.split_pdf
    monkeypatch.setattr(
        intake_watcher, "split_pdf", lambda *a: calls.append(a) or real_split(*a)
    )

    intake_watcher.process_file(path)

    assert len(calls) == 1  # no pointless retries
    status, attempts = get_record(file_id)
    assert status == "failed" and attempts == intake_watcher.MAX_ATTEMPTS

    review = list(config.MANUAL_REVIEW_DIR.glob("*.pdf"))
    assert len(review) == 1 and file_hash(review[0]) == file_id
    reason = review[0].with_suffix(".reason.txt").read_text()
    assert "No notice pages" in reason

    assert path.exists()  # kept in intake for retry_failed.py


def test_unchanged_files_are_not_rewaited_every_cycle(monkeypatch):
    path = _drop_in_intake("batch_07_blank_only")
    intake_watcher.process_file(path)

    def fail(_path):
        raise AssertionError("should not wait again for an unchanged file")

    monkeypatch.setattr(intake_watcher, "wait_for_complete_pdf", fail)
    intake_watcher.process_file(path)


def test_truncated_pdf_is_not_ready():
    path = _drop_in_intake("batch_01_standard")
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])  # scanner still writing

    assert intake_watcher.wait_for_complete_pdf(path) is False


def _corrupt_pdf():
    path = config.INTAKE_DIR / "corrupt.pdf"
    path.write_bytes(b"%PDF-1.7\nthis is not really a pdf\n%%EOF\n")
    return path


def test_corrupt_pdf_goes_to_review_once_without_retries(monkeypatch):
    path = _corrupt_pdf()
    file_id = file_hash(path)

    waits = []
    real_wait = intake_watcher.wait_for_complete_pdf
    monkeypatch.setattr(
        intake_watcher,
        "wait_for_complete_pdf",
        lambda p: waits.append(p) or real_wait(p),
    )

    for _cycle in range(3):
        intake_watcher.process_file(path)

    assert len(waits) == 1  # later scan cycles skip it
    assert get_record(file_id)[0] == "failed"
    assert len(list(config.MANUAL_REVIEW_DIR.glob("*.pdf"))) == 1
    assert path.exists()


def test_known_corrupt_pdf_is_skipped_after_restart(monkeypatch):
    path = _corrupt_pdf()
    intake_watcher.process_file(path)

    # watcher restarted: in-memory caches are empty
    intake_watcher._known_files.clear()
    intake_watcher._settled.clear()

    def fail(_path):
        raise AssertionError("should not wait on a file already sent to review")

    monkeypatch.setattr(intake_watcher, "wait_for_complete_pdf", fail)
    intake_watcher.process_file(path)


def test_file_still_being_written_is_not_given_up_on(monkeypatch):
    path = _drop_in_intake("batch_01_standard")
    data = path.read_bytes()
    path.write_bytes(data[:1000])  # partial file, unreadable so far

    checks = []
    real_check = intake_watcher._check_complete_pdf

    def scanner_finishes(p):
        checks.append(p)
        if len(checks) == 1:
            path.write_bytes(data)  # the file changes: timers reset
        return real_check(p)

    monkeypatch.setattr(intake_watcher, "_check_complete_pdf", scanner_finishes)

    assert intake_watcher.wait_for_complete_pdf(path) is True


def test_duplicate_dropped_in_same_session_is_removed_without_reprocessing(monkeypatch):
    path = _drop_in_intake("batch_01_standard")
    intake_watcher.process_file(path)
    assert not path.exists()

    # someone drops the same scan in again while the watcher is running
    archive = next(config.ARCHIVE_DIR.glob("*.pdf"))
    duplicate = config.INTAKE_DIR / "copy_of_batch.pdf"
    duplicate.write_bytes(archive.read_bytes())

    def fail(*_args):
        raise AssertionError("duplicate must not be split again")

    monkeypatch.setattr(intake_watcher, "split_pdf", fail)
    intake_watcher.process_file(duplicate)

    assert not duplicate.exists()
    assert len(list(config.SPLIT_DIR.iterdir())) == 1


def test_intake_file_is_kept_if_content_changed():
    path = _drop_in_intake("batch_01_standard")
    file_id = file_hash(path)
    intake_watcher.process_file(path)

    # scanner reuses the same filename for a new batch
    _drop_in_intake("batch_02_single", filename=path.name)
    intake_watcher._settled.clear()

    assert intake_watcher.remove_from_intake(path, file_id) is False
    assert path.exists()


def test_only_one_watcher_can_run():
    first = intake_watcher.acquire_single_instance_lock()
    try:
        assert first is not None
        # a second handle in the same process stands in for a second watcher
        if os.name == "nt":
            assert intake_watcher.acquire_single_instance_lock() is None
    finally:
        first.close()
