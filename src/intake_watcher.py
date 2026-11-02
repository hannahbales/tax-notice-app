
from importlib.resources import path
from pathlib import Path
from time import monotonic, sleep
import hashlib
import logging
import shutil
from pypdf import PdfReader

from pdf_splitter import split_pdf
from processing_history import (
    initialize_database,
    get_record,
    record_status,
)

# configuration

BASE_DIR = Path(r"C:\TTI-Notice-Dev")
INTAKE_DIR = BASE_DIR / "intake"
MANUAL_REVIEW_DIR = BASE_DIR / "manual-review"

MAX_ATTEMPTS = 3
SCAN_INTERVAL = 5
STABLE_SECONDS = 5
PDF_READY_TIMEOUT = 120
READINESS_RETRY_DELAY = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

logger = logging.getLogger(__name__)


# file identification

def file_hash(path):
    """Generate a SHA-256 hash to identify PDF content."""
    sha256 = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(
            lambda: file.read(1024 * 1024),
            b"",
        ):
            sha256.update(chunk)

    return sha256.hexdigest()


# check if pdf is ready

def wait_for_complete_pdf(path):
    """
    Wait for a PDF to become stable and readable.

    Returns:
        True if the PDF is ready.
        False if it remains unreadable after the timeout.
    """
    start = monotonic()
    previous_signature = None
    stable_since = None

    while monotonic() - start < PDF_READY_TIMEOUT:
        try:
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)

            if stat.st_size == 0:
                stable_since = None

            elif signature != previous_signature:
                previous_signature = signature
                stable_since = None

            else:
                if stable_since is None:
                    stable_since = monotonic()

                if monotonic() - stable_since >= STABLE_SECONDS:
                    try:
                        reader = PdfReader(path, strict=True)

                        if len(reader.pages) == 0:
                            raise ValueError("PDF contains no pages")

                        return True

                    except Exception as error:
                        logger.warning(
                            "PDF is stable but unreadable: %s (%s)",
                            path.name,
                            error,
                        )

                        # continue waiting in case the
                        # scanner is still finishing.
                        stable_since = monotonic()

        except OSError as error:
            logger.debug(
                "Waiting for %s: %s",
                path.name,
                error,
            )
            stable_since = None

        sleep(1)

    return False


def save_for_manual_review(path, file_id):
    """
    Copy a failed PDF into the manual-review folder.

    Keeps the original in intake so the existing
    manual retry command can find it.
    """
    MANUAL_REVIEW_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    review_filename = (
        f"{path.stem}__{file_id[:8]}{path.suffix}"
    )

    review_path = MANUAL_REVIEW_DIR / review_filename

    # do not overwrite an existing review copy.
    if review_path.exists():
        if file_hash(review_path) == file_id:
            logger.info(
                "Manual-review copy already exists: %s",
                review_path.name,
            )
            return review_path

        raise FileExistsError(
            f"Review filename collision: {review_path}"
        )

    # exclusive creation avoids silently overwriting files.
    with path.open("rb") as source:
        with review_path.open("xb") as destination:
            shutil.copyfileobj(source, destination)

    # confirm the saved copy matches the source PDF.
    if file_hash(review_path) != file_id:
        raise RuntimeError(
            f"Manual-review copy failed integrity check: {review_path}"
        )

    logger.warning(
        "Saved for manual review: %s",
        review_path,
    )

    return review_path

def handle_unreadable_pdf(path):
    """
    Handle a PDF that never became readable.

    Records the failure and preserves the file
    for manual review when possible.
    """

    logger.error(
        "PDF readiness timeout: %s",
        path.name,
    )

    try:
        if not path.is_file():
            logger.error(
                "File no longer exists: %s",
                path.name,
            )
            return

        file_id = file_hash(path)

        record = get_record(file_id)

        if record is not None:
            status, previous_attempts = record

            if status == "completed":
                return

            if previous_attempts >= MAX_ATTEMPTS:
                save_for_manual_review(path, file_id)
                return

            start_attempt = previous_attempts + 1

        else:
            start_attempt = 1

        for attempt in range(
            start_attempt,
            MAX_ATTEMPTS + 1,
        ):
            logger.warning(
                "Readiness failure for %s (attempt %s/%s)",
                path.name,
                attempt,
                MAX_ATTEMPTS,
            )

            record_status(
                file_id=file_id,
                filename=path.name,
                status="failed",
                attempts=attempt,
                error="PDF did not become readable",
            )

            if attempt < MAX_ATTEMPTS:
                sleep(READINESS_RETRY_DELAY)

                # check whether it became readable.
                if wait_for_complete_pdf(path):
                    logger.info(
                        "PDF became readable: %s",
                        path.name,
                    )

                    # resume normal processing.
                    process_file(path)
                    return

        save_for_manual_review(path, file_id)

        logger.error(
            "MANUAL REVIEW REQUIRED: unreadable PDF %s",
            path.name,
        )

    except Exception:
        logger.exception(
            "Could not preserve unreadable PDF: %s",
            path.name,
        )

