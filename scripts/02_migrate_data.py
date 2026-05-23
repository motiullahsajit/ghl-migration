#!/usr/bin/env python3
"""Migrate structured data to GHL (registry-aware, resumable)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from migration.ghl_client import GHLClient
from migration.migrate_engine import run_data_migration, write_reports
from migration.registry import MigrationRegistry


def print_audit(client: GHLClient) -> None:
    report = client.audit()
    print(f"  Custom fields: {report['custom_field_count']}")
    print(f"  Tags: {report['tag_count']}")
    print(f"  Allow duplicate contacts: {report['duplicates_allowed']}")
    perms = report.get("invoice_permissions") or {}
    for key, label in [
        ("list", "List invoices"),
        ("create", "Create invoice"),
        ("get_by_id", "Get invoice by ID"),
        ("send", "Issue invoice"),
        ("record_payment", "Record payment"),
    ]:
        print(f"    [{'OK' if perms.get(key) else 'DENIED'}] {label}")


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Migrate Zoho data to GHL")
    parser.add_argument("--excel", default=os.getenv("GHL_EXCEL_PATH", str(ROOT / "sample.xlsx")))
    parser.add_argument("--run-id", default=os.getenv("RUN_ID", ""))
    parser.add_argument("--db", default=os.getenv("MIGRATION_DB_PATH", str(ROOT / "data" / "migration.db")))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--setup", action="store_true", help="Create missing GHL fields/tags first")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--only", choices=["contacts", "invoices", "payments", "refunds", "credits", "logs"])
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--rate-limit-ms", type=int, default=150)
    args = parser.parse_args()

    api_key = os.getenv("GHL_API_KEY", "").strip()
    location_id = os.getenv("GHL_LOCATION_ID", "").strip()
    if not api_key or not location_id:
        print("Set GHL_API_KEY and GHL_LOCATION_ID in .env", file=sys.stderr)
        return 1

    run_id = args.run_id or __import__("datetime").date.today().isoformat()
    excel = Path(args.excel)
    registry = MigrationRegistry(Path(args.db))
    registry.ensure_run(run_id, str(excel))

    client = GHLClient(api_key, location_id)
    if args.audit:
        print("=== GHL Audit ===")
        print_audit(client)
        if not args.dry_run and not args.setup:
            return 0

    if args.setup:
        print("=== GHL Setup ===")
        r = client.setup(dry_run=args.dry_run)
        print(f"  fields: {r['created_fields']}")
        print(f"  tags: {r['created_tags']}")

    if not excel.is_file():
        print(f"Excel not found: {excel}", file=sys.stderr)
        return 1

    print(f"=== Migration run_id={run_id} ===")
    errors = run_data_migration(
        client,
        registry,
        run_id,
        excel,
        dry_run=args.dry_run,
        only=args.only,
        retry_failed=args.retry_failed,
        rate_limit_ms=args.rate_limit_ms,
        setup=False,
    )
    if not args.dry_run:
        write_reports(registry, run_id, ROOT / "reports")
    print(f"\nDone. Errors: {errors}")
    print(f"Dashboard: python scripts/05_dashboard_server.py --run-id {run_id}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
