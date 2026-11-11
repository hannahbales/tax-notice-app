
import logging
import os
from datetime import datetime
from time import monotonic, sleep

import pymupdf

import config
from file_utils import copy_verified, file_hash
from pdf_splitter import BatchRejected, find_archive, split_pdf
from processing_history import (
    get_record,
    initialize_database,
    record_status,
)

# configuration

MAX_ATTEMPTS = 3
SCAN_INTERVAL = 5

# the file must stop changing for this long before we try to open it.
STABLE_SECONDS = 5

# a file that has stopped changing but still cannot be opened after
# this long is corrupt. Waiting longer will not fix it. If the scanner
# was only paused, the finished file has a new hash and is processed
# as a new file.
UNREADABLE_GRACE_SECONDS = 30

# safety cap for a file that keeps changing (huge or very slow copy).
MAX_WAIT_SECONDS = 15 * 60

# a finished PDF ends with %%EOF (some scanners add trailing bytes).
EOF_TAIL_BYTES = 2048

logger = logging.getLogger(__name__)

# filename -> ((size, mtime_ns), file_id) for files already identified
# this session, so unchanged files are not re-waited and re-hashed
# every scan cycle.
_known_files = {}

# file_ids that need no more work this session (completed and cleaned
# up, or already sent to manual review). Re-checked against the DB
# each cycle, so a reset via retry_failed.py is still picked up.
_settled = set()


# check if pdf is ready

def _signature(path):
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


def _has_eof_marker(path):
    with path.open("rb") as file:
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(max(0, size - EOF_TAIL_BYTES))
        return b"%%EOF" in file.read()


def _check_complete_pdf(path):
    """Raise if the file is not (yet) a complete, readable PDF."""
    if not _has_eof_marker(path):
        raise ValueError("no %%EOF marker (still being written?)")

    with pymupdf.open(path) as doc:
        # complete but locked: let the splitter reject it with a
        # clear reason instead of waiting for it to "become ready".
        if doc.needs_pass:
            return

        if doc.page_count == 0:
            raise ValueError("PDF contains no pages")


def wait_for_complete_pdf(path):
    """
    Wait for a PDF to become stable and readable.

    Waits as long as the file keeps changing (up to MAX_WAIT_SECONDS).
    Gives up once it has stopped changing but stays unreadable for
    UNREADABLE_GRACE_SECONDS.

    Returns:
        True if the PDF is ready.
        False if it is corrupt / not a PDF.
    """
    start = monotonic()
    previous_signature = None
    stable_since = None
    unreadable_since = None

    while monotonic() - start < MAX_WAIT_SECONDS:
        try:
            signature = _signature(path)

        except OSError as error:
            logger.debug("Waiting for %s: %s", path.name, error)
            stable_since = None
            sleep(1)
            continue

        if signature != previous_signature:
            # still being written: reset every timer.
            previous_signature = signature
            stable_since = None
            unreadable_since = None

        else:
            if stable_since is None:
                stable_since = monotonic()

            if monotonic() - stable_since >= STABLE_SECONDS:
                try:
                    _check_complete_pdf(path)
                    return True

                except Exception as error:
                    if unreadable_since is None:
                        unreadable_since = monotonic()
                        logger.warning(
                            "PDF stopped changing but is unreadable: %s (%s). "
                            "Waiting up to %s s.",
                            path.name,
                            error,
                            UNREADABLE_GRACE_SECONDS,
                        )

                    elif monotonic() - unreadable_since >= UNREADABLE_GRACE_SECONDS:
                        return False

        sleep(1)

    logger.error("%s was still changing after %s s", path.name, MAX_WAIT_SECONDS)
    return False


# manual review and cleanup

