"""
Create the printable QR separator sheet.

Usage:
    python scripts/make_separator_sheet.py [output.pdf]

Print it double-sided so the code is found whichever way the sheet
faces. Light-colored paper makes the sheets easy to spot and reuse.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf  # noqa: E402

import config  # noqa: E402
from separator_sheet import add_separator_page  # noqa: E402


def main():
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        config.BASE_DIR / "separator_sheet.pdf"
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    with pymupdf.open() as doc:
        add_separator_page(doc)  # front
        add_separator_page(doc)  # back
        doc.save(output)

    print(f"Separator sheet saved: {output}")
    print("Print double-sided.")


if __name__ == "__main__":
    main()
