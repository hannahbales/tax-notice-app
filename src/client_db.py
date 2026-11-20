"""
Read-only access to the firm's Microsoft Access client database.

Settings (in .env):
    ACCESS_DB_PATH=\\\\server\\share\\Clients.accdb
    ACCESS_DB_PASSWORD=...      (only if the database has a password)

CLIENT_QUERY returns one row per current client (LeadsTable rows with a
ClientDate), using the column names in CLIENT_COLUMNS. BUSINESS_QUERY
returns the businesses each client owns (tblBusinesses), so a notice
addressed to a client's business still finds the client.

The client's folder is not stored in the database; client_matcher.py
works it out from CLIENT_FOLDERS_ROOT (in .env) and the client's name.

Only the last 4 digits of any SSN/EIN leave access_reader.py; full
numbers are never stored or logged.
"""

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

import access_reader
from access_reader import last4  # noqa: F401  (used by client_from_row)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CLIENT_QUERY = """
SELECT L.GeneralID AS client_id,
       L.[First Name] AS first_name,
       L.MiddleInitial AS middle_initial,
       L.[Last Name] AS last_name,
       L.Suffix AS suffix,
       L.GoesBy_Nickname AS nickname,
       L.[Spouse First Name] AS spouse_first_name,
       L.[Spouse Last Name] AS spouse_last_name,
       L.SSN AS ssn,
       L.SpouseSSN AS spouse_ssn,
       L.EIN AS ein,
       L.BusName AS bus_name
FROM LeadsTable AS L
WHERE L.ClientDate IS NOT NULL
"""

CLIENT_COLUMNS = (
    "client_id", "first_name", "middle_initial", "last_name", "suffix", "nickname", "spouse_first_name", "spouse_last_name",
    "ssn", "spouse_ssn", "ein", "bus_name",
)

# every business, active or closed: notices often concern closed businesses
BUSINESS_QUERY = """
SELECT B.GeneralID AS client_id, B.BusName AS bus_name, B.EIN AS ein
FROM tblBusinesses AS B
"""

BUSINESS_COLUMNS = ("client_id", "bus_name", "ein")

logger = logging.getLogger(__name__)


class ClientDbNotConfigured(Exception):
    """No database set up yet: matching is skipped."""


class ClientDbUnavailable(Exception):
    """The database is set up but cannot be read right now (network, driver, lock)."""


@dataclass(frozen=True)
class Client:
    client_id: str
    client_name: str  # "Last, First"
    spouse_name: str | None  # "Last, First"
    ssn_last4: frozenset
    ein_last4: frozenset  # client's own EIN and all their businesses'
    tin_last4: frozenset
    folder: str | None = None  # only when the database stores one
    first_name: str | None = None
    last_name: str | None = None
    nickname: str | None = None  # "goes by" name, often used for folder names
    spouse_first_name: str | None = None
    business_names: tuple = ()
    middle_initial: str | None = None  # "F"
    suffix: str | None = None  # generational only: JR, SR, II, III, IV


def clean_folder(value):
    """
    Normalise a folder path from the database.

    Access Hyperlink fields come back as "display#address#subaddress";
    the address part is the path.
    """
    text = str(value or "").strip().strip('"').strip()
    if text.count("#") >= 2:
        display, address = text.split("#")[:2]
        text = (address or display).strip()
    return text or None


# generational suffixes: John Smith Jr. and John Smith Sr. are different people
GENERATIONS = ("JR", "SR", "II", "III", "IV")


def _upper_words(*texts):
    """Upper-case words; apostrophes dropped so O'Brien stays one word."""
    return [
        w for text in texts
        for w in re.sub(r"[^A-Z ]", " ", re.sub(r"['’]", "", str(text or "").upper())).split()
    ]


def middle_initial(middle, first=None):
    """From the MiddleInitial column, or an initial typed after the first name ("John F.")."""
    words = _upper_words(middle) or [w for w in _upper_words(first)[1:] if len(w) == 1]
    return words[0][0] if words else None


def generation(*texts):
    """JR / SR / II / III / IV from the Suffix column or a name ("Smith Jr.")."""
    return next((w for w in _upper_words(*texts) if w in GENERATIONS), None)


def _text(value):
    text = " ".join(str(value or "").split())
    return text or None


