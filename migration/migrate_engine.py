"""Registry-aware migration engine (Script 2 core)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from migration.excel_loader import (
    canonical_contacts,
    load_all,
    resolve_contact_ghl_id,
)
from migration.ghl_client import GHLClient
from migration.models import ContactRecord, InvoiceRecord, PaymentRecord
from migration.registry import MigrationRegistry
from migration.utils import money


def _payload_contact(c: ContactRecord) -> dict[str, Any]:
    return {
        "zoho_contact_id": c.zoho_contact_id,
        "zoho_customer_id": c.zoho_customer_id,
        "email": c.email,
        "display_name": c.display_name,
        "tags": c.tags,
    }


def run_data_migration(
    client: GHLClient,
    registry: MigrationRegistry,
    run_id: str,
    excel_path: Path,
    *,
    dry_run: bool = False,
    only: str | None = None,
    retry_failed: bool = False,
    rate_limit_ms: int = 150,
    setup: bool = False,
) -> int:
    errors = 0
    if setup and not dry_run:
        client.setup(dry_run=False)

    data = load_all(excel_path)
    contacts = canonical_contacts(data["contacts"])

    def should_run(phase: str) -> bool:
        return only is None or only == phase

    def sleep() -> None:
        if rate_limit_ms > 0:
            time.sleep(rate_limit_ms / 1000.0)

    # --- Contacts ---
    if should_run("contacts"):
        print(f"\n--- Contacts ({len(contacts)} canonical) ---")
        for i, contact in enumerate(contacts, 1):
            zkey = contact.registry_key or ""
            label = contact.display_name or f"{contact.first_name} {contact.last_name}".strip()
            existing = registry.get_entity(run_id, zkey)
            if existing and existing["status"] == "success" and existing.get("ghl_id"):
                print(f"  [{i}] SKIP (ok) {label} -> {existing['ghl_id']}")
                continue
            if existing and existing["status"] == "failed" and not retry_failed:
                print(f"  [{i}] SKIP (failed) {label}")
                errors += 1
                continue
            if dry_run:
                print(f"  [{i}] would create {label}")
                continue
            registry.mark_in_progress(run_id, zkey)
            try:
                cid = client.create_contact(contact)
                registry.mark_success(run_id, zkey, cid)
                registry.log_event(run_id, "contact_created", label, zoho_key=zkey, detail={"ghl_id": cid})
                print(f"  [{i}] OK {label} -> {cid}")
            except Exception as exc:
                registry.mark_failed(run_id, zkey, str(exc))
                print(f"  [{i}] FAIL {label}: {exc}", file=__import__("sys").stderr)
                errors += 1
            sleep()

    invoice_number_to_ghl: dict[str, str] = {}
    invoice_totals: dict[str, float] = {inv.invoice_number: money(inv.total) for inv in data["invoices"]}
    invoice_paid: dict[str, float] = {}
    payments_by_inv: dict[str, list[PaymentRecord]] = {}
    for p in data["payments"]:
        payments_by_inv.setdefault(p.invoice_number, []).append(p)

    # --- Invoices ---
    if should_run("invoices"):
        print(f"\n--- Invoices ({len(data['invoices'])}) ---")
        for inv in data["invoices"]:
            zkey = inv.registry_key or ""
            existing = registry.get_entity(run_id, zkey)
            if existing and existing["status"] == "success" and existing.get("ghl_id"):
                invoice_number_to_ghl[inv.invoice_number] = existing["ghl_id"]
                print(f"  SKIP (ok) {inv.invoice_number} -> {existing['ghl_id']}")
                continue
            cid = resolve_contact_ghl_id(
                registry,
                run_id,
                zoho_customer_id=inv.customer_id,
                customer_name=inv.customer_name,
            )
            if dry_run:
                print(f"  would create {inv.invoice_number} for {inv.customer_name}")
                invoice_number_to_ghl[inv.invoice_number] = f"dry-{inv.invoice_number}"
                continue
            if not cid:
                print(f"  SKIP {inv.invoice_number}: no contact")
                registry.mark_failed(run_id, zkey, "no contact")
                errors += 1
                continue
            registry.mark_in_progress(run_id, zkey)
            try:
                contact_stub = ContactRecord(
                    email=inv.email,
                    first_name="",
                    last_name="",
                    phone=inv.phone,
                    zoho_customer_id=inv.customer_id,
                    display_name=inv.customer_name,
                    registry_key=f"customer:{inv.customer_id}" if inv.customer_id else None,
                )
                iid = client.create_invoice(inv, cid, contact_stub)
                warns = client.issue_invoice(iid)
                registry.mark_success(run_id, zkey, iid)
                invoice_number_to_ghl[inv.invoice_number] = iid
                st = client.get_invoice_status(iid) or "?"
                print(f"  OK {inv.invoice_number} -> {iid} [{st}]")
                for w in warns:
                    print(f"      warn: {w}", file=__import__("sys").stderr)
            except Exception as exc:
                registry.mark_failed(run_id, zkey, str(exc))
                print(f"  FAIL {inv.invoice_number}: {exc}", file=__import__("sys").stderr)
                errors += 1
            sleep()

    # refresh invoice map from registry
    for inv in data["invoices"]:
        if inv.invoice_number not in invoice_number_to_ghl:
            gid = registry.get_ghl_id(run_id, inv.registry_key or "")
            if gid:
                invoice_number_to_ghl[inv.invoice_number] = gid

    # --- Payments ---
    if should_run("payments"):
        n = sum(len(v) for v in payments_by_inv.values())
        print(f"\n--- Payments ({n}) ---")
        for inv_num, pays in sorted(payments_by_inv.items()):
            paid = invoice_paid.get(inv_num, 0.0)
            total = invoice_totals.get(inv_num)
            for pay in pays:
                zkey = pay.registry_key or ""
                existing = registry.get_entity(run_id, zkey)
                if existing and existing["status"] == "success":
                    paid = money(paid + pay.amount)
                    print(f"  SKIP (ok) {inv_num} ${pay.amount}")
                    continue
                if dry_run:
                    print(f"  would pay {inv_num} ${pay.amount}")
                    continue
                iid = invoice_number_to_ghl.get(inv_num)
                if not iid:
                    registry.mark_failed(run_id, zkey, "invoice not found")
                    errors += 1
                    continue
                registry.mark_in_progress(run_id, zkey)
                try:
                    warns, txn = client.record_invoice_payment(
                        iid, pay, invoice_total=total, paid_so_far=paid
                    )
                    if any("exceeds" in w for w in warns):
                        registry.mark_failed(run_id, zkey, ";".join(warns))
                        errors += 1
                    else:
                        registry.mark_success(run_id, zkey, txn or iid)
                        paid = money(paid + pay.amount)
                        cid = resolve_contact_ghl_id(
                            registry, run_id, customer_name=pay.customer_name
                        )
                        if cid and txn:
                            client.add_contact_tags(cid, ["Paid", "zoho-payment"])
                        print(f"  OK {inv_num} ${pay.amount} txn={txn}")
                except Exception as exc:
                    registry.mark_failed(run_id, zkey, str(exc))
                    print(f"  FAIL {inv_num}: {exc}", file=__import__("sys").stderr)
                    errors += 1
                sleep()
            invoice_paid[inv_num] = paid

    # --- Refunds ---
    if should_run("refunds"):
        from migration.models import RefundRecord

        print(f"\n--- Refunds ({len(data['refunds'])}) ---")
        for ref in data["refunds"]:
            zkey = ref.registry_key or ""
            existing = registry.get_entity(run_id, zkey)
            if existing and existing["status"] == "success":
                print(f"  SKIP (ok) {ref.credit_note_number}")
                continue
            if dry_run:
                print(f"  would refund {ref.credit_note_number}")
                continue
            cid = resolve_contact_ghl_id(registry, run_id, customer_name=ref.customer_name)
            if not cid:
                registry.mark_failed(run_id, zkey, "no contact")
                errors += 1
                continue
            inv_id = None
            if ref.applied_invoice:
                inv_id = invoice_number_to_ghl.get(ref.applied_invoice)
            registry.mark_in_progress(run_id, zkey)
            try:
                warns, txn, method = client.record_refund(cid, ref, inv_id)
                registry.mark_success(run_id, zkey, txn or f"activity:{method}")
                print(f"  OK {ref.credit_note_number} [{method}]")
            except Exception as exc:
                registry.mark_failed(run_id, zkey, str(exc))
                errors += 1
            sleep()

    # --- Credits ---
    if should_run("credits"):
        print(f"\n--- Credits ({len(data['credits'])}) ---")
        for cred in data["credits"]:
            zkey = cred.registry_key or ""
            existing = registry.get_entity(run_id, zkey)
            if existing and existing["status"] == "success":
                continue
            body = (
                f"Zoho Credit Note (imported)\nNumber: {cred.credit_note_number}\n"
                f"Total: {cred.total}\nApplied: {cred.applied_invoice}\n{cred.description}"
            )
            if dry_run:
                print(f"  would credit {cred.credit_note_number}")
                continue
            cid = resolve_contact_ghl_id(registry, run_id, customer_name=cred.customer_name)
            if not cid:
                registry.mark_failed(run_id, zkey, "no contact")
                errors += 1
                continue
            try:
                client.add_contact_tags(cid, ["zoho-credit"])
                client.add_contact_activity(cid, f"Credit Note — {cred.credit_note_number}", body)
                registry.mark_success(run_id, zkey, cid)
                print(f"  OK credit {cred.credit_note_number}")
            except Exception as exc:
                registry.mark_failed(run_id, zkey, str(exc))
                errors += 1
            sleep()

    # --- Logs ---
    if should_run("logs"):
        print(f"\n--- Logs ({len(data['logs'])}) ---")
        for log in data["logs"]:
            zkey = log.registry_key or ""
            existing = registry.get_entity(run_id, zkey)
            if existing and existing["status"] in ("success", "skipped"):
                continue
            if not log.customer_name:
                registry.mark_skipped(run_id, zkey, "no customer")
                continue
            if dry_run:
                continue
            cid = resolve_contact_ghl_id(registry, run_id, customer_name=log.customer_name)
            if not cid:
                registry.mark_failed(run_id, zkey, "no contact")
                errors += 1
                continue
            body = (
                f"Zoho Activity Log\nID: {log.activity_id}\nDate: {log.date}\n"
                f"{log.description}"
            )
            try:
                client.add_contact_tags(cid, ["zoho-log"])
                client.add_contact_activity(cid, log.transaction_name or "Zoho Activity", body)
                registry.mark_success(run_id, zkey, cid)
            except Exception as exc:
                registry.mark_failed(run_id, zkey, str(exc))
                errors += 1

    return errors


def write_reports(registry: MigrationRegistry, run_id: str, reports_dir: Path) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    summary = registry.export_summary(run_id)
    (reports_dir / f"summary_{run_id}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    failures = registry.recent_failures(run_id, limit=5000)
    if failures:
        import csv

        path = reports_dir / f"exceptions_{run_id}.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=["entity_type", "zoho_key", "display_label", "error", "updated_at"],
                extrasaction="ignore",
            )
            w.writeheader()
            w.writerows(failures)
