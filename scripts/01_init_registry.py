#!/usr/bin/env python3
"""Initialize migration registry and ingest Excel (no GHL writes)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from migration.excel_loader import ingest_excel_to_registry
from migration.registry import MigrationRegistry


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Init migration registry from Zoho Excel")
    parser.add_argument("--excel", default=str(ROOT / "sample.xlsx"))
    parser.add_argument("--run-id", default=date.today().isoformat())
    parser.add_argument("--db", default=str(ROOT / "data" / "migration.db"))
    parser.add_argument("--status", action="store_true", help="Show counts only")
    args = parser.parse_args()

    db = Path(args.db)
    registry = MigrationRegistry(db)
    run_id = args.run_id

    if args.status:
        summary = registry.export_summary(run_id)
        print(json.dumps(summary, indent=2))
        return 0

    excel = Path(args.excel)
    if not excel.is_file():
        print(f"Excel not found: {excel}", file=sys.stderr)
        return 1

    registry.ensure_run(run_id, str(excel))
    counts = ingest_excel_to_registry(registry, run_id, excel)
    print(f"Run ID: {run_id}")
    print(f"Database: {db}")
    print(f"Excel: {excel}")
    print("Ingested:")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print("\nNext: python scripts/02_migrate_data.py --run-id", run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
