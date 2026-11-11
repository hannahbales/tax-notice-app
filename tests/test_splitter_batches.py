"""Every synthetic batch in scripts/generate_test_batch.py must split as declared."""

import json

import pymupdf
import pytest

import config
from file_utils import file_hash
from generate_test_batch import BATCHES, build_batch
from pdf_splitter import BatchRejected, split_pdf


def _write_batch(name):
    path = config.INTAKE_DIR / f"{name}.pdf"
    with build_batch(name) as doc:
        doc.save(path)
    return path


@pytest.mark.parametrize("name", list(BATCHES))
def test_batch_splits_as_expected(name):
    expected = BATCHES[name].expected
    path = _write_batch(name)

    if expected.rejected:
        with pytest.raises(BatchRejected):
            split_pdf(path)
        # nothing half-written is left behind
        assert not any(config.SPLIT_DIR.iterdir())
        return

    manifest = split_pdf(path)
    pages = {p["page"]: p for p in manifest["pages"]}
    debug = json.dumps(manifest["pages"], indent=1)

    assert [n["page_count"] for n in manifest["notices"]] == expected.notice_pages, debug
    assert bool(manifest["warnings"]) == expected.batch_warning, manifest["warnings"]

    flagged = tuple(i for i, n in enumerate(manifest["notices"], 1) if n["needs_review"])
    assert flagged == expected.review_notices, manifest["notices"]

    # every source page lands in exactly one place
    used = [p for n in manifest["notices"] for p in n["source_pages"]]
    assert len(used) == len(set(used))
    assert all(pages[p]["kind"] == "content" for p in used)

    # output files exist and have the declared page counts
    output_dir = config.SPLIT_DIR / next(config.ARCHIVE_DIR.glob("*.pdf")).stem
    for notice in manifest["notices"]:
        with pymupdf.open(output_dir / notice["file"]) as doc:
            assert doc.page_count == notice["page_count"]


def test_archive_is_verified_and_rerun_is_idempotent():
    path = _write_batch("batch_01_standard")
    file_id = file_hash(path)

    first = split_pdf(path, file_id)
    second = split_pdf(path, file_id)

    archives = list(config.ARCHIVE_DIR.glob("*.pdf"))
    assert len(archives) == 1
    assert file_hash(archives[0]) == file_id
    assert file_id[:12] in archives[0].name

    assert first == second
    assert len(list(config.SPLIT_DIR.iterdir())) == 1


def test_interrupted_run_leftovers_are_replaced():
    path = _write_batch("batch_01_standard")
    file_id = file_hash(path)

    # simulate a crash: archive exists, partial output folder left behind
    from pdf_splitter import archive_original

    archive = archive_original(path, file_id)
    partial = config.SPLIT_DIR / (archive.stem + ".partial")
    partial.mkdir(parents=True)
    (partial / "notice_001.pdf").write_bytes(b"garbage")

    manifest = split_pdf(path, file_id)

    assert len(manifest["notices"]) == 3
    assert not partial.exists()


def test_password_protected_pdf_is_rejected():
    path = config.INTAKE_DIR / "locked.pdf"
    with build_batch("batch_01_standard") as doc:
        doc.save(
            path,
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            user_pw="secret",
            owner_pw="secret",
        )

    with pytest.raises(BatchRejected, match="password"):
        split_pdf(path)
