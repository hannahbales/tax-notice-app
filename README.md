# TTI Notice Processing

Automated document scanning, client identification,
and PDF filing system.

## Technologies
- Python
- Microsoft Access (read-only)
- Azure AI Document Intelligence
- Windows Network File Shares
## Setup
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Separator sheets
Notices are split on a printed **QR separator sheet**, not a blank page
(blank pages are dropped, so duplex scanning is safe).

1. `python scripts/make_separator_sheet.py` creates `separator_sheet.pdf`.
2. Print it double-sided and reuse the sheets.
3. Put one sheet between each notice, then scan the whole stack once.

## Azure OCR
Notices are read with Azure AI Document Intelligence (`prebuilt-read`).
Put the resource's endpoint and key in `.env` (never commit it):
```
AZURE_DI_ENDPOINT=https://<name>.cognitiveservices.azure.com/
AZURE_DI_KEY=<key 1>
```
Check the connection: `python scripts/test_azure_connection.py`

Free tier (F0): 500 pages/month, 4 MB per file, first 2 pages of each
notice only (flagged in the manifest). On Standard (S0), add
`AZURE_DI_MAX_MB=500` to `.env`.

If Azure is unavailable (bad key, quota used up, outage), the watcher
pauses for 5 minutes and leaves batches in intake. No attempts are
used up, and nothing is sent to manual review.

## Running
- Watcher: `python src/intake_watcher.py`
- Split one file by hand: `python src/pdf_splitter.py path\to\batch.pdf`
- OCR one notice by hand: `python src/ocr.py path\to\notice_001.pdf`
- Retry a failed batch: `python src/retry_failed.py --filename name.pdf`

Each split batch gets a `manifest.json` that lists, for every page, its
kind (content / separator / blank) and ink level, plus any notices
flagged for review.

## Tests
`python -m pytest`. Test batches (fictional data) live in
`scripts/generate_test_batch.py`. Each one declares the result it
should produce, and the tests check every batch.
