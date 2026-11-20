r"""
Print the Access database's tables, saved queries, and column names.

Shows names and types only - never any client data - so the output is
safe to share when setting up CLIENT_QUERY in src/client_db.py.

    python scripts/show_access_schema.py               (uses ACCESS_DB_PATH from .env)
    python scripts/show_access_schema.py path\to\db.accdb
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from client_db import ClientDbNotConfigured, ClientDbUnavailable, read_schema  # noqa: E402


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        schema = read_schema(path)
    except (ClientDbNotConfigured, ClientDbUnavailable) as error:
        sys.exit(f"ERROR: {error}")

    for name, columns in schema.items():
        print()
        print(f"[{name}]")
        for column, type_name in columns:
            print(f"    {column:32} {type_name}")


if __name__ == "__main__":
    main()
