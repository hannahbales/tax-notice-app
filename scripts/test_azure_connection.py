"""
Check that the Azure Document Intelligence settings in .env work.

Sends ONE fictional page (uses 1 page of the monthly quota) and
prints what Azure read. Never prints the key.

Usage:
    python scripts/test_azure_connection.py
"""

import os
import sys
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf  # noqa: E402

import ocr  # noqa: E402


def sample_pdf():
    with pymupdf.open() as doc:
        page = doc.new_page()
        lines = [
            "SYNTHETIC TEST NOTICE - NOT A REAL TAXPAYER",
            "Notice CP14    Tax Year 2024",
            "Taxpayer: Test Person    SSN: XXX-XX-0000",
            "Amount due: $123.45",
        ]
        for number, line in enumerate(lines):
            page.insert_text((72, 100 + number * 24), line, fontsize=12)
        return BytesIO(doc.tobytes())


def main():
    try:
        client = ocr.get_client()
    except ocr.OcrUnavailable as error:
        sys.exit(f"FAILED: {error}")

    host = urlparse(os.environ["AZURE_DI_ENDPOINT"]).netloc
    print(f"Endpoint: {host}")
    print(f"Model:    {ocr.MODEL_ID}")
    print("Sending 1 fictional page to Azure...\n")

    try:
        raw = ocr.analyze(client, sample_pdf())
    except (ocr.OcrUnavailable, ocr.OcrRejected) as error:
        sys.exit(f"FAILED: {error}")

    summary = ocr.summarize(raw, page_count=1)

    print("Azure read:")
    print("  " + raw.get("content", "").replace("\n", "\n  "))
    print()
    print(f"API version: {raw.get('apiVersion')}")
    print(f"Words: {summary['word_count']}   Confidence: {summary['mean_confidence']}")
    print("\nSUCCESS: Azure connection works.")


if __name__ == "__main__":
    main()
