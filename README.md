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

## Extraction
After OCR, `src/extractor.py` reads each notice's taxpayer name(s),
SSN/EIN (last 4 digits only), notice/letter number, tax year/period,
notice date, and agency into `manifest.json` (`"extracted"`).

A notice is `identified` when the client (name or ID), notice number,
and tax year/period are all found. Otherwise it is `incomplete`,
flagged `needs_review`, and the reasons are listed for the exceptions
report. Extraction reads the saved OCR, so re-running it after a rule
change costs no Azure pages:
`python src/extractor.py <split batch folder>`

## Client matching
After extraction, `src/client_matcher.py` looks up each notice's client
in the Access database (read-only) and works out the client's folder.

- **Clients:** `LeadsTable` rows with a `ClientDate` (current clients),
  plus their businesses from `tblBusinesses`.
- **Match:** a notice is `matched` only when a name (person or business)
  **and** the SSN/EIN last 4 agree with exactly one client. Anything else
  (`suggested`, `conflict`, `ambiguous`, `not_found`, `no_client_info`)
  goes to review with a reason.
- **Folder:** for each folder in `CLIENT_FOLDERS_ROOT` (Resolution, then
  Tax Prep Only), `<root>\<first letter of last name>\<Last, First and
  SpouseFirst>`. Folder names are typed by hand, so if that exact folder
  doesn't exist, the letter folder and then the new-client intake folders
  are searched for one naming the client or their nickname (`Last_First`
  names included). Folders named "... Copy" are skipped when another
  candidate exists. The client's middle initial and Jr./Sr./II/III/IV
  (`MiddleInitial` / `Suffix` columns, or typed into the name) pick
  between folders: John F Smith goes in "Smith, John F" over "Smith,
  John", and the other way round; a folder with a *different* initial
  or suffix is never used. No folder, or several candidates, means review.
- The result goes in `manifest.json` (`"match"`), including the client's
  `Notices` folder.

Setup (the firm has 32-bit Office, so Access is read by a small 32-bit
Python helper, `src/access_reader.py`; full SSNs/EINs never leave it):
1. Install 32-bit Python and its environment (once per machine):
   ```
   py install 3.15-32
   py -V:3.15-32 -m venv .venv32
   .venv32\Scripts\pip install pyodbc
   ```
2. Add to `.env`:
   ```
   ACCESS_DB_PATH=\\server\share\Clients.accdb
   ACCESS_DB_PASSWORD=...
   CLIENT_FOLDERS_ROOT=\\tti-srv01\TTI Clients\Resolution;\\tti-srv01\TTI Clients\Tax Prep Only
   ```
3. Check: `python scripts/check_client_folders.py` (counts only). Add a
   path to also write a CSV for spot-checking (it contains client names,
   so save it outside the project and delete it afterwards).

If a 64-bit Access driver is ever installed, the app uses it directly
and the helper isn't needed. Until the database is set up, the watcher
skips matching. If the database is unreachable, it pauses for 5 minutes
like it does for Azure.

## Running
- Watcher: `python src/intake_watcher.py`
- Split one file by hand: `python src/pdf_splitter.py path\to\batch.pdf`
- OCR one notice by hand: `python src/ocr.py path\to\notice_001.pdf`
- Extract (or re-extract) a batch: `python src/extractor.py <batch folder>`
- Match (or re-match) a batch: `python src/client_matcher.py <batch folder>`
- Show the Access schema: `python scripts/show_access_schema.py`
- Check client folders: `python scripts/check_client_folders.py [--missing] [out.csv]`
- Retry a failed batch: `python src/retry_failed.py --filename name.pdf`

Each split batch gets a `manifest.json` that lists, for every page, its
kind (content / separator / blank) and ink level, plus any notices
flagged for review.

## Tests
`python -m pytest`. Test batches (fictional data) live in
`scripts/generate_test_batch.py`. Each one declares the result it
should produce, and the tests check every batch.