def save_for_manual_review(path, file_id, reason):
    """
    Copy a PDF into the manual-review folder with a reason file.

    Keeps the original in intake so retry_failed.py can find it.
    """
    config.MANUAL_REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    review_path = config.MANUAL_REVIEW_DIR / (
        f"{path.stem}__{file_id[:8]}{path.suffix}"
    )

    if review_path.exists():
        if file_hash(review_path) != file_id:
            raise FileExistsError(f"Review filename collision: {review_path}")

        logger.info("Manual-review copy already exists: %s", review_path.name)

    else:
        copy_verified(path, review_path, file_id)
        logger.warning("Saved for manual review: %s", review_path)

    # always rewritten, so it shows the most recent failure.
    review_path.with_suffix(".reason.txt").write_text(
        f"Original file: {path.name}\n"
        f"SHA-256: {file_id}\n"
        f"Sent to review: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"Reason: {reason}\n"
        f"\n"
        f"Once fixed, retry with:\n"
        f'  python src/retry_failed.py --filename "{path.name}"\n',
        encoding="utf-8",
    )

    return review_path


def send_to_review(path, file_id, reason):
    """Save for manual review and never let that crash the watcher."""
    _settled.add(file_id)

    logger.error("MANUAL REVIEW REQUIRED: %s - %s", path.name, reason)

    try:
        save_for_manual_review(path, file_id, reason)
    except Exception:
        logger.exception(
            "CRITICAL: Could not save %s for manual review",
            path.name,
        )


def remove_from_intake(path, file_id):
    """
    Delete a processed batch from intake, but only once a verified
    archive copy exists and the intake file still has the same content.

    If the file has to be left in place, it is marked settled so the
    warning is not repeated every scan cycle.
    """
    try:
        archive = find_archive(file_id)

        if archive is None:
            logger.warning(
                "Leaving %s in intake: no verified archive copy found",
                path.name,
            )
            _settled.add(file_id)
            return False

        if file_hash(path) != file_id:
            logger.warning(
                "Leaving %s in intake: content changed since processing",
                path.name,
            )
            _settled.add(file_id)
            return False

        path.unlink()

    except OSError:
        logger.exception("Could not remove %s from intake", path.name)
        _settled.add(file_id)
        return False

    _known_files.pop(path.name, None)
    logger.info("Removed %s from intake (archived as %s)", path.name, archive.name)
    return True


def handle_unreadable_pdf(path):
    """
    Handle a PDF that never became readable.

    Sends it to manual review once, without retries (the readiness
    wait already gave it time), and remembers it so later scan
    cycles skip it instead of waiting on it again.
    """
    reason = (
        "PDF stopped changing but could not be opened "
        "(corrupt, incomplete, or not a real PDF)"
    )

    try:
        if not path.is_file():
            logger.error("File no longer exists: %s", path.name)
            return

        signature = _signature(path)
        file_id = file_hash(path)

    except OSError:
        logger.exception("Could not read unreadable PDF: %s", path.name)
        return

    _known_files[path.name] = (signature, file_id)

    if file_id in _settled:
        return

    try:
        record = get_record(file_id)
        if record is not None and record[0] == "completed":
            return

        record_status(
            file_id=file_id,
            filename=path.name,
            status="failed",
            attempts=MAX_ATTEMPTS,
            error=reason,
        )

    except Exception:
        logger.exception("Could not record failure for %s", path.name)

    send_to_review(path, file_id, reason)


# process a PDF with automatic retries

def _identify(path):
    """
    Return the content hash of a ready PDF, or None if it is not ready.

    Unchanged files seen earlier this session skip the readiness wait.
    """
    try:
        signature = _signature(path)
    except OSError:
        return None

    cached = _known_files.get(path.name)
    if cached and cached[0] == signature:
        return cached[1]

    # after a restart: files already finished with (completed or sent
    # to review) need no readiness wait.
    try:
        file_id = file_hash(path)
        record = get_record(file_id)
    except OSError:
        record = None  # locked by the scanner; wait below

    if record is not None and (
        record[0] == "completed" or record[1] >= MAX_ATTEMPTS
    ):
        _known_files[path.name] = (signature, file_id)
        return file_id

    # wait for the scanner or file copy to finish.
    if not wait_for_complete_pdf(path):
        handle_unreadable_pdf(path)
        return None

    try:
        signature = _signature(path)
        file_id = file_hash(path)
    except OSError:
        logger.exception("Could not read PDF: %s", path.name)
        return None

    _known_files[path.name] = (signature, file_id)
    return file_id


