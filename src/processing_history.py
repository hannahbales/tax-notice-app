
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(r"C:\TTI-Notice-Dev")
DB_PATH = BASE_DIR / "processing_history.db"


def connect():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def initialize_database():
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_files (
                file_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TEXT NOT NULL
            )
        """)


def get_status(file_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM processed_files WHERE file_id = ?",
            (file_id,)
        ).fetchone()

    return row[0] if row else None


def record_status(file_id, filename, status, attempts, error=None):
    now = datetime.now(timezone.utc).isoformat()

    with connect() as conn:
        conn.execute("""
            INSERT INTO processed_files (
                file_id, filename, status,
                attempts, last_error, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                filename = excluded.filename,
                status = excluded.status,
                attempts = excluded.attempts,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
        """, (file_id, filename, status, attempts, error, now))

def get_record(file_id):
    with connect() as conn:
        return conn.execute(
            """
            SELECT status, attempts
            FROM processed_files
            WHERE file_id = ?
            """,
            (file_id,)
        ).fetchone()

    
def reset_failed_file(file_id):
    """
    Reset a failed PDF so the watcher can retry it.

    Only failed records can be reset.
    Completed records remain protected.
    """
    now = datetime.now(timezone.utc).isoformat()

    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE processed_files
            SET status = 'pending',
                attempts = 0,
                last_error = NULL,
                updated_at = ?
            WHERE file_id = ?
              AND status = 'failed'
            """,
            (now, file_id),
        )

        return cursor.rowcount > 0