def client_from_row(row, businesses=()):
    """
    Build a Client from one CLIENT_QUERY row (a dict), plus that
    client's BUSINESS_QUERY rows. "client_name" / "spouse_name" /
    "folder" / "tin" keys are also accepted.
    """

    def ids(*keys, rows=(row,)):
        return frozenset(filter(None, (last4(r.get(key)) for r in rows for key in keys)))

    first, last = _text(row.get("first_name")), _text(row.get("last_name"))
    client_name = _text(row.get("client_name")) or ", ".join(filter(None, (last, first)))
    if not last and "," in client_name:
        last, first = (part.strip() or None for part in client_name.split(",", 1))

    spouse_first = _text(row.get("spouse_first_name"))
    spouse_name = _text(row.get("spouse_name"))
    if not spouse_name and spouse_first:
        spouse_name = f"{_text(row.get('spouse_last_name')) or last}, {spouse_first}"

    business_names = tuple(
        dict.fromkeys(
            name for name in (_text(r.get("bus_name")) for r in (row, *businesses)) if name
        )
    )

    return Client(
        client_id=str(row.get("client_id")),
        client_name=client_name,
        spouse_name=spouse_name,
        ssn_last4=ids("ssn", "spouse_ssn"),
        ein_last4=ids("ein", rows=(row, *businesses)),
        tin_last4=ids("tin"),
        folder=clean_folder(row.get("folder")),
        first_name=first,
        last_name=last,
        nickname=_text(row.get("nickname")),
        spouse_first_name=spouse_first,
        business_names=business_names,
        middle_initial=middle_initial(row.get("middle_initial"), first),
        suffix=generation(row.get("suffix"), last, first),
    )


# --- database -------------------------------------------------------------

# 32-bit Python that runs access_reader.py (the firm has 32-bit Office,
# so only a 32-bit Access driver exists). Override with ACCESS_HELPER_PYTHON.
DEFAULT_HELPER_PYTHON = PROJECT_ROOT / ".venv32" / "Scripts" / "python.exe"
READER_SCRIPT = Path(__file__).with_name("access_reader.py")
HELPER_TIMEOUT_SECONDS = 120


def db_path():
    load_dotenv(PROJECT_ROOT / ".env")
    return os.environ.get("ACCESS_DB_PATH") or None


def db_password():
    """ACCESS_DB_PASSWORD from .env, or None for a database without one."""
    load_dotenv(PROJECT_ROOT / ".env")
    return os.environ.get("ACCESS_DB_PASSWORD") or None


def helper_python():
    load_dotenv(PROJECT_ROOT / ".env")
    return Path(os.environ.get("ACCESS_HELPER_PYTHON") or DEFAULT_HELPER_PYTHON)


def _raise(kind, message):
    if kind == "config":
        raise ClientDbNotConfigured(message)
    raise ClientDbUnavailable(message)


def _direct_driver_available():
    try:
        return access_reader.find_driver() is not None
    except ImportError:  # pyodbc not installed
        return False


def _request(payload):
    """
    Run an access_reader request: in this process if a matching Access
    driver is installed, otherwise in the 32-bit helper Python.
    """
    if _direct_driver_available():
        try:
            return access_reader.handle(payload)
        except access_reader.ReaderError as error:
            _raise(error.kind, str(error))

    python = helper_python()
    if not python.is_file():
        raise ClientDbNotConfigured(
            f"32-bit helper Python not found at {python} "
            "(see README: Client matching setup)"
        )

    try:
        completed = subprocess.run(
            [str(python), "-I", str(READER_SCRIPT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=HELPER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ClientDbUnavailable(
            f"client database did not respond within {HELPER_TIMEOUT_SECONDS}s"
        ) from error

    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError:
        detail = (completed.stderr or completed.stdout).strip()[-500:]
        raise ClientDbUnavailable(f"32-bit Access helper failed: {detail}") from None

    if "error" in response:
        _raise(response["kind"], response["error"])
    return response


def _rows(path, query, columns):
    return _request(
        {
            "action": "rows",
            "path": path,
            "password": db_password(),
            "query": query,
            "columns": list(columns),
        }
    )["rows"]


def load_clients():
    """Every current client in the database, as Client objects."""
    path = db_path()
    if not path:
        raise ClientDbNotConfigured("ACCESS_DB_PATH is not set in .env")

    businesses = {}
    for row in _rows(path, BUSINESS_QUERY, BUSINESS_COLUMNS):
        businesses.setdefault(str(row["client_id"]), []).append(row)

    clients = [
        client_from_row(row, businesses.get(str(row["client_id"]), ()))
        for row in _rows(path, CLIENT_QUERY, CLIENT_COLUMNS)
    ]

    logger.info("Loaded %s clients from the client database", len(clients))
    return clients


def read_schema(path=None):
    """Table / query / column names (no data)."""
    path = path or db_path()
    if not path:
        raise ClientDbNotConfigured("ACCESS_DB_PATH is not set in .env")
    return _request({"action": "schema", "path": path, "password": db_password()})["schema"]
