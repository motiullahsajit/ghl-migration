# Migration Runbook

## Setup

1. Python 3.10+
2. Copy `.env.example` → `.env` and set `GHL_API_KEY`, `GHL_LOCATION_ID`
3. GHL: enable **Allow Duplicate Contact**
4. Token scopes: contacts, custom fields, tags, invoices, payments

```powershell
cd "C:\Local Disk D\GHL"
pip install -r requirements.txt
```

Optional OCR (Script 4): install [Tesseract](https://github.com/tesseract-ocr/tesseract) and set `TESSERACT_CMD` in `.env`.

## Run order

Use your own Excel path, run ID, and attachment folder.

### 1. Load Excel into registry (no GHL writes)

```powershell
python scripts/01_init_registry.py --excel production.xlsx --run-id 2026-05-23
python scripts/01_init_registry.py --run-id 2026-05-23 --status
```

### 2. Migrate data

```powershell
python scripts/02_migrate_data.py --run-id 2026-05-23 --setup --audit
python scripts/02_migrate_data.py --run-id 2026-05-23
```

- `--dry-run` — preview only
- `--retry-failed` — resume failed rows

### 3. Upload attachments (optional)

Point at **one root folder**. The script scans all subfolders for PDFs, images, and `.csv` files.

**Required**

- One or more `.csv` mapping files (anywhere under the root)
- PDF/image files (anywhere under the root — flat or nested)

**Not required**

- Separate `pdfs/` and `images/` folders (optional for your own organization)
- A fixed folder structure

**CSV columns:** `File Name` (or `Filename`) plus one of: `Zoho Contact ID`, `Customer ID`, `Customer Name`, `GHL Contact ID`.

**Example A — flat (simplest):**

```
attachments_import/
  mapping.csv
  2621465000000317037_contract.pdf
  photo_001.png
```

**Example B — with subfolders (also fine):**

```
attachments_import/
  students_batch1.csv
  pdfs/
    2621465000000317037_contract.pdf
  images/
    photo_001.png
```

```powershell
python scripts/03_attach_documents.py --run-id 2026-05-23 --attachments-dir "D:\attachments_import"
```

Add `--move-after-upload` to remove files from the source folder after upload (default: copy to `attachments/uploaded/` only).

### 4. OCR unmatched files (optional)

```powershell
python scripts/04_ocr_rename.py --run-id 2026-05-23
python scripts/03_attach_documents.py --run-id 2026-05-23 --attachments-dir "D:\attachments_import"
```

### 5. Monitor (optional)

```powershell
python scripts/05_dashboard_server.py --run-id 2026-05-23
```

Open `http://127.0.0.1:8765?run_id=2026-05-23`

## Output

- `data/migration.db` — progress registry
- `reports/summary_{run_id}.json`
- `reports/exceptions_{run_id}.csv`
