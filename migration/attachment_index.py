"""Load attachment folder: multiple CSV maps + discover PDF/image files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

from migration.excel_loader import resolve_contact_ghl_id
from migration.utils import str_val

ATTACH_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff"}

# Column aliases (lowercase) -> logical field
COLUMN_ALIASES: dict[str, set[str]] = {
    "file_name": {
        "file",
        "filename",
        "file name",
        "file_name",
        "document",
        "document name",
        "attachment",
        "path",
        "file path",
    },
    "zoho_contact_id": {
        "zoho contact id",
        "contact id",
        "contact_id",
        "zoho_contact_id",
        "contactid",
    },
    "zoho_customer_id": {
        "zoho customer id",
        "customer id",
        "customer_id",
        "zoho_customer_id",
    },
    "customer_name": {
        "customer name",
        "customer_name",
        "name",
        "display name",
        "display_name",
        "contact name",
    },
    "ghl_contact_id": {
        "ghl contact id",
        "ghl_contact_id",
        "ghl id",
        "contact ghl id",
    },
}


def _normalize_col(col: str) -> str | None:
    c = re.sub(r"\s+", " ", str(col).strip().lower())
    for field, aliases in COLUMN_ALIASES.items():
        if c in aliases:
            return field
    return None


def _row_to_fields(row: pd.Series) -> dict[str, str]:
    out: dict[str, str] = {}
    for col in row.index:
        field = _normalize_col(str(col))
        if not field:
            continue
        val = str_val(row[col])
        if val:
            out[field] = val
    return out


def load_csv_mappings(
    attachments_dir: Path,
    registry: Any,
    run_id: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """
    Load all .csv files under attachments_dir (recursive).
    Returns (lookup, warnings) where lookup keys are lowercase file basenames
    and optional relative paths.
    """
    attachments_dir = Path(attachments_dir).resolve()
    lookup: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    csv_files = sorted(attachments_dir.rglob("*.csv"))
    print(f"CSV index files: {len(csv_files)}")

    for csv_path in csv_files:
        try:
            df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        except Exception as exc:
            warnings.append(f"Could not read {csv_path.name}: {exc}")
            continue
        if df.empty:
            continue
        for _, row in df.iterrows():
            fields = _row_to_fields(row)
            fname = fields.get("file_name")
            if not fname:
                continue
            # Allow path in CSV — use basename for key, keep full for resolve
            file_ref = fname.replace("\\", "/")
            base = Path(file_ref).name
            ghl_id = fields.get("ghl_contact_id")
            if not ghl_id:
                ghl_id = resolve_contact_ghl_id(
                    registry,
                    run_id,
                    zoho_contact_id=fields.get("zoho_contact_id"),
                    zoho_customer_id=fields.get("zoho_customer_id"),
                    customer_name=fields.get("customer_name"),
                )
            if not ghl_id:
                warnings.append(f"No GHL contact for CSV row file={base} in {csv_path.name}")
                continue
            entry = {
                "ghl_contact_id": ghl_id,
                "method": f"csv:{csv_path.name}",
                "confidence": 1.0,
                "source_csv": str(csv_path),
                "customer_name": fields.get("customer_name"),
            }
            lookup[base.lower()] = entry
            lookup[file_ref.lower()] = entry
            # Relative path from attachments root
            candidate = attachments_dir / file_ref
            if candidate.exists():
                try:
                    rel = candidate.resolve().relative_to(attachments_dir).as_posix().lower()
                    lookup[rel] = entry
                except ValueError:
                    pass
    return lookup, warnings


def discover_attachment_files(attachments_dir: Path) -> list[Path]:
    """All PDF/images under folder (recursive); skip .csv files."""
    root = Path(attachments_dir).resolve()
    files: list[Path] = []
    for fp in root.rglob("*"):
        if not fp.is_file():
            continue
        if fp.suffix.lower() == ".csv":
            continue
        if fp.suffix.lower() in ATTACH_EXT:
            files.append(fp)
    return sorted(files, key=lambda p: str(p).lower())


def match_file_with_csv_index(
    path: Path,
    attachments_dir: Path,
    csv_lookup: dict[str, dict[str, Any]],
    registry_index: dict[str, Any],
    *,
    min_confidence: float = 0.85,
) -> tuple[str | None, str, float]:
    """Match priority: CSV index -> filename/id -> name tokens."""
    path = path.resolve()
    root = Path(attachments_dir).resolve()
    keys = [path.name.lower()]
    try:
        keys.append(path.relative_to(root).as_posix().lower())
    except ValueError:
        pass

    for key in keys:
        hit = csv_lookup.get(key)
        if hit and hit.get("ghl_contact_id"):
            return hit["ghl_contact_id"], hit.get("method", "csv"), float(hit.get("confidence", 1.0))

    from migration.matchers import match_file_to_contact

    return match_file_to_contact(path, registry_index, min_confidence=min_confidence)
