"""
Split a scanned batch PDF into individual notice PDFs.

Flow:
  1. Archive the original batch (hash-verified, once per unique file).
  2. Classify every page of the *archived* copy: separator / blank /
     content. (The intake file is never read again; it could change.)
  3. Group content pages between separator sheets into notices.
     Blank pages are dropped (duplex back sides) and never split.
  4. Write notices plus a manifest.json into a temporary folder, verify,
     then rename the folder into place. A crash never leaves a
     half-finished output folder that looks complete.

Re-running on a file that was already split returns the existing
manifest instead of creating duplicates.
"""

import json
import logging
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

import config
from file_utils import copy_verified, file_hash
from page_classifier import BLANK, CONTENT, SEPARATOR, classify_page

logger = logging.getLogger(__name__)

# A notice longer than this is probably two notices with a
# missing or unreadable separator sheet between them.
MAX_NOTICE_PAGES = 15

MANIFEST_NAME = "manifest.json"


class BatchRejected(Exception):
    """
    The batch cannot be split automatically and needs a person.

    Retrying will not help (e.g. encrypted PDF, nothing but blank
    pages), so the watcher sends it straight to manual review.
    """


# --- archiving -------------------------------------------------------

def _safe_filename(name):
    """Keep scanner filenames but strip characters Windows rejects."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)


def find_archive(file_id):
    """Return the archived copy of this exact content, if any."""
    if not config.ARCHIVE_DIR.is_dir():
        return None

    tag = file_id[:12]

    for candidate in sorted(config.ARCHIVE_DIR.glob(f"*_{tag}_*.pdf")):
        if file_hash(candidate) == file_id:
            return candidate

    return None


def archive_original(input_file, file_id):
    """
    Save a hash-verified copy of the original batch.

    Name: <YYYYMMDD_HHMMSS>_<first 12 of SHA-256>_<original name>
    Archiving the same content twice returns the existing copy.
    """
    existing = find_archive(file_id)
    if existing:
        logger.info("Original batch already archived: %s", existing.name)
        return existing

    config.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = config.ARCHIVE_DIR / (
        f"{timestamp}_{file_id[:12]}_{_safe_filename(Path(input_file).name)}"
    )

    copy_verified(input_file, archive_path, file_id)
    logger.info("Original batch archived: %s", archive_path.name)

    return archive_path


# --- splitting -------------------------------------------------------

def group_into_notices(pages):
    """
    Group classified pages into notices.

    Separators close the current notice. Blank pages are skipped.
    Repeated separators, or separators at the start or end, are harmless.
    """
    notices = []
    current = []

    for page in pages:
        if page.kind == SEPARATOR:
            if current:
                notices.append(current)
                current = []

        elif page.kind == CONTENT:
            current.append(page)

    if current:
        notices.append(current)

    return notices


def write_notice(doc, page_indices, output_path):
    """Copy the given pages (original quality) into a new PDF."""
    with pymupdf.open() as notice:
        for index in page_indices:
            notice.insert_pdf(doc, from_page=index, to_page=index)

        notice.save(output_path, garbage=3, deflate=True)

    with pymupdf.open(output_path) as check:
        if check.page_count != len(page_indices):
            raise RuntimeError(
                f"{output_path.name}: wrote {check.page_count} pages, "
                f"expected {len(page_indices)}"
            )


def _notice_warnings(group):
    warnings = []

    if len(group) > MAX_NOTICE_PAGES:
        warnings.append(
            f"{len(group)} pages - unusually long; a separator sheet "
            f"may be missing"
        )

    for page in group:
        for note in page.notes:
            warnings.append(f"source page {page.index + 1} {note}")

    return warnings


def _load_existing_manifest(output_dir, file_id):
    manifest_path = output_dir / MANIFEST_NAME

    if not manifest_path.is_file():
        raise RuntimeError(
            f"Output folder exists without a manifest: {output_dir}"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("source_sha256") != file_id:
        raise RuntimeError(f"Output folder belongs to another batch: {output_dir}")

    return manifest


def split_pdf(input_file, file_id=None):
    """
    Archive and split one batch PDF.

    Returns the manifest dict. Raises BatchRejected for batches that
    need a person, and other exceptions for retryable failures.
    """
    input_file = Path(input_file)

    if not input_file.is_file():
        raise FileNotFoundError(input_file)

    if file_id is None:
        file_id = file_hash(input_file)

    archive_path = archive_original(input_file, file_id)

    output_dir = config.SPLIT_DIR / archive_path.stem

    if output_dir.exists():
        logger.info("Batch already split: %s", output_dir.name)
        return _load_existing_manifest(output_dir, file_id)

    partial_dir = output_dir.with_name(output_dir.name + ".partial")

    # leftover from an interrupted run; it only holds our own output.
    if partial_dir.exists():
        shutil.rmtree(partial_dir)

    partial_dir.mkdir(parents=True)

    try:
        manifest = _split_into(archive_path, partial_dir, input_file.name, file_id)
        partial_dir.rename(output_dir)

    except BaseException:
        shutil.rmtree(partial_dir, ignore_errors=True)
        raise

    logger.info(
        "Split %s into %s notice(s): %s",
        input_file.name,
        len(manifest["notices"]),
        output_dir,
    )

    for warning in manifest["warnings"]:
        logger.warning("%s: %s", input_file.name, warning)

    for notice in manifest["notices"]:
        for warning in notice["warnings"]:
            logger.warning("%s / %s: %s", input_file.name, notice["file"], warning)

    return manifest


def _split_into(archive_path, output_dir, source_name, file_id):
    try:
        doc = pymupdf.open(archive_path)
    except Exception as error:
        raise BatchRejected(f"PDF cannot be opened: {error}") from error

    with doc:
        if doc.needs_pass:
            raise BatchRejected("PDF is password-protected")

        if doc.page_count == 0:
            raise BatchRejected("PDF has no pages")

        pages = []

        for index in range(doc.page_count):
            info = classify_page(doc[index], index)
            logger.debug(
                "page %s: %s (ink %.4f%%)",
                index + 1,
                info.kind,
                info.ink_ratio * 100,
            )
            pages.append(info)

        groups = group_into_notices(pages)

        if not groups:
            raise BatchRejected(
                "No notice pages found - batch is blank or only separator sheets"
            )

        notices = []

        for number, group in enumerate(groups, start=1):
            filename = f"notice_{number:03d}.pdf"
            indices = [page.index for page in group]

            write_notice(doc, indices, output_dir / filename)

            warnings = _notice_warnings(group)
            notices.append(
                {
                    "file": filename,
                    "source_pages": [index + 1 for index in indices],
                    "page_count": len(indices),
                    "needs_review": bool(warnings),
                    "warnings": warnings,
                }
            )

    counts = {kind: 0 for kind in (CONTENT, SEPARATOR, BLANK)}
    for page in pages:
        counts[page.kind] += 1

    # every source page is accounted for exactly once.
    written = sum(notice["page_count"] for notice in notices)
    if written != counts[CONTENT]:
        raise RuntimeError(
            f"Page accounting mismatch: {len(pages)} pages, "
            f"{written} written, counts={counts}"
        )

    batch_warnings = []

    if counts[SEPARATOR] == 0 and counts[CONTENT] > 1:
        batch_warnings.append(
            "No separator sheets found - the whole batch was saved as one notice"
        )

    manifest = {
        "source_filename": source_name,
        "source_sha256": file_id,
        "archive_file": archive_path.name,
        "split_at": datetime.now(timezone.utc).isoformat(),
        "page_count": len(pages),
        "separator_pages": counts[SEPARATOR],
        "blank_pages_dropped": [p.index + 1 for p in pages if p.kind == BLANK],
        "warnings": batch_warnings,
        "notices": notices,
        "pages": [
            {
                "page": page.index + 1,
                "kind": page.kind,
                "ink_percent": round(page.ink_ratio * 100, 4),
                "notes": page.notes,
            }
            for page in pages
        ],
    }

    (output_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    return manifest


if __name__ == "__main__":
    # manual test: python src/pdf_splitter.py path\to\batch.pdf
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    if len(sys.argv) != 2:
        sys.exit("usage: python pdf_splitter.py <batch.pdf>")

    result = split_pdf(sys.argv[1])
    for item in result["notices"]:
        print(item["file"], item["source_pages"], item["warnings"] or "")
