"""
Read the Access database with pyodbc. Standard library + pyodbc only.

The firm's PCs have 32-bit Office, so only a 32-bit Access driver is
installed, and the 64-bit app can't load it. client_db.py therefore
runs this file in the 32-bit Python (.venv32) and reads its JSON
output. If a 64-bit driver is ever installed, client_db.py imports
these functions directly instead.

SSN/EIN columns are cut to their last 4 digits HERE, so full numbers
never leave this process. The database password arrives on stdin (never
on the command line, where other programs could see it) and is never
logged or echoed back.

    echo {"action": "schema", "path": "..."} | .venv32\\Scripts\\python src\\access_reader.py
"""

import json
import re
import sys
from pathlib import Path

# columns that hold SSNs / EINs: reduced to the last 4 digits
ID_COLUMNS = ("ssn", "spouse_ssn", "ein", "tin")


class ReaderError(Exception):
    """kind is "config" (fix the settings) or "unavailable" (try again later)."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def last4(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-4:] if len(digits) >= 4 else None


def find_driver():
    """An installed ODBC driver (matching this Python's bitness) that opens .accdb files."""
    import pyodbc

    for driver in pyodbc.drivers():
        if "*.accdb" in driver:
            return driver
    return None


def _odbc_value(value):
    """
    The Access driver takes PWD literally ("{pw}" would include the
    braces), so only brace-quote when a ; would otherwise end the value.
    """
    if ";" in value or value.startswith("{"):
        return "{" + value.replace("}", "}}") + "}"
    return value


def connection_string(driver, path, password=None):
    parts = [f"DRIVER={{{driver}}}", f"DBQ={path}", "ReadOnly=1"]
    if password:
        parts.append(f"PWD={_odbc_value(password)}")
    return ";".join(parts) + ";"


def _connect(path, password=None):
    import pyodbc

    driver = find_driver()
    if driver is None:
        raise ReaderError("unavailable", "no Microsoft Access ODBC driver for this Python")
    if not Path(path).is_file():
        raise ReaderError("unavailable", f"client database not found: {path}")
    try:
        return pyodbc.connect(connection_string(driver, path, password), autocommit=True)
    except pyodbc.Error as error:
        if "password" in str(error).lower():
            # a wrong password won't fix itself by waiting
            raise ReaderError(
                "config",
                "the client database password is missing or wrong "
                "(ACCESS_DB_PASSWORD in .env)",
            ) from None
        raise ReaderError("unavailable", f"cannot open client database: {error}") from error


def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)  # dates, decimals


def read_rows(path, query, columns, password=None):
    """Run query; return rows as dicts with ID columns cut to last 4."""
    import pyodbc

    connection = _connect(path, password)
    try:
        cursor = connection.cursor()
        cursor.execute(query)
        names = [column[0].lower() for column in cursor.description]

        missing = set(columns) - set(names)
        if missing:
            raise ReaderError("config", f"CLIENT_QUERY is missing columns: {', '.join(sorted(missing))}")

        rows = []
        for values in cursor.fetchall():
            row = dict(zip(names, values))
            rows.append(
                {
                    name: last4(row[name]) if name in ID_COLUMNS else _plain(row[name])
                    for name in columns
                }
            )
        return rows
    except pyodbc.Error as error:
        raise ReaderError("unavailable", f"cannot read client database: {error}") from error
    finally:
        connection.close()


def _lenient_utf16(raw):
    """
    The Access ODBC driver returns table/column Descriptions (REMARKS)
    with a wrong length, so the text ends in garbage bytes that are not
    valid UTF-16. Decode what is valid and stop at the first NUL.
    """
    if raw is None:
        return None
    return raw.decode("utf-16-le", errors="ignore").split("\x00", 1)[0]


def read_schema(path, password=None):
    """{"TABLE name" or "QUERY name": [(column, type), ...]} - names only, no data."""
    import pyodbc

    connection = _connect(path, password)
    for sql_type in (pyodbc.SQL_WCHAR, pyodbc.SQL_WVARCHAR, pyodbc.SQL_WLONGVARCHAR):
        connection.add_output_converter(sql_type, _lenient_utf16)
    try:
        objects = [
            (row.table_type, row.table_name)
            for row in connection.cursor().tables()
            if row.table_type in ("TABLE", "VIEW")
        ]
        schema = {}
        for kind, name in sorted(objects):
            label = "TABLE" if kind == "TABLE" else "QUERY"
            schema[f"{label} {name}"] = [
                (column.column_name, column.type_name)
                for column in connection.cursor().columns(table=name)
            ]
        return schema
    finally:
        connection.close()


def handle(request):
    if request["action"] == "rows":
        return {
            "rows": read_rows(
                request["path"], request["query"], request["columns"], request.get("password")
            )
        }
    if request["action"] == "schema":
        return {"schema": read_schema(request["path"], request.get("password"))}
    raise ReaderError("config", f"unknown action: {request['action']}")


def main():
    try:
        response = handle(json.loads(sys.stdin.read()))
    except ReaderError as error:
        response = {"error": str(error), "kind": error.kind}
    except ImportError:
        response = {"error": "pyodbc is not installed in this Python", "kind": "config"}
    except Exception as error:  # report, never crash silently
        response = {"error": f"{type(error).__name__}: {error}", "kind": "unavailable"}

    sys.stdout.write(json.dumps(response))


if __name__ == "__main__":
    main()
