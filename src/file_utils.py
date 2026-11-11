"""Shared file helpers: content hashing and verified copies."""

import hashlib
import os
import shutil
from pathlib import Path


def file_hash(path):
    """Generate a SHA-256 hash to identify PDF content."""
    sha256 = hashlib.sha256()

    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            sha256.update(chunk)

    return sha256.hexdigest()


def copy_verified(source, destination, expected_hash):
    """
    Copy source to destination and prove the copy is intact.

    Writes to a temporary ".partial" file first, so a crash never
    leaves a half-written file under the final name. Never
    overwrites an existing destination.
    """
    source = Path(source)
    destination = Path(destination)

    if destination.exists():
        raise FileExistsError(f"Destination already exists: {destination}")

    partial = destination.with_name(destination.name + ".partial")

    # leftover from an interrupted copy; it is our own temp file.
    partial.unlink(missing_ok=True)

    try:
        with source.open("rb") as src, partial.open("xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())

        if file_hash(partial) != expected_hash:
            raise RuntimeError(
                f"Copy failed integrity check (source changed or "
                f"disk error): {source} -> {destination}"
            )

        partial.rename(destination)

    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    return destination
