# Migration Runbook

## Prerequisites

- Python 3.10+
- `.env` with `GHL_API_KEY`, `GHL_LOCATION_ID`
- GHL: Allow Duplicate Contact enabled
- Token scopes: contacts, custom fields, tags, invoices, payments read

## Install

```powershell
cd "C:\Local Disk D\GHL"
pip install -r requirements.txt
```

Optional OCR: install [Tesseract](https://github.com/tesseract-ocr/tesseract) and set `TESSERACT_CMD` in `.env`.

## Production workflow (~4,000 rows)

### 1. Initialize registry (no GHL writes)

```powershell
python scripts/01_init_registry.py --excel production.xlsx --run-id 2026-05-23
python scripts/01_init_registry.py --run-id 2026-05-23 --status
```

### 2. Start dashboard (optional)

```powershell
python scripts/05_dashboard_server.py --run-id 2026-05-23
```

Open `http://127.0.0.1:8765?run_id=2026-05-23`

### 3. Migrate structured data

```powershell
python scripts/02_migrate_data.py --run-id 2026-05-23 --setup --audit
python scripts/02_migrate_data.py --run-id 2026-05-23
```

Resume after crash:

```powershell
python scripts/02_migrate_data.py --run-id 2026-05-23 --retry-failed
```

Dry-run:

```powershell
python scripts/02_migrate_data.py --run-id 2026-05-23 --dry-run
```

### 4. Upload attachments

Use one folder with **PDFs/images** and **multiple CSV mapping files** (any subfolders). The script scans recursively.

Example layout:

```
attachments_import/
  students_batch1.csv
  students_batch2.csv
  pdfs/
    2621465000000317037_contract.pdf
  images/
    photo_001.png
```

**CSV columns** (flexible headers): `File Name` or `Filename`, plus one of:
`Zoho Contact ID`, `Customer ID`, `Customer Name`, or `GHL Contact ID`.

```powershell
python scripts/03_attach_documents.py --run-id 2026-05-23 --attachments-dir "D:\path\to\attachments_import"
```

Optional: `--move-after-upload` to move files out of the source folder (default copies to `attachments/uploaded/` only).

Legacy single inbox:

```powershell
python scripts/03_attach_documents.py --run-id 2026-05-23 --inbox attachments/inbox
```

```powershell
python scripts/03_attach_documents.py --audit-upload-api
```

### 5. OCR pass (unmatched only)

```powershell
python scripts/04_ocr_rename.py --run-id 2026-05-23
python scripts/03_attach_documents.py --run-id 2026-05-23
```

Review `reports/ocr_manual_review_{run_id}.csv` for remaining files.

## Reports

- `data/migration.db` — registry (gitignored)
- `reports/summary_{run_id}.json`
- `reports/exceptions_{run_id}.csv`

## Legacy script

`upload_to_ghl.py` remains for quick tests; prefer `scripts/02_migrate_data.py` for production.