def _is_candidate(path):
    return (
        path.is_file()
        and path.suffix.lower() == ".pdf"
        # scanner / Office temp files
        and not path.name.startswith(("~", "."))
    )


def process_file(path):
    """Process one PDF and retry failures up to MAX_ATTEMPTS."""

    if not _is_candidate(path):
        return

    file_id = _identify(path)
    if file_id is None:
        return

    # check whether this PDF has been processed before.
    record = get_record(file_id)
    status, previous_attempts = record if record else (None, 0)

    if status == "completed":
        if file_id not in _settled:
            logger.info("Already processed (duplicate copy): %s", path.name)
            remove_from_intake(path, file_id)
        return

    if previous_attempts >= MAX_ATTEMPTS:
        if file_id not in _settled:
            send_to_review(
                path,
                file_id,
                f"Failed {previous_attempts} times - see intake_watcher.log",
            )
        return

    # new, interrupted, or reset via retry_failed.py
    _settled.discard(file_id)
    last_error = None

    # attempt processing, retrying if needed.
    for attempt in range(previous_attempts + 1, MAX_ATTEMPTS + 1):
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

            # archives the original, splits it, and writes a manifest.
            split_pdf(path, file_id)

            record_status(
                file_id=file_id,
                filename=path.name,
                status="completed",
                attempts=attempt,
            )

            logger.info("Successfully processed: %s", path.name)
            remove_from_intake(path, file_id)
            return

        except BatchRejected as error:
            # retrying cannot fix this; go straight to review.
            record_status(
                file_id=file_id,
                filename=path.name,
                status="failed",
                attempts=MAX_ATTEMPTS,
                error=str(error),
            )
            send_to_review(path, file_id, str(error))
            return

        except Exception as error:
            last_error = str(error)

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
                error=last_error,
            )

            if attempt < MAX_ATTEMPTS:
                delay = 5 * attempt
                logger.info("Retrying %s in %s seconds...", path.name, delay)
                sleep(delay)

    send_to_review(
        path,
        file_id,
        f"Failed after {MAX_ATTEMPTS} attempts: {last_error}",
    )


# single running watcher

def acquire_single_instance_lock():
    """
    Return an open lock handle, or None if another watcher is running.

    Two watchers on the same intake folder would process the same
    batch twice. The OS releases the lock if the process dies.
    """
    config.BASE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(config.LOCK_PATH, "a+")

    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    except OSError:
        handle.close()
        return None

    return handle


def configure_logging():
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.LOG_DIR / "intake_watcher.log", encoding="utf-8"),
        ],
    )


# monitor intake folder

def main():
    """Continuously scan the intake folder for PDFs."""
    configure_logging()

    lock = acquire_single_instance_lock()
    if lock is None:
        logger.error("Another intake watcher is already running. Exiting.")
        return

    config.INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    initialize_database()

    logger.info("Monitoring: %s", config.INTAKE_DIR)
    logger.info("Checking for PDFs every %s seconds", SCAN_INTERVAL)
    logger.info("Press Ctrl+C to stop.")

    try:
        while True:
            for path in sorted(config.INTAKE_DIR.iterdir()):
                try:
                    process_file(path)
                except Exception:
                    # one bad file must never stop the watcher.
                    logger.exception("Unexpected error with %s", path.name)

            sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Stopping intake watcher.")

    finally:
        lock.close()


# start application

if __name__ == "__main__":
    main()
