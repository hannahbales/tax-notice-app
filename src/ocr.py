"""
OCR split notices with Azure AI Document Intelligence.

Uses the "prebuilt-read" model (text only, the cheapest model). Each
notice's result is saved next to it as notice_001.ocr.json, so a
notice is never sent to Azure (or paid for) twice.

Credentials come from the project's .env file:
    AZURE_DI_ENDPOINT=https://<name>.cognitiveservices.azure.com/
    AZURE_DI_KEY=<key 1>
Optional:
    AZURE_DI_MAX_MB=500   (after moving to the Standard S0 tier)

Manual test:
    python src/ocr.py path\\to\\notice_001.pdf
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)
from dotenv import load_dotenv

from pdf_splitter import MANIFEST_NAME

logger = logging.getLogger(__name__)

# the Azure SDK logs every HTTP request at INFO; keep only problems.
logging.getLogger("azure").setLevel(logging.WARNING)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

MODEL_ID = "prebuilt-read"
OCR_SUFFIX = ".ocr.json"

# Free tier (F0): 4 MB per file, and only the first 2 pages are read.
# Standard tier (S0): 500 MB. Override with AZURE_DI_MAX_MB in .env.
DEFAULT_MAX_MB = 4

# Average word confidence below this suggests a poor scan.
LOW_CONFIDENCE = 0.80


class OcrUnavailable(Exception):
    """
    Azure cannot be used right now: missing or wrong key, monthly
    quota used up, network down, or service busy. Nothing is wrong
    with the notice, so try again later rather than sending it to
    manual review.
    """


class OcrRejected(Exception):
    """Azure refused this particular notice. A person needs to look at it."""


# --- client -------------------------------------------------------

_client = None


def get_client():
    """Create the Azure client once, from the .env settings."""
    global _client

    if _client is None:
        load_dotenv(PROJECT_ROOT / ".env")

        endpoint = os.environ.get("AZURE_DI_ENDPOINT", "").strip()
        key = os.environ.get("AZURE_DI_KEY", "").strip()

        if not endpoint or not key:
            raise OcrUnavailable(
                "AZURE_DI_ENDPOINT or AZURE_DI_KEY is missing - check the .env file"
            )

        # the SDK already retries busy (429) and server (5xx) errors
        # with backoff before raising.
        _client = DocumentIntelligenceClient(endpoint, AzureKeyCredential(key))

    return _client


def _max_bytes():
    return float(os.environ.get("AZURE_DI_MAX_MB", DEFAULT_MAX_MB)) * 1024 * 1024


def _short(message, limit=300):
    message = " ".join(str(message).split())
    return message if len(message) <= limit else message[:limit] + "..."


def analyze(client, stream):
    """
    Send a PDF stream to Azure and return the raw result as a dict.

    Translates Azure errors into OcrUnavailable / OcrRejected.
    """
    try:
        poller = client.begin_analyze_document(MODEL_ID, body=stream)
        return poller.result().as_dict()

    except ClientAuthenticationError as error:
        raise OcrUnavailable(
            f"Azure rejected the key or endpoint: {_short(error.message)}"
        ) from error

    except HttpResponseError as error:
        status = error.status_code

        if status in (400, 413, 415):
            raise OcrRejected(
                f"Azure could not read this file: {_short(error.message)}"
            ) from error

        # 403 is also what the free tier returns when the monthly
        # page quota is used up.
        if status in (401, 403):
            raise OcrUnavailable(
                f"Azure refused access (quota used up or wrong key?): "
                f"{_short(error.message)}"
            ) from error

        if status == 429 or (status or 0) >= 500:
            raise OcrUnavailable(
                f"Azure is busy or unavailable ({status})"
            ) from error

        raise

    except (ServiceRequestError, ServiceResponseError) as error:
        raise OcrUnavailable(f"Cannot reach Azure: {_short(error)}") from error


# --- single notice --------------------------------------------------

def ocr_path(pdf_path):
    """notice_001.pdf -> notice_001.ocr.json"""
    return Path(pdf_path).with_suffix(OCR_SUFFIX)


def summarize(raw, page_count):
    """Quality checks on an Azure result."""
    pages = raw.get("pages") or []
    words = [word for page in pages for word in page.get("words") or []]
    confidences = [word["confidence"] for word in words if "confidence" in word]
    mean_confidence = sum(confidences) / len(confidences) if confidences else None

    warnings = []

    if len(pages) < page_count:
        warnings.append(
            f"only {len(pages)} of {page_count} pages were read "
            f"(the Azure free tier reads the first 2 pages)"
        )

    if not (raw.get("content") or "").strip():
        warnings.append("no text found")

    elif mean_confidence is not None and mean_confidence < LOW_CONFIDENCE:
        warnings.append(
            f"low OCR confidence ({mean_confidence:.0%}) - poor scan quality?"
        )

    return {
        "pages_read": len(pages),
        "word_count": len(words),
        "mean_confidence": (
            round(mean_confidence, 3) if mean_confidence is not None else None
        ),
        "warnings": warnings,
    }


def notice_text(record):
    """Full OCR text of a notice, in reading order."""
    return record["raw"].get("content") or ""


def ocr_notice(pdf_path, client=None):
    """
    OCR one notice PDF and save the result next to it.

    Returns the saved record. A notice that already has a result is
    not sent to Azure again.
    """
    pdf_path = Path(pdf_path)
    output = ocr_path(pdf_path)

    if output.is_file():
        return json.loads(output.read_text(encoding="utf-8"))

    client = client or get_client()

    size = pdf_path.stat().st_size
    if size > _max_bytes():
        raise OcrRejected(
            f"{pdf_path.name} is {size / 1024 / 1024:.1f} MB, over the "
            f"{_max_bytes() / 1024 / 1024:.0f} MB Azure limit for this tier"
        )

    with pymupdf.open(pdf_path) as doc:
        page_count = doc.page_count

    with pdf_path.open("rb") as stream:
        raw = analyze(client, stream)

    record = {
        "source_file": pdf_path.name,
        "model_id": raw.get("modelId", MODEL_ID),
        "api_version": raw.get("apiVersion"),
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "page_count": page_count,
        **summarize(raw, page_count),
        "raw": raw,
    }

    _write_json(output, record)
    return record


def _write_json(path, data):
    """Write JSON so a crash never leaves a half-written file."""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(data, indent=1), encoding="utf-8")
    partial.replace(path)


# --- whole batch ----------------------------------------------------

def ocr_batch(batch_dir):
    """
    OCR every notice in a split batch and record the results in its
    manifest.json.

    Progress is saved after each notice, so after a crash or an Azure
    outage only the remaining notices are sent.

    Raises OcrUnavailable if Azure can't be used. A notice Azure
    refuses is flagged for review and the batch carries on.
    """
    batch_dir = Path(batch_dir)
    manifest_path = batch_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for notice in manifest["notices"]:
        if "ocr" in notice:
            continue  # done in an earlier run

        pdf_path = batch_dir / notice["file"]

        try:
            record = ocr_notice(pdf_path)
            notice["ocr"] = {
                "file": ocr_path(pdf_path).name,
                "pages_read": record["pages_read"],
                "mean_confidence": record["mean_confidence"],
            }
            notice["warnings"] += [f"OCR {w}" for w in record["warnings"]]

        except OcrRejected as error:
            logger.warning("%s / %s: %s", batch_dir.name, notice["file"], error)
            notice["ocr"] = {"file": None, "error": str(error)}
            notice["warnings"].append(f"OCR failed: {error}")

        notice["needs_review"] = bool(notice["warnings"])
        _write_json(manifest_path, manifest)

    logger.info("OCR complete: %s (%s notices)", batch_dir.name, len(manifest["notices"]))
    return manifest


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) != 2:
        sys.exit("usage: python src/ocr.py <notice.pdf>")

    result = ocr_notice(sys.argv[1])
    print(notice_text(result))
    print("-" * 60)
    print(
        f"pages read: {result['pages_read']}/{result['page_count']}   "
        f"words: {result['word_count']}   "
        f"confidence: {result['mean_confidence']}"
    )
    for warning in result["warnings"]:
        print("WARNING:", warning)