# process a PDF with automatic retries

def process_file(path):
    """Process one PDF and retry failures up to MAX_ATTEMPTS."""

    if not path.is_file():
        return

    if path.suffix.lower() != ".pdf":
        return

    # wait for the scanner or file copy to finish.
    if not wait_for_complete_pdf(path):
        handle_unreadable_pdf(path)
        return

    # identify the PDF by its content.
    try:
        file_id = file_hash(path)
    except OSError:
        logger.exception(
            "Could not read PDF: %s",
            path.name,
        )
        return


    # check whether this PDF has been processed before.
    record = get_record(file_id)

    if record is not None:
        status, previous_attempts = record

        if status == "completed":
            logger.debug(
                "SKIPPED: %s has already been processed.",
                path.name,
            )
            return

        if previous_attempts >= MAX_ATTEMPTS:
            logger.debug(
                "SKIPPED: %s has already failed %s times.",
                path.name,
                previous_attempts,
            )

            if status == "failed":
                try:
                    save_for_manual_review(path, file_id)
                except Exception:
                    logger.exception(
                        "Could not preserve failed PDF: %s",
                        path.name,
                    )

            return

        # resume from the next attempt.
        start_attempt = previous_attempts + 1

    else:
        # new PDF with no processing history.
        start_attempt = 1


    # attempt processing, retrying if needed.
    for attempt in range(start_attempt, MAX_ATTEMPTS + 1):

        try:
            logger.info(
                "Processing %s (attempt %s/%s)",
                path.name,
                attempt,
                MAX_ATTEMPTS,
            )

            record_status(
                file_id=file_id,
                filename=path.name,
                status="processing",
                attempts=attempt,
            )

            # existing splitter handles:
            # 1. archiving the original batch
            # 2.dDetecting blank separator pages
            # 3. saving individual notices
            split_pdf(path)

            record_status(
                file_id=file_id,
                filename=path.name,
                status="completed",
                attempts=attempt,
            )

            logger.info(
                "Successfully processed: %s",
                path.name,
            )

            return

        except Exception as error:
            logger.exception(
                "Attempt %s/%s failed for %s",
                attempt,
                MAX_ATTEMPTS,
                path.name,
            )

            record_status(
                file_id=file_id,
                filename=path.name,
                status="failed",
                attempts=attempt,
                error=str(error),
            )

            if attempt < MAX_ATTEMPTS:
                delay = 5 * attempt

                logger.info(
                    "Retrying %s in %s seconds...",
                    path.name,
                    delay,
                )

                sleep(delay)

    logger.error(
        "MANUAL REVIEW REQUIRED: %s failed after %s attempts",
        path.name,
        MAX_ATTEMPTS,
    )

    try:
        review_path = save_for_manual_review(path, file_id)

        logger.warning(
            "Failed PDF available at: %s",
            review_path,
        )

    except Exception:
        logger.exception(
            "CRITICAL: Could not save %s for manual review",
            path.name,
        )


# monitor intake folder

def main():
    """Continuously scan the intake folder for PDFs."""

    INTAKE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    initialize_database()

    logger.info(
        "Monitoring: %s",
        INTAKE_DIR,
    )

    logger.info(
        "Checking for PDFs every %s seconds",
        SCAN_INTERVAL,
    )

    logger.info(
        "Press Ctrl+C to stop.",
    )

    try:
        while True:

            for path in sorted(INTAKE_DIR.iterdir()):

                if (
                    path.is_file()
                    and path.suffix.lower() == ".pdf"
                ):
                    process_file(path)

            sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Stopping intake watcher.")


# start application

if __name__ == "__main__":
    main()
