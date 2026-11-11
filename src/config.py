"""
Central folder configuration.

Set the TTI_BASE_DIR environment variable to point the app at a
different root folder (tests use this via set_base_dir()).
"""

import os
from pathlib import Path


def set_base_dir(base_dir):
    """Point every app folder at a new root."""
    global BASE_DIR, INTAKE_DIR, ARCHIVE_DIR, SPLIT_DIR
    global MANUAL_REVIEW_DIR, LOG_DIR, DB_PATH, LOCK_PATH

    BASE_DIR = Path(base_dir)
    INTAKE_DIR = BASE_DIR / "intake"
    ARCHIVE_DIR = BASE_DIR / "original-batches"
    SPLIT_DIR = BASE_DIR / "working" / "split_notices"
    MANUAL_REVIEW_DIR = BASE_DIR / "manual-review"
    LOG_DIR = BASE_DIR / "logs"
    DB_PATH = BASE_DIR / "processing_history.db"
    LOCK_PATH = BASE_DIR / "intake_watcher.lock"


set_base_dir(os.environ.get("TTI_BASE_DIR", r"C:\TTI-Notice-Dev"))
