"""Load Zoho Books Excel and build registry keys."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pandas as pd

from migration.constants import ZOHO_TO_EXISTING_CF
from migration.models import (
    ContactRecord,
    CreditRecord,
    InvoiceRecord,
    LogRecord,
    PaymentRecord,
    RefundRecord,
)
from migration.utils import (
    date_str,
    money,
    name_key,
    normalize_phone,
    parse_display_name,
    str_val,
)


def contact_zoho_key(
    zoho_contact_id: str | None,
    zoho_customer_id: str | None,
    source_row: dict[str, Any],
) -> str:
    if zoho_contact_id:
        return f"contact:{zoho_contact_id}"
    if zoho_customer_id:
        return f"customer:{zoho_customer_id}"
    raw = str(source_row)
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"import:{h}"


def invoice_zoho_key(invoice_id: str | None, invoice_number: str) -> str:
    if invoice_id:
        return f"invoice:{invoice_id}"
    return f"invoice_num:{invoice_number}"


def payment_zoho_key(payment_id: str | None, invoice_number: str, date: str | None, amount: float) -> str:
    if payment_id:
        return f"payment:{payment_id}"
    return f"payment:{invoice_number}:{date or 'na'}:{money(amount)}"


def _contact_from_contact_row(row: pd.Series) -> ContactRecord:
    email = str_val(row.get("EmailID"))
    phone = normalize_phone(row.get("Phone") or row.get("MobilePhone"))
    first = str_val(row.get("First Name")) or ""
    last = str_val(row.get("Last Name")) or ""
    display = str_val(row.get("Display Name")) or ""
    if not first and not last and display:
        first, last = parse_display_name(display)

    cf: dict[str, str] = {}
    for zoho_col, ghl_name in ZOHO_TO_EXISTING_CF.items():
        v = str_val(row.get(zoho_col))
        if v:
            cf[ghl_name] = v
    zoho_cf_map = {
        "CF.Student Status": "Zoho Student Status",
        "CF.Student Email": "Zoho Student Email",
        "CF.Group Name": "Zoho Group Name",
        "CF.Frais d'inscription payés?": "Zoho Frais Inscription Payes",
        "CF.Date paiement frais": "Zoho Date Paiement Frais",
        "CF.IDAuto": "Zoho ID Auto",
    }
    for col, ghl_name in zoho_cf_map.items():
        v = str_val(row.get(col))
        if v:
            cf[ghl_name] = v
    status = str_val(row.get("Status"))
    if status:
        cf["Zoho Contact Status"] = status

    zcid = str_val(row.get("Contact ID"))
    rec = ContactRecord(
        email=email,
        first_name=first,
        last_name=last,
        phone=phone,
        company_name=str_val(row.get("Company Name")),
        address=str_val(row.get("Billing Address")),
        city=str_val(row.get("Billing City")),
        state=str_val(row.get("Billing State")),
        country=str_val(row.get("Billing Country")),
        postal_code=str_val(row.get("Billing Code")),
        tags=["zoho-import", "zoho-books-import", "zoho-contact"],
        custom_values=cf,
        zoho_contact_id=zcid,
        display_name=display or None,
    )
    rec.registry_key = contact_zoho_key(zcid, None, {"sheet": "contact", "display": display})
    return rec


def _contact_from_invoice_row(row: pd.Series) -> ContactRecord:
    customer = str_val(row.get("Customer Name")) or ""
    first, last = parse_display_name(customer)
    zcust = str_val(row.get("Customer ID"))
    rec = ContactRecord(
        email=str_val(row.get("Primary Contact EmailID")),
        first_name=first,
        last_name=last,
        phone=normalize_phone(
            row.get("Primary Contact Mobile")
            or row.get("Primary Contact Phone")
            or row.get("Billing Phone")
        ),
        address=str_val(row.get("Billing Address")),
        city=str_val(row.get("Billing City")),
        state=str_val(row.get("Billing State")),
        country=str_val(row.get("Billing Country")),
        postal_code=str_val(row.get("Billing Code")),
        tags=["zoho-import", "zoho-books-import", "zoho-invoice"],
        zoho_customer_id=zcust,
        display_name=customer or None,
    )
    rec.registry_key = contact_zoho_key(None, zcust, {"sheet": "invoice", "customer": customer})
    return rec


def _contact_from_name(name: str, tag: str) -> ContactRecord:
    first, last = parse_display_name(name)
    rec = ContactRecord(
        email=None,
        first_name=first,
        last_name=last,
        phone=None,
        tags=["zoho-import", "zoho-books-import", tag],
        display_name=name,
    )
    rec.registry_key = contact_zoho_key(None, None, {"sheet": tag, "name": name})
    return rec


def load_all(path: Path) -> dict[str, Any]:
    xl = pd.ExcelFile(path)
    out: dict[str, Any] = {
        "contacts": [],
        "invoices": [],
        "payments": [],
        "refunds": [],
        "credits": [],
        "logs": [],
    }

    if "contact" in xl.sheet_names:
        for _, row in pd.read_excel(xl, "contact").iterrows():
            out["contacts"].append(_contact_from_contact_row(row))

    if "invoice" in xl.sheet_names:
        for _, row in pd.read_excel(xl, "invoice").iterrows():
            inv_num = str_val(row.get("Invoice Number"))
            if not inv_num:
                continue
            inv = InvoiceRecord(
                invoice_number=inv_num,
                invoice_id=str_val(row.get("Invoice ID")),
                customer_name=str_val(row.get("Customer Name")) or "",
                customer_id=str_val(row.get("Customer ID")),
                email=str_val(row.get("Primary Contact EmailID")),
                phone=normalize_phone(
                    row.get("Primary Contact Mobile") or row.get("Primary Contact Phone")
                ),
                total=float(row.get("Total") or 0),
                currency=str_val(row.get("Currency Code")) or "CAD",
                invoice_date=date_str(row.get("Invoice Date")),
                due_date=date_str(row.get("Due Date")),
                status=str_val(row.get("Invoice Status")),
                item_name=str_val(row.get("Item Name")),
                balance=float(row.get("Balance") or 0),
            )
            inv.registry_key = invoice_zoho_key(inv.invoice_id, inv.invoice_number)
            out["invoices"].append(inv)
            out["contacts"].append(_contact_from_invoice_row(row))

    if "payment" in xl.sheet_names:
        for _, row in pd.read_excel(xl, "payment").iterrows():
            inv_num = str_val(row.get("Invoice Number"))
            if not inv_num:
                continue
            pay = PaymentRecord(
                invoice_number=inv_num,
                payment_id=str_val(row.get("CustomerPayment ID")),
                amount=float(row.get("Amount") or 0),
                date=date_str(row.get("Date")),
                mode=str_val(row.get("Mode")),
                customer_name=str_val(row.get("Customer Name")),
            )
            pay.registry_key = payment_zoho_key(pay.payment_id, inv_num, pay.date, pay.amount)
            out["payments"].append(pay)

    credit_note_to_invoice: dict[str, str] = {}

    if "credit" in xl.sheet_names:
        for _, row in pd.read_excel(xl, "credit").iterrows():
            cn_num = str_val(row.get("Credit Note Number")) or ""
            applied = str_val(row.get("Applied Invoice Number"))
            cred = CreditRecord(
                credit_note_id=str_val(row.get("CreditNotes ID")),
                credit_note_number=cn_num,
                customer_name=str_val(row.get("Customer Name")) or "",
                total=float(row.get("Total") or 0),
                date=date_str(row.get("Credit Note Date")),
                status=str_val(row.get("Credit Note Status")),
                applied_invoice=applied,
                description=str_val(row.get("Item Desc")),
            )
            cred.registry_key = (
                f"credit:{cred.credit_note_id}" if cred.credit_note_id else f"credit_num:{cn_num}"
            )
            out["credits"].append(cred)
            if cn_num and applied:
                credit_note_to_invoice[cn_num] = applied
            name = str_val(row.get("Customer Name"))
            if name:
                out["contacts"].append(_contact_from_name(name, "zoho-credit"))

    if "refund" in xl.sheet_names:
        for _, row in pd.read_excel(xl, "refund").iterrows():
            cn_num = str_val(row.get("Credit Note Number")) or ""
            ref = RefundRecord(
                credit_note_number=cn_num,
                customer_name=str_val(row.get("Customer Name")) or "",
                amount=float(row.get("Amount") or 0),
                date=date_str(row.get("Date")),
                mode=str_val(row.get("Mode of Refund")),
                description=str_val(row.get("Description")),
                reference=str_val(row.get("Reference Number")),
                applied_invoice=credit_note_to_invoice.get(cn_num),
            )
            ref.registry_key = f"refund:{cn_num}"
            out["refunds"].append(ref)
            name = str_val(row.get("Customer Name"))
            if name:
                out["contacts"].append(_contact_from_name(name, "zoho-refund"))

    if "log" in xl.sheet_names:
        for _, row in pd.read_excel(xl, "log").iterrows():
            log = LogRecord(
                activity_id=str_val(row.get("activity_id")),
                date=date_str(row.get("date")),
                description=str_val(row.get("description")),
                customer_name=str_val(row.get("customer_name")),
                transaction_type=str_val(row.get("transaction_type")),
                transaction_name=str_val(row.get("transaction_name")),
            )
            log.registry_key = (
                f"log:{log.activity_id}" if log.activity_id else f"log:{hash(str(row))}"
            )
            out["logs"].append(log)

    return out


def canonical_contacts(contacts: list[ContactRecord]) -> list[ContactRecord]:
    """Dedupe contact rows to one record per registry_key (merge tags)."""
    by_key: dict[str, ContactRecord] = {}
    for c in contacts:
        key = c.registry_key or contact_zoho_key(
            c.zoho_contact_id, c.zoho_customer_id, {"display": c.display_name}
        )
        c.registry_key = key
        if key not in by_key:
            by_key[key] = c
            continue
        existing = by_key[key]
        for t in c.tags:
            if t not in existing.tags:
                existing.tags.append(t)
        if c.email and not existing.email:
            existing.email = c.email
        if c.phone and not existing.phone:
            existing.phone = c.phone
        if c.zoho_contact_id and not existing.zoho_contact_id:
            existing.zoho_contact_id = c.zoho_contact_id
        if c.zoho_customer_id and not existing.zoho_customer_id:
            existing.zoho_customer_id = c.zoho_customer_id
        for k, v in c.custom_values.items():
            existing.custom_values.setdefault(k, v)
    return list(by_key.values())


def ingest_excel_to_registry(registry: Any, run_id: str, path: Path) -> dict[str, int]:
    """Load Excel and register all entities as pending."""
    from migration.registry import MigrationRegistry

    assert isinstance(registry, MigrationRegistry)
    data = load_all(path)
    counts: dict[str, int] = {}

    for c in canonical_contacts(data["contacts"]):
        label = c.display_name or f"{c.first_name} {c.last_name}".strip()
        registry.upsert_entity(
            run_id,
            "contact",
            c.registry_key or "",
            display_label=label,
            payload={
                "zoho_contact_id": c.zoho_contact_id,
                "zoho_customer_id": c.zoho_customer_id,
                "email": c.email,
                "display_name": c.display_name,
                "tags": c.tags,
            },
            status="pending",
        )
    counts["contact"] = len(canonical_contacts(data["contacts"]))

    for inv in data["invoices"]:
        registry.upsert_entity(
            run_id,
            "invoice",
            inv.registry_key or "",
            display_label=inv.invoice_number,
            payload=inv.__dict__,
            status="pending",
        )
    counts["invoice"] = len(data["invoices"])

    for pay in data["payments"]:
        registry.upsert_entity(
            run_id,
            "payment",
            pay.registry_key or "",
            display_label=f"{pay.invoice_number} ${pay.amount}",
            payload=pay.__dict__,
            status="pending",
        )
    counts["payment"] = len(data["payments"])

    for ref in data["refunds"]:
        registry.upsert_entity(
            run_id,
            "refund",
            ref.registry_key or "",
            display_label=ref.credit_note_number,
            payload=ref.__dict__,
            status="pending",
        )
    counts["refund"] = len(data["refunds"])

    for cred in data["credits"]:
        registry.upsert_entity(
            run_id,
            "credit",
            cred.registry_key or "",
            display_label=cred.credit_note_number,
            payload=cred.__dict__,
            status="pending",
        )
    counts["credit"] = len(data["credits"])

    for log in data["logs"]:
        if not log.customer_name:
            registry.upsert_entity(
                run_id,
                "log",
                log.registry_key or "",
                display_label=log.description[:80] if log.description else "log",
                payload=log.__dict__,
                status="skipped",
                error="no customer_name",
            )
        else:
            registry.upsert_entity(
                run_id,
                "log",
                log.registry_key or "",
                display_label=log.customer_name,
                payload=log.__dict__,
                status="pending",
            )
    counts["log"] = len(data["logs"])

    registry.log_event(run_id, "ingest", f"Ingested Excel {path}", detail=counts)
    return counts


def resolve_contact_ghl_id(
    registry: Any,
    run_id: str,
    *,
    zoho_contact_id: str | None = None,
    zoho_customer_id: str | None = None,
    customer_name: str | None = None,
) -> str | None:
    if zoho_contact_id:
        gid = registry.get_ghl_id(run_id, f"contact:{zoho_contact_id}")
        if gid:
            return gid
    if zoho_customer_id:
        gid = registry.get_ghl_id(run_id, f"customer:{zoho_customer_id}")
        if gid:
            return gid
    if customer_name:
        key = name_key(customer_name)
        for ent in registry.list_entities(run_id, entity_type="contact", status="success"):
            label = (ent.get("display_label") or "").lower()
            if name_key(label) == key:
                return ent.get("ghl_id")
    return None
