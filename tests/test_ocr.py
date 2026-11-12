import json

import pytest
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError

import config
import intake_watcher
import ocr
from file_utils import file_hash
from generate_test_batch import build_batch
from pdf_splitter import batch_dir, split_pdf
from processing_history import get_record, initialize_database


def _split(batch_name):
    path = config.INTAKE_DIR / f"{batch_name}.pdf"
    with build_batch(batch_name) as doc:
        doc.save(path)
    return batch_dir(split_pdf(path))


def _http_error(status, message="error"):
    error = HttpResponseError(message=message)
    error.status_code = status
    return error


def _manifest(folder):
    return json.loads((folder / "manifest.json").read_text())


# --- single notice ---------------------------------------------------

def test_notice_result_is_saved_and_never_sent_twice(fake_azure):
    folder = _split("batch_01_standard")
    pdf = folder / "notice_001.pdf"

    first = ocr.ocr_notice(pdf)
    second = ocr.ocr_notice(pdf)

    assert fake_azure.calls == 1
    assert first == second
    assert (folder / "notice_001.ocr.json").is_file()
    assert "John Smith" in ocr.notice_text(first)
    assert first["pages_read"] == 2 and first["warnings"] == []


def test_free_tier_two_page_limit_is_flagged(fake_azure):
    fake_azure.max_pages = 2
    folder = _split("batch_03_multipage")  # 3-page notices

    record = ocr.ocr_notice(folder / "notice_001.pdf")

    assert record["pages_read"] == 2
    assert "only 2 of 3 pages" in record["warnings"][0]


def test_low_confidence_is_flagged(fake_azure):
    fake_azure.confidence = 0.5
    folder = _split("batch_01_standard")

    record = ocr.ocr_notice(folder / "notice_002.pdf")

    assert "low OCR confidence" in record["warnings"][0]


def test_oversized_file_is_rejected_without_calling_azure(fake_azure, monkeypatch):
    monkeypatch.setenv("AZURE_DI_MAX_MB", "0.0001")
    folder = _split("batch_01_standard")

    with pytest.raises(ocr.OcrRejected, match="MB"):
        ocr.ocr_notice(folder / "notice_001.pdf")

    assert fake_azure.calls == 0


@pytest.mark.parametrize(
    "error, expected",
    [
        (ClientAuthenticationError(message="bad key"), ocr.OcrUnavailable),
        (_http_error(403, "Out of call volume quota"), ocr.OcrUnavailable),
        (_http_error(429), ocr.OcrUnavailable),
        (_http_error(503), ocr.OcrUnavailable),
        (_http_error(400, "InvalidContent"), ocr.OcrRejected),
    ],
)
def test_azure_errors_are_classified(fake_azure, error, expected):
    fake_azure.error = error
    folder = _split("batch_01_standard")

    with pytest.raises(expected):
        ocr.ocr_notice(folder / "notice_001.pdf")

    assert not (folder / "notice_001.ocr.json").exists()


def test_missing_credentials_are_reported(monkeypatch):
    monkeypatch.undo()  # use the real get_client
    monkeypatch.setattr(ocr, "_client", None)
    monkeypatch.setattr(ocr, "load_dotenv", lambda *_: None)
    monkeypatch.delenv("AZURE_DI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_DI_KEY", raising=False)

    with pytest.raises(ocr.OcrUnavailable, match=".env"):
        ocr.get_client()


# --- whole batch -----------------------------------------------------

def test_batch_ocr_updates_manifest(fake_azure):
    folder = _split("batch_04_large")

    ocr.ocr_batch(folder)

    notices = _manifest(folder)["notices"]
    assert fake_azure.calls == 5
    assert all(n["ocr"]["file"] == n["file"].replace(".pdf", ".ocr.json") for n in notices)
    assert not any(n["needs_review"] for n in notices)


def test_rejected_notice_is_flagged_and_batch_continues(fake_azure, monkeypatch):
    folder = _split("batch_01_standard")
    real = fake_azure.begin_analyze_document

    def reject_second(model_id, body):
        if fake_azure.calls == 1:
            fake_azure.calls += 1
            raise _http_error(400, "InvalidContent")
        return real(model_id, body)

    monkeypatch.setattr(fake_azure, "begin_analyze_document", reject_second)

    ocr.ocr_batch(folder)

    notices = _manifest(folder)["notices"]
    assert [n["needs_review"] for n in notices] == [False, True, False]
    assert "OCR failed" in notices[1]["warnings"][0]


def test_batch_resumes_after_outage(fake_azure, monkeypatch):
    folder = _split("batch_01_standard")
    real = fake_azure.begin_analyze_document

    def outage_on_third(model_id, body):
        if fake_azure.calls == 2:
            raise _http_error(503)
        return real(model_id, body)

    monkeypatch.setattr(fake_azure, "begin_analyze_document", outage_on_third)

    with pytest.raises(ocr.OcrUnavailable):
        ocr.ocr_batch(folder)

    assert [("ocr" in n) for n in _manifest(folder)["notices"]] == [True, True, False]

    monkeypatch.setattr(fake_azure, "begin_analyze_document", real)
    ocr.ocr_batch(folder)

    assert fake_azure.calls == 3  # only the missing notice was sent again


# --- watcher ---------------------------------------------------------

@pytest.fixture
def watcher(monkeypatch):
    initialize_database()
    monkeypatch.setattr(intake_watcher, "STABLE_SECONDS", 0)
    monkeypatch.setattr(intake_watcher, "sleep", lambda _seconds: None)
    monkeypatch.setattr(intake_watcher, "_paused_until", 0.0)
    intake_watcher._known_files.clear()
    intake_watcher._settled.clear()
    return intake_watcher


def test_watcher_ocrs_every_notice(watcher, fake_azure):
    path = config.INTAKE_DIR / "batch.pdf"
    with build_batch("batch_01_standard") as doc:
        doc.save(path)

    watcher.process_file(path)

    assert fake_azure.calls == 3
    assert len(list(config.SPLIT_DIR.glob("*/notice_*.ocr.json"))) == 3
    assert not path.exists()


def test_watcher_waits_out_azure_outage_without_using_attempts(watcher, fake_azure):
    path = config.INTAKE_DIR / "batch.pdf"
    with build_batch("batch_01_standard") as doc:
        doc.save(path)
    file_id = file_hash(path)

    fake_azure.error = ClientAuthenticationError(message="bad key")
    watcher.process_file(path)

    assert get_record(file_id) == ("waiting", 0)
    assert path.exists()
    assert not config.MANUAL_REVIEW_DIR.exists()

    # while paused, nothing is attempted
    fake_azure.error = None
    watcher.process_file(path)
    assert get_record(file_id)[0] == "waiting"

    # after the pause the batch finishes, reusing the existing split
    watcher._paused_until = 0.0
    watcher.process_file(path)

    assert get_record(file_id) == ("completed", 1)
    assert len(list(config.SPLIT_DIR.iterdir())) == 1
