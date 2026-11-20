r"""
Check that every current client's folder can be found.

Prints a summary (counts only). With --missing, also prints every client
whose folder was not found (or has several possible folders), with the
folder name the app expected. With an output path, also writes a CSV
listing each client's database name next to the folder the app would
use, so a person can spot-check the matches in Excel.

    python scripts/check_client_folders.py
    python scripts/check_client_folders.py --missing
    python scripts/check_client_folders.py "%USERPROFILE%\Desktop\client_folders.csv"

The --missing list and the CSV contain client names (no SSNs/EINs).
Save the CSV outside the project folder and delete it when done.
"""

import argparse
import collections
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import client_db  # noqa: E402
from client_matcher import NEW_CLIENT_FOLDERS, FolderFinder, expected_folder_name, folders_root  # noqa: E402


def classify(client, folder, warnings):
    if folder:
        if any(name in folder for name in NEW_CLIENT_FOLDERS):
            return "new-client intake folder"
        if Path(folder).name == expected_folder_name(client):
            return "exact pattern"
        return "found by search"
    if warnings and "several" in warnings[0]:
        return "several possible"
    return "not found"


def print_missing(rows):
    """Clients with no folder, grouped by result, sorted by name."""
    for result in ("not found", "several possible"):
        group = sorted((r for r in rows if r[1] == result), key=lambda r: r[0].client_name.upper())
        print(f"\n{result.upper()} ({len(group)})")
        for client, _result, _folder, note in group:
            spouse = f"  (spouse: {client.spouse_name})" if client.spouse_name else ""
            goes_by = f"  (goes by: {client.nickname})" if client.nickname else ""
            print(f"  [{client.client_id}] {client.client_name}{spouse}{goes_by}")
            print(f"      expected folder: {expected_folder_name(client)}")
            if note:
                print(f"      {note}")


def main():
    parser = argparse.ArgumentParser(description="Check that every current client's folder can be found.")
    parser.add_argument("output", nargs="?", type=Path, help="also write a CSV here (outside the project)")
    parser.add_argument("--missing", action="store_true",
                        help="list the clients whose folder was not found or is ambiguous")
    args = parser.parse_args()
    output = args.output

    root = folders_root()
    if not root:
        sys.exit("ERROR: CLIENT_FOLDERS_ROOT is not set in .env")

    try:
        clients = client_db.load_clients()
    except (client_db.ClientDbNotConfigured, client_db.ClientDbUnavailable) as error:
        sys.exit(f"ERROR: {error}")

    finder = FolderFinder(root)
    rows = []
    for client in clients:
        folder, warnings = finder(client)
        rows.append((client, classify(client, folder, warnings), folder, "; ".join(warnings)))

    counts = collections.Counter(result for _client, result, _folder, _note in rows)
    print(f"{len(clients)} current clients")
    for result in ("exact pattern", "found by search", "new-client intake folder", "several possible", "not found"):
        print(f"  {result:26} {counts.get(result, 0)}")

    if args.missing:
        print_missing(rows)

    if output:
        # spot-check order: the ones most worth a look first
        order = {"several possible": 0, "found by search": 1, "not found": 2}
        rows.sort(key=lambda r: (order.get(r[1], 3), r[0].client_name.upper()))

        with output.open("w", newline="", encoding="utf-8-sig") as file:  # BOM: opens cleanly in Excel
            writer = csv.writer(file)
            writer.writerow(["GeneralID", "Client (database)", "Spouse (database)", "Goes by",
                             "Result", "Folder the app would use", "Note"])
            for client, result, folder, note in rows:
                writer.writerow([client.client_id, client.client_name, client.spouse_name or "",
                                 client.nickname or "", result, folder or "", note])
        print(f"\nWrote {output} - it contains client names; delete it when done.")


if __name__ == "__main__":
    main()
