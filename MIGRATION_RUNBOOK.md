# Migration Runbook

Zoho Books Excel → GoHighLevel migration. Run scripts in order using the **same `--run-id`** for one batch.

## One-time setup

1. Install **Python 3.10+**
2. Clone/download this repo
3. Copy `.env.example` → `.env` and fill in:

```env
GHL_API_KEY=your-token
GHL_LOCATION_ID=your-location-id
ATTACHMENTS_DIR=D:\attachments_import
GHL_EXCEL_PATH=production.xlsx
RUN_ID=2026-05-23
```

4. In GHL: enable **Allow Duplicate Contact**
5. API token scopes: contacts, custom fields, tags, invoices, payments

```powershell
cd "C:\path\to\GHL"
pip install -r requirements.txt
```

Optional OCR (Script 4): install [Tesseract](https://github.com/tesseract-ocr/tesseract) and set `TESSERACT_CMD` in `.env`.

## Prepare data (before running)

**Excel** — place your Zoho export on disk (not in git). Example: `production.xlsx`

**Attachments** — one root folder with PDFs/images and CSV mapping file(s). Flat or nested subfolders both work.

```
D:\attachments_import\
  mapping.csv
  contract_001.pdf
  photo_001.png
```

CSV columns: `File Name` (or `Filename`) plus one of `Zoho Contact ID`, `Customer ID`, `Customer Name`, `GHL Contact ID`.

Do **not** commit `.env`, Excel, or attachment files to git.

## Script running instructions

Open PowerShell in the repo folder. Use the **same `--run-id`** for every script in one migration batch.

If `GHL_EXCEL_PATH`, `RUN_ID`, and `ATTACHMENTS_DIR` are set in `.env`, you can omit those flags on the command line.

| Script | Purpose | Writes to GHL? |
|--------|---------|----------------|
| `01_init_registry.py` | Load Excel into local registry | No |
| `02_migrate_data.py` | Contacts, invoices, payments, refunds, credits, logs | Yes |
| `03_attach_documents.py` | Upload PDFs/images to Contact Documents | Yes |
| `04_ocr_rename.py` | OCR rename for unmatched files | No |
| `05_dashboard_server.py` | Local progress dashboard | No |

---

### Script 1 — `01_init_registry.py`

Load Excel into `data/migration.db`. No API calls to GHL.

```powershell
python scripts/01_init_registry.py --excel production.xlsx --run-id 2026-05-23
```

Check counts:

```powershell
python scripts/01_init_registry.py --run-id 2026-05-23 --status
```

| Flag | Required | Description |
|------|----------|-------------|
| `--excel` | Yes | Path to Zoho Excel export |
| `--run-id` | Yes | Batch ID (e.g. `2026-05-23`) |
| `--status` | No | Show registry counts only (no ingest) |

---

### Script 2 — `02_migrate_data.py`

Migrate structured data. Resumable — re-run skips rows already marked success.

**First run — setup + audit:**

```powershell
python scripts/02_migrate_data.py --run-id 2026-05-23 --setup --audit
```

**Preview (no GHL writes):**

```powershell
python scripts/02_migrate_data.py --run-id 2026-05-23 --dry-run
```

**Live migration:**

```powershell
python scripts/02_migrate_data.py --run-id 2026-05-23
```

**Retry failed rows only:**

```powershell
python scripts/02_migrate_data.py --run-id 2026-05-23 --retry-failed
```

**Run one section only:**

```powershell
python scripts/02_migrate_data.py --run-id 2026-05-23 --only contacts
python scripts/02_migrate_data.py --run-id 2026-05-23 --only invoices
python scripts/02_migrate_data.py --run-id 2026-05-23 --only payments
```

| Flag | Description |
|------|-------------|
| `--setup` | Create missing GHL custom fields and tags |
| `--audit` | Check API permissions before migrating |
| `--dry-run` | Preview without writing to GHL |
| `--retry-failed` | Re-process rows that failed previously |
| `--only` | Run one step: `contacts`, `invoices`, `payments`, `refunds`, `credits`, `logs` |
| `--excel` | Excel path (defaults to `GHL_EXCEL_PATH` in `.env`) |

Order when using `--only`: contacts → invoices → payments → refunds/credits/logs.

---

### Script 3 — `03_attach_documents.py`

Upload attachments to GHL Contact Documents. **Run after Script 2** so contacts exist in the registry.

```powershell
python scripts/03_attach_documents.py --run-id 2026-05-23
```

Or with explicit folder:

```powershell
python scripts/03_attach_documents.py --run-id 2026-05-23 --attachments-dir "D:\attachments_import"
```

**Preview matches (no upload):**

```powershell
python scripts/03_attach_documents.py --run-id 2026-05-23 --dry-run
```

**Test upload API access:**

```powershell
python scripts/03_attach_documents.py --audit-upload-api
```

| Flag | Description |
|------|-------------|
| `--run-id` | Required — same batch ID as Script 1 and 2 |
| `--attachments-dir` | Root folder with PDFs/images + CSV files (defaults to `ATTACHMENTS_DIR` in `.env`) |
| `--dry-run` | Match files only, do not upload |
| `--move-after-upload` | Move source files after upload (default: copy to `attachments/uploaded/`) |
| `--audit-upload-api` | Check document upload API (no `--run-id` needed) |

---

### Script 4 — `04_ocr_rename.py` (optional)

OCR unmatched files from `attachments/unmatched/`, rename, and requeue for Script 3. Requires Tesseract.

```powershell
python scripts/04_ocr_rename.py --run-id 2026-05-23
python scripts/03_attach_documents.py --run-id 2026-05-23
```

| Flag | Description |
|------|-------------|
| `--run-id` | Required |
| `--use-claude` | Use Claude OCR if `ANTHROPIC_API_KEY` is set |

Review `reports/ocr_manual_review_{run_id}.csv` for files still unmatched.

---

### Script 5 — `05_dashboard_server.py` (optional)

Local web dashboard. Run in a **second terminal** while Scripts 2 or 3 are running.

```powershell
python scripts/05_dashboard_server.py --run-id 2026-05-23
```

Open `http://127.0.0.1:8765?run_id=2026-05-23`

| Flag | Description |
|------|-------------|
| `--run-id` | Batch to display |
| `--port` | Port (default `8765`, or `DASHBOARD_PORT` in `.env`) |
| `--no-open-browser` | Do not auto-open browser |

---

## Full run (copy-paste)

Replace `C:\path\to\GHL`, `production.xlsx`, and `2026-05-23` with your values.

```powershell
cd "C:\path\to\GHL"

# 1. Registry
python scripts/01_init_registry.py --excel production.xlsx --run-id 2026-05-23
python scripts/01_init_registry.py --run-id 2026-05-23 --status

# 2. Migrate
python scripts/02_migrate_data.py --run-id 2026-05-23 --setup --audit
python scripts/02_migrate_data.py --run-id 2026-05-23 --dry-run
python scripts/02_migrate_data.py --run-id 2026-05-23

# 3. Attachments
python scripts/03_attach_documents.py --run-id 2026-05-23

# 4. OCR (optional)
python scripts/04_ocr_rename.py --run-id 2026-05-23
python scripts/03_attach_documents.py --run-id 2026-05-23
```

## Output and verification

| File | Purpose |
|------|---------|
| `data/migration.db` | Progress registry — do not delete mid-run |
| `reports/summary_{run_id}.json` | Overall counts |
| `reports/exceptions_{run_id}.csv` | Failed rows |

Spot-check in GHL: contacts, invoices (Issued/Paid), Documents tab on a few records.
