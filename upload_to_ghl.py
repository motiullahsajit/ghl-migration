#!/usr/bin/env python3
"""
Migrate Zoho Books export (sample.xlsx) to GoHighLevel via API v2.

Zoho → GHL mapping (see project requirements):
  1. Contacts   → GHL contacts (name, email, phone, address, company)
  2. Invoices   → GHL invoices, status Issued (API: sent via send_manually)
  3. Payments   → GHL Transactions via record-payment (Paid; supports partial amounts)
  4. Refunds    → GHL refund transaction when API allows; else tag Refund + activity
  5. Attachments → deferred (not implemented)
  6. Credit/Log → Contact activity notes on timeline

Workflow:
  python upload_to_ghl.py --audit
  python upload_to_ghl.py --setup
  python upload_to_ghl.py --dry-run
  python upload_to_ghl.py

Requires .env with GHL_API_KEY and GHL_LOCATION_ID.
Enable "Allow Duplicate Contact" in sub-account settings for duplicate imports.
Token scopes: contacts.write, locations/customFields.write, locations/tags.write,
  invoices.readonly, invoices.write, payments/transactions.readonly.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv

API_BASE = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"

# Tags used to segment imported records (GHL "lists" via tags)
REQUIRED_TAGS = [
    "zoho-import",
    "zoho-books-import",
    "zoho-contact",
    "zoho-invoice",
    "zoho-payment",
    "zoho-refund",
    "zoho-credit",
    "zoho-log",
    "Refund",  # requirement: refund records tagged as Refund
    "Paid",    # payment transactions applied to invoices
]

# New custom fields to create on the contact object if missing
REQUIRED_CUSTOM_FIELDS: list[tuple[str, str]] = [
    ("Zoho Contact ID", "TEXT"),
    ("Zoho Customer ID", "TEXT"),
    ("Zoho Display Name", "TEXT"),
    ("Zoho Contact Status", "TEXT"),
    ("Zoho Student Status", "TEXT"),
    ("Zoho Student Email", "TEXT"),
    ("Zoho Group Name", "TEXT"),
    ("Zoho Frais Inscription Payes", "TEXT"),
    ("Zoho Date Paiement Frais", "TEXT"),
    ("Zoho ID Auto", "TEXT"),
    ("Zoho Invoice ID", "TEXT"),
    ("Zoho Invoice Number", "TEXT"),
    ("Zoho Payment ID", "TEXT"),
    ("Zoho Credit Note ID", "TEXT"),
    ("Zoho Credit Note Number", "TEXT"),
    ("Zoho Refund Reference", "TEXT"),
    ("Zoho Activity Log", "LARGE_TEXT"),
]

# Map Zoho contact-sheet columns to existing GHL custom field names (by display name)
ZOHO_TO_EXISTING_CF: dict[str, str] = {
    "CF.CodePermanent": "Code permanent",
    "CF.NAS": "Numéro d'assurance sociale",
    "CF.Programme Suivi": "Programme souhaité",
}


@dataclass
class ContactRecord:
    email: str | None
    first_name: str
    last_name: str
    phone: str | None
    company_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    postal_code: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str = "Zoho Books Import"
    custom_values: dict[str, str] = field(default_factory=dict)
    zoho_contact_id: str | None = None
    zoho_customer_id: str | None = None
    display_name: str | None = None


@dataclass
class InvoiceRecord:
    invoice_number: str
    invoice_id: str | None
    customer_name: str
    customer_id: str | None
    email: str | None
    phone: str | None
    total: float
    currency: str
    invoice_date: str | None
    due_date: str | None
    status: str | None
    item_name: str | None
    balance: float = 0.0


@dataclass
class PaymentRecord:
    invoice_number: str
    payment_id: str | None
    amount: float
    date: str | None
    mode: str | None
    customer_name: str | None


@dataclass
class RefundRecord:
    credit_note_number: str
    customer_name: str
    amount: float
    date: str | None
    mode: str | None
    description: str | None
    reference: str | None
    applied_invoice: str | None = None


@dataclass
class CreditRecord:
    credit_note_id: str | None
    credit_note_number: str
    customer_name: str
    total: float
    date: str | None
    status: str | None
    applied_invoice: str | None
    description: str | None


@dataclass
class LogRecord:
    activity_id: str | None
    date: str | None
    description: str | None
    customer_name: str | None
    transaction_type: str | None
    transaction_name: str | None


def _is_empty(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, float) and pd.isna(val):
        return True
    if isinstance(val, str) and not val.strip():
        return True
    return False


def _str_val(val: Any) -> str | None:
    if _is_empty(val):
        return None
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val).strip()


def _date_str(val: Any) -> str | None:
    if _is_empty(val):
        return None
    return pd.Timestamp(val).strftime("%Y-%m-%d")


def normalize_phone(phone: Any) -> str | None:
    raw = _str_val(phone)
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if len(set(digits)) <= 2:
        return None
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}" if not raw.startswith("+") else raw


def parse_display_name(name: str) -> tuple[str, str]:
    name = name.strip()
    name = re.sub(
        r"^(M\.|Mme|Mr\.|Mrs\.|Ms\.|Dr\.|Mlle)\s+",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()
    parts = name.split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (" ".join(parts[:-1]), parts[-1])


def _money(val: float) -> float:
    return round(float(val), 2)


def effective_due_date(zoho_due: str | None, zoho_issue: str | None) -> str:
    """GHL rejects past due dates on create; use Zoho due date when still in the future."""
    today = date.today()
    for candidate in (zoho_due, zoho_issue):
        if candidate:
            d = pd.Timestamp(candidate).date()
            if d >= today:
                return d.isoformat()
    return (today + timedelta(days=30)).isoformat()


class GHLClient:
    def __init__(self, api_key: str, location_id: str) -> None:
        self.location_id = location_id
        self._business_details: dict[str, Any] | None = None
        self._custom_fields_by_name: dict[str, dict[str, Any]] | None = None
        self._tags_by_name: dict[str, dict[str, Any]] | None = None
        self._default_user_id: str | None = None
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Version": API_VERSION,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        resp = self.session.request(method, url, timeout=90, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text}")
        if resp.text:
            return resp.json()
        return {}

    def _invoice_location_params(self) -> dict[str, str]:
        return {"altId": self.location_id, "altType": "location"}

    def _invoice_location_context(self) -> dict[str, str]:
        return {"altId": self.location_id, "altType": "location"}

    def get_default_user_id(self) -> str | None:
        if self._default_user_id is not None:
            return self._default_user_id
        resp = self.session.get(
            f"{API_BASE}/users/",
            params={"locationId": self.location_id},
            timeout=60,
        )
        if resp.status_code >= 400:
            return None
        users = (resp.json() or {}).get("users") or []
        if users:
            self._default_user_id = users[0].get("id")
        return self._default_user_id

    def get_custom_fields(self, refresh: bool = False) -> dict[str, dict[str, Any]]:
        if self._custom_fields_by_name is not None and not refresh:
            return self._custom_fields_by_name
        data = self._request("GET", f"/locations/{self.location_id}/customFields")
        fields = data.get("customFields") or []
        self._custom_fields_by_name = {
            (f.get("name") or "").strip().lower(): f for f in fields
        }
        return self._custom_fields_by_name

    def get_tags(self, refresh: bool = False) -> dict[str, dict[str, Any]]:
        if self._tags_by_name is not None and not refresh:
            return self._tags_by_name
        data = self._request("GET", f"/locations/{self.location_id}/tags")
        tags = data.get("tags") or []
        self._tags_by_name = {
            (t.get("name") or "").strip().lower(): t for t in tags
        }
        return self._tags_by_name

    def check_invoice_permissions(self) -> dict[str, bool]:
        """Probe which invoice endpoints the current token can actually call."""
        url = f"{API_BASE}/invoices/"
        list_ok = False
        create_ok = False
        get_by_id_ok = False
        send_ok = False
        payment_ok = False
        probe_id: str | None = None

        resp = self.session.get(
            url,
            params={"altId": self.location_id, "altType": "location", "limit": "1", "offset": "0"},
            timeout=60,
        )
        list_ok = resp.status_code < 400
        if list_ok:
            invoices = (resp.json() or {}).get("invoices") or []
            if invoices:
                probe_id = invoices[0].get("_id")

        if not probe_id:
            biz = self.get_business_details()
            create_resp = self.session.post(
                url,
                json={
                    "altId": self.location_id,
                    "altType": "location",
                    "name": "ZOHO-TOKEN-PROBE",
                    "businessDetails": biz,
                    "contactDetails": {
                        "name": "Token Probe",
                        "email": "zoho-probe@books-import.local",
                        "phone": "+15555550199",
                    },
                    "currency": "CAD",
                    "items": [{"name": "Probe", "currency": "CAD", "amount": 1, "qty": 1}],
                    "issueDate": date.today().isoformat(),
                    "dueDate": (date.today() + timedelta(days=30)).isoformat(),
                },
                timeout=60,
            )
            create_ok = create_resp.status_code < 400
            if create_ok:
                probe_id = (create_resp.json() or {}).get("_id")
        else:
            create_ok = True

        inv_params = self._invoice_location_params()
        inv_ctx = self._invoice_location_context()
        if probe_id:
            get_resp = self.session.get(
                f"{url}{probe_id}", params=inv_params, timeout=60
            )
            get_by_id_ok = get_resp.status_code < 400
            uid = self.get_default_user_id()
            send_body: dict[str, Any] = {
                "action": "send_manually",
                "liveMode": True,
                **inv_ctx,
            }
            if uid:
                send_body["userId"] = uid
            send_resp = self.session.post(
                f"{url}{probe_id}/send",
                params=inv_params,
                json=send_body,
                timeout=60,
            )
            send_ok = send_resp.status_code < 400
            inv_status = (get_resp.json() or {}).get("status") if get_by_id_ok else None
            if inv_status == "paid":
                payment_ok = True
            else:
                pay_resp = self.session.post(
                    f"{url}{probe_id}/record-payment",
                    params=inv_params,
                    json={"mode": "cash", "amount": 1, **inv_ctx},
                    timeout=60,
                )
                payment_ok = pay_resp.status_code < 400

        return {
            "list": list_ok,
            "create": create_ok,
            "get_by_id": get_by_id_ok,
            "send": send_ok,
            "record_payment": payment_ok,
        }

    def check_duplicates_allowed(self) -> bool:
        """Probe whether location allows duplicate contacts via POST /contacts/."""
        probe = {
            "locationId": self.location_id,
            "firstName": "Zoho",
            "lastName": "DuplicateProbe",
            "email": f"zoho-dup-probe-{int(time.time())}@invalid.local",
        }
        url = f"{API_BASE}/contacts/"
        resp = self.session.post(url, json=probe, timeout=60)
        if resp.status_code == 201:
            cid = (resp.json().get("contact") or resp.json()).get("id")
            if cid:
                try:
                    self._request("DELETE", f"/contacts/{cid}")
                except Exception:
                    pass
            return True
        text = resp.text.lower()
        if "does not allow duplicated contacts" in text:
            return False
        if "duplicate" in text and resp.status_code == 400:
            return False
        return True

    def audit(self) -> dict[str, Any]:
        fields = self.get_custom_fields(refresh=True)
        tags = self.get_tags(refresh=True)
        missing_fields = [
            name for name, _ in REQUIRED_CUSTOM_FIELDS if name.lower() not in fields
        ]
        missing_tags = [t for t in REQUIRED_TAGS if t.lower() not in tags]
        dup_ok = self.check_duplicates_allowed()
        invoice_perms = self.check_invoice_permissions()
        return {
            "custom_field_count": len(fields),
            "tag_count": len(tags),
            "missing_fields": missing_fields,
            "missing_tags": missing_tags,
            "duplicates_allowed": dup_ok,
            "invoice_permissions": invoice_perms,
        }

    def setup(self, dry_run: bool = False) -> dict[str, list[str]]:
        created_fields: list[str] = []
        created_tags: list[str] = []
        fields = self.get_custom_fields(refresh=True)
        tags = self.get_tags(refresh=True)

        for name, data_type in REQUIRED_CUSTOM_FIELDS:
            if name.lower() in fields:
                continue
            if dry_run:
                created_fields.append(f"(dry-run) {name}")
                continue
            self._request(
                "POST",
                f"/locations/{self.location_id}/customFields",
                json={"name": name, "dataType": data_type, "placeholder": name},
            )
            created_fields.append(name)
            time.sleep(0.1)

        for tag_name in REQUIRED_TAGS:
            if tag_name.lower() in tags:
                continue
            if dry_run:
                created_tags.append(f"(dry-run) {tag_name}")
                continue
            self._request(
                "POST",
                f"/locations/{self.location_id}/tags",
                json={"name": tag_name},
            )
            created_tags.append(tag_name)
            time.sleep(0.1)

        self.get_custom_fields(refresh=True)
        self.get_tags(refresh=True)
        return {"created_fields": created_fields, "created_tags": created_tags}

    def resolve_cf_payload(self, values: dict[str, str]) -> list[dict[str, str]]:
        fields = self.get_custom_fields()
        payload: list[dict[str, str]] = []
        for name, value in values.items():
            if not value:
                continue
            cf = fields.get(name.strip().lower())
            if not cf:
                continue
            entry: dict[str, str] = {
                "id": cf["id"],
                "field_value": value,
            }
            if cf.get("fieldKey"):
                entry["key"] = cf["fieldKey"]
            payload.append(entry)
        return payload

    def get_business_details(self) -> dict[str, Any]:
        if self._business_details is not None:
            return self._business_details
        data = self._request("GET", f"/locations/{self.location_id}")
        loc = data.get("location") or data
        biz = loc.get("business") or {}
        self._business_details = {
            "name": biz.get("name") or loc.get("name") or "Business",
            "email": loc.get("email") or "",
            "phone": loc.get("phone") or "",
            "address": {
                "addressLine1": biz.get("address") or loc.get("address") or "",
                "city": biz.get("city") or loc.get("city") or "",
                "state": biz.get("state") or loc.get("state") or "",
                "countryCode": biz.get("country") or loc.get("country") or "CA",
                "postalCode": biz.get("postalCode") or loc.get("postalCode") or "",
            },
        }
        return self._business_details

    def _fallback_email(self, contact: ContactRecord) -> str:
        if contact.email:
            return contact.email
        token = contact.zoho_customer_id or contact.zoho_contact_id or re.sub(
            r"[^a-z0-9]+",
            "-",
            (contact.display_name or contact.first_name or "contact").lower(),
        )
        return f"zoho-{token}@books-import.local"

    def _fallback_phone(self, contact: ContactRecord) -> str:
        if contact.phone:
            return contact.phone
        key = (
            getattr(contact, "registry_key", None)
            or contact.zoho_contact_id
            or contact.zoho_customer_id
            or contact.display_name
            or "contact"
        )
        token = re.sub(r"[^a-z0-9]+", "-", str(key).lower())
        n = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:7], 16) % 9_000_000 + 1_000_000
        return f"+1{n}"

    def create_contact(self, contact: ContactRecord) -> str:
        """Create contact (POST /contacts/) — respects location duplicate settings."""
        email = contact.email or self._fallback_email(contact)
        phone = self._fallback_phone(contact)
        payload: dict[str, Any] = {
            "locationId": self.location_id,
            "firstName": contact.first_name or "Unknown",
            "lastName": contact.last_name or "",
            "tags": contact.tags,
            "source": contact.source,
            "email": email,
            "phone": phone,
        }
        if contact.company_name:
            payload["companyName"] = contact.company_name
        if contact.address:
            payload["address1"] = contact.address
        if contact.city:
            payload["city"] = contact.city
        if contact.state:
            payload["state"] = contact.state
        if contact.country:
            payload["country"] = contact.country
        if contact.postal_code:
            payload["postalCode"] = contact.postal_code

        cf_values = dict(contact.custom_values)
        if contact.zoho_contact_id:
            cf_values["Zoho Contact ID"] = contact.zoho_contact_id
        if contact.zoho_customer_id:
            cf_values["Zoho Customer ID"] = contact.zoho_customer_id
        if contact.display_name:
            cf_values["Zoho Display Name"] = contact.display_name

        cf_payload = self.resolve_cf_payload(cf_values)
        if cf_payload:
            payload["customFields"] = cf_payload

        data = self._request("POST", "/contacts/", json=payload)
        contact_obj = data.get("contact") or data
        contact_id = contact_obj.get("id")
        if not contact_id:
            raise RuntimeError(f"No contact id in response: {data}")
        return str(contact_id)

    def add_contact_activity(self, contact_id: str, title: str, body: str) -> None:
        """Add entry to contact timeline (Notes / activity)."""
        self._request(
            "POST",
            f"/contacts/{contact_id}/notes",
            json={"title": title[:200], "body": body},
        )

    def add_contact_tags(self, contact_id: str, tags: list[str]) -> None:
        if not tags:
            return
        self._request(
            "POST",
            f"/contacts/{contact_id}/tags",
            json={"tags": tags},
        )

    def update_contact_custom_fields(
        self, contact_id: str, values: dict[str, str]
    ) -> None:
        cf_payload = self.resolve_cf_payload(values)
        if not cf_payload:
            return
        self._request(
            "PUT",
            f"/contacts/{contact_id}",
            json={"customFields": cf_payload},
        )

    @staticmethod
    def _payment_mode(mode: str | None) -> str:
        mode_map = {
            "credit card": "card",
            "bank transfer": "bank_transfer",
            "bank remittance": "bank_transfer",
            "cash": "cash",
        }
        return mode_map.get((mode or "").lower(), "other")

    def get_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        resp = self.session.get(
            f"{API_BASE}/invoices/{invoice_id}",
            params=self._invoice_location_params(),
            timeout=60,
        )
        if resp.status_code >= 400:
            return None
        return resp.json() or {}

    def get_invoice_status(self, invoice_id: str) -> str | None:
        inv = self.get_invoice(invoice_id)
        return inv.get("status") if inv else None

    def get_invoice_total(self, invoice_id: str) -> float | None:
        inv = self.get_invoice(invoice_id)
        if not inv:
            return None
        for key in ("total", "amount", "invoiceTotal"):
            if inv.get(key) is not None:
                return _money(float(inv[key]))
        items = inv.get("items") or []
        if items:
            return _money(
                sum(float(i.get("amount") or 0) * float(i.get("qty") or 1) for i in items)
            )
        return None

    def list_transactions_for_invoice(self, invoice_id: str) -> list[dict[str, Any]]:
        resp = self.session.get(
            f"{API_BASE}/payments/transactions",
            params={
                **self._invoice_location_params(),
                "entityId": invoice_id,
                "limit": "100",
                "offset": "0",
            },
            timeout=60,
        )
        if resp.status_code >= 400:
            resp = self.session.get(
                f"{API_BASE}/payments/transactions/",
                params={**self._invoice_location_params(), "limit": "100", "offset": "0"},
                timeout=60,
            )
        if resp.status_code >= 400:
            return []
        rows = (resp.json() or {}).get("data") or []
        return [
            t
            for t in rows
            if t.get("entityId") == invoice_id or t.get("invoiceId") == invoice_id
        ]

    def _find_payment_transaction(
        self, invoice_id: str, amount: float
    ) -> dict[str, Any] | None:
        target = _money(amount)
        matches = [
            t
            for t in self.list_transactions_for_invoice(invoice_id)
            if _money(float(t.get("amount") or 0)) == target
            and (t.get("status") or "").lower() in ("succeeded", "paid", "complete")
        ]
        return matches[-1] if matches else None

    def issue_invoice(self, invoice_id: str) -> list[str]:
        """Mark invoice as Issued in GHL (API status: sent)."""
        warnings: list[str] = []
        inv_params = self._invoice_location_params()
        inv_ctx = self._invoice_location_context()
        uid = self.get_default_user_id()
        send_body: dict[str, Any] = {
            "action": "send_manually",
            "liveMode": True,
            **inv_ctx,
        }
        if uid:
            send_body["userId"] = uid
        try:
            resp = self.session.post(
                f"{API_BASE}/invoices/{invoice_id}/send",
                params=inv_params,
                json=send_body,
                timeout=60,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"{resp.status_code}: {resp.text}")
        except RuntimeError as exc:
            warnings.append(f"issue: {exc}")
        return warnings

    def record_invoice_payment(
        self,
        invoice_id: str,
        payment: PaymentRecord,
        invoice_total: float | None = None,
        paid_so_far: float = 0.0,
    ) -> tuple[list[str], str | None]:
        """Record payment on invoice; creates GHL Transaction linked to invoice."""
        warnings: list[str] = []
        amount = _money(payment.amount)
        if amount <= 0:
            warnings.append(f"payment: skipped non-positive amount {amount}")
            return warnings, None

        if invoice_total is not None:
            remaining = _money(invoice_total - paid_so_far)
            if amount > remaining + 0.01:
                warnings.append(
                    f"payment: amount {amount} exceeds invoice balance {remaining}"
                )
                return warnings, None

        inv_params = self._invoice_location_params()
        inv_ctx = self._invoice_location_context()
        mode = self._payment_mode(payment.mode)
        notes = f"Zoho Books payment {payment.payment_id or ''}".strip()
        body: dict[str, Any] = {
            "mode": mode,
            "amount": amount,
            "notes": notes,
            **inv_ctx,
        }
        if payment.date:
            body["paymentDate"] = payment.date

        try:
            resp = self.session.post(
                f"{API_BASE}/invoices/{invoice_id}/record-payment",
                params=inv_params,
                json=body,
                timeout=60,
            )
            if resp.status_code >= 400 and payment.date:
                body.pop("paymentDate", None)
                resp = self.session.post(
                    f"{API_BASE}/invoices/{invoice_id}/record-payment",
                    params=inv_params,
                    json=body,
                    timeout=60,
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"{resp.status_code}: {resp.text}")
        except RuntimeError as exc:
            warnings.append(f"payment: {exc}")
            return warnings, None

        txn = self._find_payment_transaction(invoice_id, amount)
        txn_id = txn.get("_id") if txn else None
        if not txn_id:
            txns = self.list_transactions_for_invoice(invoice_id)
            if txns:
                txn_id = txns[-1].get("_id")
        return warnings, str(txn_id) if txn_id else None

    def create_invoice(
        self,
        inv: InvoiceRecord,
        contact_id: str,
        contact: ContactRecord | None = None,
    ) -> str:
        issue = inv.invoice_date or date.today().isoformat()
        due = effective_due_date(inv.due_date, inv.invoice_date)
        item_label = (inv.item_name or "Invoice line item")[:200]
        currency = (inv.currency or "CAD").upper()

        email = inv.email
        phone = inv.phone
        if contact:
            email = email or contact.email or self._fallback_email(contact)
            phone = phone or contact.phone or self._fallback_phone(contact)
        if not email:
            email = f"zoho-{inv.invoice_number.lower()}@books-import.local"
        if not phone:
            phone = self._fallback_phone(
                ContactRecord(email=email, first_name="", last_name="", phone=None)
            )

        contact_details: dict[str, Any] = {
            "id": contact_id,
            "name": inv.customer_name,
            "email": email,
            "phone": phone,
        }

        payload: dict[str, Any] = {
            "altId": self.location_id,
            "altType": "location",
            "name": inv.invoice_number,
            "title": inv.invoice_number,
            "businessDetails": self.get_business_details(),
            "contactDetails": contact_details,
            "currency": currency,
            "items": [
                {
                    "name": item_label,
                    "currency": currency,
                    "amount": inv.total,
                    "qty": 1,
                }
            ],
            "issueDate": issue,
            "dueDate": due,
        }

        data = self._request("POST", "/invoices/", json=payload)
        invoice_id = data.get("_id") or (data.get("invoice") or {}).get("_id")
        if not invoice_id:
            raise RuntimeError(f"No invoice id in response: {data}")
        return str(invoice_id)

    def finalize_invoice(
        self,
        invoice_id: str,
        inv: InvoiceRecord,
        payment: PaymentRecord | None = None,
    ) -> list[str]:
        """Issue invoice, then record payment when Zoho shows it as closed/paid."""
        warnings = self.issue_invoice(invoice_id)
        if payment and (
            inv.balance == 0 or (inv.status or "").lower() == "closed"
        ):
            pay_warns, _ = self.record_invoice_payment(invoice_id, payment)
            warnings.extend(pay_warns)
        return warnings

    def _record_refund_activity_fallback(
        self,
        contact_id: str,
        refund: RefundRecord,
        warnings: list[str],
    ) -> None:
        body = (
            f"Zoho Refund (imported)\n"
            f"Credit Note: {refund.credit_note_number}\n"
            f"Amount: {refund.amount}\n"
            f"Date: {refund.date}\n"
            f"Mode: {refund.mode}\n"
            f"Description: {refund.description}\n"
            f"Reference: {refund.reference}\n"
            f"Applied invoice: {refund.applied_invoice or 'n/a'}\n"
            f"Tag: Refund\n"
            f"Note: GHL public API has no refund-create endpoint; "
            f"recorded as contact activity."
        )
        self.add_contact_tags(contact_id, ["Refund", "zoho-refund"])
        try:
            self.update_contact_custom_fields(
                contact_id,
                {
                    "Zoho Refund Reference": refund.reference or refund.credit_note_number,
                    "Zoho Credit Note Number": refund.credit_note_number,
                },
            )
        except RuntimeError as exc:
            warnings.append(f"refund cf: {exc}")
        self.add_contact_activity(
            contact_id,
            f"Refund — {refund.credit_note_number}",
            body,
        )

    def record_refund(
        self,
        contact_id: str,
        refund: RefundRecord,
        invoice_id: str | None = None,
    ) -> tuple[list[str], str | None, str]:
        """
        Record refund as GHL transaction when supported; otherwise tag + activity.
        Returns (warnings, transaction_id, method) where method is 'transaction' or 'activity'.
        """
        warnings: list[str] = []
        inv_params = self._invoice_location_params()
        inv_ctx = self._invoice_location_context()
        mode = self._payment_mode(refund.mode)
        amount = _money(refund.amount)
        notes = (
            f"Zoho refund {refund.credit_note_number}: "
            f"{refund.description or refund.reference or ''}"
        ).strip()

        if invoice_id and amount > 0:
            refund_attempts: list[tuple[str, dict[str, Any]]] = []
            for txn in self.list_transactions_for_invoice(invoice_id):
                txn_id = txn.get("_id")
                if not txn_id:
                    continue
                if _money(float(txn.get("amount") or 0)) <= 0:
                    continue
                refund_attempts.append(
                    (
                        f"{API_BASE}/payments/transactions/{txn_id}/refund",
                        {"amount": amount, **inv_ctx},
                    )
                )
            refund_attempts.extend(
                [
                    (
                        f"{API_BASE}/invoices/{invoice_id}/refund-payment",
                        {
                            "amount": amount,
                            "mode": mode,
                            "notes": notes,
                            **inv_ctx,
                        },
                    ),
                    (
                        f"{API_BASE}/invoices/{invoice_id}/record-payment",
                        {
                            "mode": mode,
                            "amount": amount,
                            "isRefund": True,
                            "notes": notes,
                            **inv_ctx,
                        },
                    ),
                ]
            )

            for url, body in refund_attempts:
                resp = self.session.post(
                    url,
                    params=inv_params,
                    json=body,
                    timeout=60,
                )
                if resp.status_code < 400:
                    self.add_contact_tags(contact_id, ["Refund", "zoho-refund"])
                    try:
                        self.update_contact_custom_fields(
                            contact_id,
                            {
                                "Zoho Refund Reference": refund.reference
                                or refund.credit_note_number,
                                "Zoho Credit Note Number": refund.credit_note_number,
                            },
                        )
                    except RuntimeError as exc:
                        warnings.append(f"refund cf: {exc}")
                    txns = self.list_transactions_for_invoice(invoice_id)
                    txn = txns[-1] if txns else None
                    txn_id_out = (txn or {}).get("_id")
                    return warnings, str(txn_id_out) if txn_id_out else None, "transaction"
                if resp.status_code not in (404, 405):
                    warnings.append(
                        f"refund try {url.split(API_BASE)[-1]}: "
                        f"{resp.status_code} {resp.text[:180]}"
                    )

        self._record_refund_activity_fallback(contact_id, refund, warnings)
        return warnings, None, "activity"

    def finalize_all_draft_invoices(self) -> int:
        """Issue any draft invoices (mark as sent/Issued)."""
        errors = 0
        offset = "0"
        while True:
            resp = self.session.get(
                f"{API_BASE}/invoices/",
                params={
                    **self._invoice_location_params(),
                    "limit": "50",
                    "offset": offset,
                },
                timeout=60,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"list invoices -> {resp.status_code}: {resp.text}")
            invoices = (resp.json() or {}).get("invoices") or []
            if not invoices:
                break
            for inv_obj in invoices:
                iid = inv_obj.get("_id")
                name = inv_obj.get("name") or iid
                status = inv_obj.get("status")
                if status in ("sent", "paid", "partially_paid"):
                    print(f"  skip {name} ({status})")
                    continue
                warns = self.issue_invoice(iid)
                new_status = self.get_invoice_status(iid) or "?"
                print(f"  {name}: {status} -> {new_status}")
                for w in warns:
                    print(f"    warn: {w}", file=sys.stderr)
                if new_status not in ("sent", "paid", "partially_paid"):
                    errors += 1
                time.sleep(0.2)
            if len(invoices) < 50:
                break
            offset = str(int(offset) + 50)
        return errors


def _contact_from_contact_row(row: pd.Series) -> ContactRecord:
    email = _str_val(row.get("EmailID"))
    phone = normalize_phone(row.get("Phone") or row.get("MobilePhone"))
    first = _str_val(row.get("First Name")) or ""
    last = _str_val(row.get("Last Name")) or ""
    display = _str_val(row.get("Display Name")) or ""
    if not first and not last and display:
        first, last = parse_display_name(display)

    cf: dict[str, str] = {}
    for zoho_col, ghl_name in ZOHO_TO_EXISTING_CF.items():
        v = _str_val(row.get(zoho_col))
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
        v = _str_val(row.get(col))
        if v:
            cf[ghl_name] = v

    status = _str_val(row.get("Status"))
    if status:
        cf["Zoho Contact Status"] = status

    return ContactRecord(
        email=email,
        first_name=first,
        last_name=last,
        phone=phone,
        company_name=_str_val(row.get("Company Name")),
        address=_str_val(row.get("Billing Address")),
        city=_str_val(row.get("Billing City")),
        state=_str_val(row.get("Billing State")),
        country=_str_val(row.get("Billing Country")),
        postal_code=_str_val(row.get("Billing Code")),
        tags=["zoho-import", "zoho-books-import", "zoho-contact"],
        custom_values=cf,
        zoho_contact_id=_str_val(row.get("Contact ID")),
        display_name=display or None,
    )


def _contact_from_invoice_row(row: pd.Series) -> ContactRecord:
    customer = _str_val(row.get("Customer Name")) or ""
    first, last = parse_display_name(customer)
    email = _str_val(row.get("Primary Contact EmailID"))
    phone = normalize_phone(
        row.get("Primary Contact Mobile")
        or row.get("Primary Contact Phone")
        or row.get("Billing Phone")
    )
    return ContactRecord(
        email=email,
        first_name=first,
        last_name=last,
        phone=phone,
        address=_str_val(row.get("Billing Address")),
        city=_str_val(row.get("Billing City")),
        state=_str_val(row.get("Billing State")),
        country=_str_val(row.get("Billing Country")),
        postal_code=_str_val(row.get("Billing Code")),
        tags=["zoho-import", "zoho-books-import", "zoho-invoice"],
        zoho_customer_id=_str_val(row.get("Customer ID")),
        display_name=customer or None,
    )


def _contact_from_name(
    name: str,
    tag: str,
    zoho_customer_id: str | None = None,
) -> ContactRecord:
    first, last = parse_display_name(name)
    return ContactRecord(
        email=None,
        first_name=first,
        last_name=last,
        phone=None,
        tags=["zoho-import", "zoho-books-import", tag],
        zoho_customer_id=zoho_customer_id,
        display_name=name,
    )


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
            inv_num = _str_val(row.get("Invoice Number"))
            if not inv_num:
                continue
            out["invoices"].append(
                InvoiceRecord(
                    invoice_number=inv_num,
                    invoice_id=_str_val(row.get("Invoice ID")),
                    customer_name=_str_val(row.get("Customer Name")) or "",
                    customer_id=_str_val(row.get("Customer ID")),
                    email=_str_val(row.get("Primary Contact EmailID")),
                    phone=normalize_phone(
                        row.get("Primary Contact Mobile")
                        or row.get("Primary Contact Phone")
                    ),
                    total=float(row.get("Total") or 0),
                    currency=_str_val(row.get("Currency Code")) or "CAD",
                    invoice_date=_date_str(row.get("Invoice Date")),
                    due_date=_date_str(row.get("Due Date")),
                    status=_str_val(row.get("Invoice Status")),
                    item_name=_str_val(row.get("Item Name")),
                    balance=float(row.get("Balance") or 0),
                )
            )
            out["contacts"].append(_contact_from_invoice_row(row))

    if "payment" in xl.sheet_names:
        for _, row in pd.read_excel(xl, "payment").iterrows():
            inv_num = _str_val(row.get("Invoice Number"))
            if not inv_num:
                continue
            out["payments"].append(
                PaymentRecord(
                    invoice_number=inv_num,
                    payment_id=_str_val(row.get("CustomerPayment ID")),
                    amount=float(row.get("Amount") or 0),
                    date=_date_str(row.get("Date")),
                    mode=_str_val(row.get("Mode")),
                    customer_name=_str_val(row.get("Customer Name")),
                )
            )

    credit_note_to_invoice: dict[str, str] = {}

    if "credit" in xl.sheet_names:
        for _, row in pd.read_excel(xl, "credit").iterrows():
            cn_num = _str_val(row.get("Credit Note Number")) or ""
            applied = _str_val(row.get("Applied Invoice Number"))
            out["credits"].append(
                CreditRecord(
                    credit_note_id=_str_val(row.get("CreditNotes ID")),
                    credit_note_number=cn_num,
                    customer_name=_str_val(row.get("Customer Name")) or "",
                    total=float(row.get("Total") or 0),
                    date=_date_str(row.get("Credit Note Date")),
                    status=_str_val(row.get("Credit Note Status")),
                    applied_invoice=applied,
                    description=_str_val(row.get("Item Desc")),
                )
            )
            if cn_num and applied:
                credit_note_to_invoice[cn_num] = applied
            name = _str_val(row.get("Customer Name"))
            if name:
                out["contacts"].append(_contact_from_name(name, "zoho-credit"))

    if "refund" in xl.sheet_names:
        for _, row in pd.read_excel(xl, "refund").iterrows():
            cn_num = _str_val(row.get("Credit Note Number")) or ""
            out["refunds"].append(
                RefundRecord(
                    credit_note_number=cn_num,
                    customer_name=_str_val(row.get("Customer Name")) or "",
                    amount=float(row.get("Amount") or 0),
                    date=_date_str(row.get("Date")),
                    mode=_str_val(row.get("Mode of Refund")),
                    description=_str_val(row.get("Description")),
                    reference=_str_val(row.get("Reference Number")),
                    applied_invoice=credit_note_to_invoice.get(cn_num),
                )
            )
            name = _str_val(row.get("Customer Name"))
            if name:
                out["contacts"].append(_contact_from_name(name, "zoho-refund"))

    if "log" in xl.sheet_names:
        for _, row in pd.read_excel(xl, "log").iterrows():
            out["logs"].append(
                LogRecord(
                    activity_id=_str_val(row.get("activity_id")),
                    date=_date_str(row.get("date")),
                    description=_str_val(row.get("description")),
                    customer_name=_str_val(row.get("customer_name")),
                    transaction_type=_str_val(row.get("transaction_type")),
                    transaction_name=_str_val(row.get("transaction_name")),
                )
            )

    return out


def _name_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def migrate(
    client: GHLClient,
    data: dict[str, Any],
    dry_run: bool = False,
) -> int:
    errors = 0
    name_to_contact_ids: dict[str, list[str]] = {}
    zoho_customer_to_contact: dict[str, str] = {}
    invoice_number_to_id: dict[str, str] = {}
    invoice_totals: dict[str, float] = {
        inv.invoice_number: _money(inv.total) for inv in data["invoices"]
    }
    invoice_paid_so_far: dict[str, float] = {}
    contact_records_by_id: dict[str, ContactRecord] = {}
    payments_by_invoice: dict[str, list[PaymentRecord]] = {}
    for pay in data["payments"]:
        payments_by_invoice.setdefault(pay.invoice_number, []).append(pay)

    print(f"\n--- Contacts ({len(data['contacts'])} rows) ---")
    for i, contact in enumerate(data["contacts"], 1):
        label = contact.display_name or f"{contact.first_name} {contact.last_name}".strip()
        if dry_run:
            print(f"  [{i}] would create: {label} | {contact.email or 'no email'}")
            fake_id = f"dry-run-{i}"
        else:
            try:
                cid = client.create_contact(contact)
                print(f"  [{i}] OK {label} -> {cid}")
                key = _name_key(label)
                name_to_contact_ids.setdefault(key, []).append(cid)
                if contact.zoho_customer_id:
                    zoho_customer_to_contact[contact.zoho_customer_id] = cid
                contact_records_by_id[cid] = contact
                time.sleep(0.15)
                fake_id = cid
            except RuntimeError as exc:
                print(f"  [{i}] FAIL {label}: {exc}", file=sys.stderr)
                errors += 1
                continue

        key = _name_key(label)
        name_to_contact_ids.setdefault(key, []).append(fake_id)

    def resolve_contact_id(inv: InvoiceRecord) -> str | None:
        if inv.customer_id and inv.customer_id in zoho_customer_to_contact:
            return zoho_customer_to_contact[inv.customer_id]
        ids = name_to_contact_ids.get(_name_key(inv.customer_name), [])
        return ids[-1] if ids else None

    print(f"\n--- Invoices ({len(data['invoices'])}) ---")
    for inv in data["invoices"]:
        cid = resolve_contact_id(inv)
        if not cid or cid.startswith("dry-run"):
            if dry_run and not cid:
                print(f"  would SKIP {inv.invoice_number}: no contact")
                continue
        if dry_run:
            print(
                f"  would create {inv.invoice_number} issue={inv.invoice_date} "
                f"total={inv.total} status={inv.status}"
            )
            invoice_number_to_id[inv.invoice_number] = f"dry-{inv.invoice_number}"
            continue
        if not cid:
            print(f"  SKIP {inv.invoice_number}: no contact for {inv.customer_name}")
            errors += 1
            continue
        try:
            inv_contact = contact_records_by_id.get(cid)
            iid = client.create_invoice(inv, cid, inv_contact)
            warns = client.issue_invoice(iid)
            invoice_number_to_id[inv.invoice_number] = iid
            cf_values: dict[str, str] = {"Zoho Invoice Number": inv.invoice_number}
            if inv.invoice_id:
                cf_values["Zoho Invoice ID"] = inv.invoice_id
            try:
                client.update_contact_custom_fields(cid, cf_values)
            except RuntimeError:
                pass
            ghl_status = client.get_invoice_status(iid) or "draft"
            status_note = " (+warnings)" if warns else ""
            print(
                f"  OK {inv.invoice_number} issue={inv.invoice_date} "
                f"-> {iid} [{ghl_status}]{status_note}"
            )
            for w in warns:
                print(f"      warn: {w}", file=sys.stderr)
            time.sleep(0.2)
        except RuntimeError as exc:
            print(f"  FAIL {inv.invoice_number}: {exc}", file=sys.stderr)
            errors += 1

    payment_count = sum(len(v) for v in payments_by_invoice.values())
    print(f"\n--- Payments ({payment_count} on {len(payments_by_invoice)} invoices) ---")
    for inv_num in sorted(payments_by_invoice.keys()):
        payments = payments_by_invoice[inv_num]
        inv_total = invoice_totals.get(inv_num)
        paid_so_far = invoice_paid_so_far.get(inv_num, 0.0)
        for pay in payments:
            remaining = (
                _money(inv_total - paid_so_far) if inv_total is not None else None
            )
            if dry_run:
                partial = (
                    f" (partial, {paid_so_far + pay.amount}/{inv_total})"
                    if inv_total and paid_so_far + pay.amount < inv_total - 0.01
                    else ""
                )
                print(
                    f"  would record transaction {inv_num} "
                    f"${pay.amount}{partial} (Paid, offset invoice)"
                )
                paid_so_far = _money(paid_so_far + pay.amount)
                continue
            iid = invoice_number_to_id.get(inv_num)
            if not iid:
                print(f"  SKIP {inv_num}: invoice not created")
                errors += 1
                continue
            try:
                warns, txn_id = client.record_invoice_payment(
                    iid,
                    pay,
                    invoice_total=inv_total,
                    paid_so_far=paid_so_far,
                )
                if any("exceeds invoice balance" in w for w in warns):
                    errors += 1
                paid_so_far = _money(paid_so_far + pay.amount)
                invoice_paid_so_far[inv_num] = paid_so_far
                ghl_status = client.get_invoice_status(iid) or "?"
                cid = resolve_contact_id(
                    InvoiceRecord(
                        invoice_number=inv_num,
                        invoice_id=None,
                        customer_name=pay.customer_name or "",
                        customer_id=None,
                        email=None,
                        phone=None,
                        total=pay.amount,
                        currency="CAD",
                        invoice_date=pay.date,
                        due_date=None,
                        status=None,
                        item_name=None,
                    )
                )
                if cid and txn_id:
                    client.add_contact_tags(cid, ["Paid", "zoho-payment"])
                    try:
                        client.update_contact_custom_fields(
                            cid,
                            {
                                "Zoho Payment ID": pay.payment_id or "",
                                "Zoho Invoice Number": inv_num,
                            },
                        )
                    except RuntimeError:
                        pass
                partial_note = ""
                if inv_total and paid_so_far < inv_total - 0.01:
                    partial_note = f" partial {paid_so_far}/{inv_total}"
                txn_label = f" txn={txn_id}" if txn_id else ""
                warn_label = " (+warnings)" if warns else ""
                print(
                    f"  OK {inv_num} ${pay.amount} "
                    f"invoice={ghl_status}{partial_note}{txn_label}{warn_label}"
                )
                for w in warns:
                    print(f"      warn: {w}", file=sys.stderr)
                time.sleep(0.2)
            except RuntimeError as exc:
                print(f"  FAIL {inv_num}: {exc}", file=sys.stderr)
                errors += 1
        if dry_run and inv_num:
            invoice_paid_so_far[inv_num] = paid_so_far

    print(f"\n--- Refunds ({len(data['refunds'])}) ---")
    for ref in data["refunds"]:
        inv_label = f" invoice={ref.applied_invoice}" if ref.applied_invoice else ""
        if dry_run:
            print(
                f"  would record Refund transaction for {ref.customer_name}: "
                f"{ref.credit_note_number} ${ref.amount}{inv_label}"
            )
            continue
        ids = name_to_contact_ids.get(_name_key(ref.customer_name), [])
        if not ids:
            print(f"  SKIP refund {ref.credit_note_number}: no contact")
            errors += 1
            continue
        invoice_id = None
        if ref.applied_invoice:
            invoice_id = invoice_number_to_id.get(ref.applied_invoice)
            if not invoice_id:
                print(
                    f"  WARN refund {ref.credit_note_number}: "
                    f"invoice {ref.applied_invoice} not in this import",
                    file=sys.stderr,
                )
        try:
            warns, txn_id, method = client.record_refund(ids[-1], ref, invoice_id)
            method_label = "transaction" if method == "transaction" else "activity"
            txn_label = f" txn={txn_id}" if txn_id else ""
            warn_label = " (+warnings)" if warns else ""
            print(
                f"  OK Refund {ref.credit_note_number} on {ref.customer_name} "
                f"[{method_label}]{txn_label}{warn_label}"
            )
            for w in warns:
                print(f"      warn: {w}", file=sys.stderr)
            time.sleep(0.1)
        except RuntimeError as exc:
            print(f"  FAIL refund {ref.credit_note_number}: {exc}", file=sys.stderr)
            errors += 1

    print(f"\n--- Credit notes ({len(data['credits'])}) ---")
    for cred in data["credits"]:
        body = (
            f"Zoho Credit Note (imported)\n"
            f"Number: {cred.credit_note_number}\n"
            f"ID: {cred.credit_note_id}\n"
            f"Total: {cred.total}\n"
            f"Date: {cred.date}\n"
            f"Status: {cred.status}\n"
            f"Applied to invoice: {cred.applied_invoice}\n"
            f"Description: {cred.description}"
        )
        if dry_run:
            print(
                f"  would add activity for {cred.customer_name}: "
                f"{cred.credit_note_number}"
            )
            continue
        ids = name_to_contact_ids.get(_name_key(cred.customer_name), [])
        if not ids:
            print(f"  SKIP credit {cred.credit_note_number}: no contact")
            errors += 1
            continue
        try:
            client.add_contact_tags(ids[-1], ["zoho-credit"])
            client.add_contact_activity(
                ids[-1],
                f"Credit Note — {cred.credit_note_number}",
                body,
            )
            try:
                client.update_contact_custom_fields(
                    ids[-1],
                    {
                        "Zoho Credit Note ID": cred.credit_note_id or "",
                        "Zoho Credit Note Number": cred.credit_note_number,
                    },
                )
            except RuntimeError:
                pass
            print(f"  OK credit activity {cred.credit_note_number} on {cred.customer_name}")
            time.sleep(0.1)
        except RuntimeError as exc:
            print(f"  FAIL credit {cred.credit_note_number}: {exc}", file=sys.stderr)
            errors += 1

    print(f"\n--- Activity log ({len(data['logs'])}) ---")
    for log in data["logs"]:
        body = (
            f"Zoho Activity Log (imported)\n"
            f"ID: {log.activity_id}\n"
            f"Date: {log.date}\n"
            f"Type: {log.transaction_type}\n"
            f"Transaction: {log.transaction_name}\n"
            f"{log.description}"
        )
        if dry_run:
            print(
                f"  would add activity: "
                f"{log.description[:60] if log.description else log.activity_id}"
            )
            continue
        if not log.customer_name:
            print(f"  SKIP log (no customer): {log.description[:50] if log.description else '?'}")
            continue
        ids = name_to_contact_ids.get(_name_key(log.customer_name), [])
        if not ids:
            print(f"  SKIP log for {log.customer_name}: no contact")
            errors += 1
            continue
        try:
            title = log.transaction_name or "Zoho Activity"
            client.add_contact_tags(ids[-1], ["zoho-log"])
            client.add_contact_activity(ids[-1], title, body)
            if log.activity_id:
                try:
                    client.update_contact_custom_fields(
                        ids[-1],
                        {"Zoho Activity Log": log.activity_id},
                    )
                except RuntimeError:
                    pass
            print(f"  OK activity log on {log.customer_name}")
            time.sleep(0.1)
        except RuntimeError as exc:
            print(f"  FAIL log: {exc}", file=sys.stderr)
            errors += 1

    return errors


def print_audit_report(report: dict[str, Any]) -> None:
    print(f"  Custom fields in location: {report['custom_field_count']}")
    print(f"  Tags in location: {report['tag_count']}")
    print(f"  Allow duplicate contacts: {report['duplicates_allowed']}")
    perms = report.get("invoice_permissions") or {}
    if perms:
        print("  Invoice API (this token — not UI checkboxes):")
        labels = {
            "list": "List invoices",
            "create": "Create invoice",
            "get_by_id": "Get invoice by ID",
            "send": "Issue invoice (send_manually -> sent)",
            "record_payment": "Record payment",
        }
        for key, label in labels.items():
            mark = "OK" if perms.get(key) else "DENIED"
            print(f"    [{mark}] {label}")
        if not perms.get("get_by_id") or not perms.get("record_payment"):
            print(
                "\n  Invoice API access incomplete. Ensure invoices.readonly and\n"
                "  invoices.write are enabled, then regenerate the token in .env.\n",
                file=sys.stderr,
            )
    if not report["duplicates_allowed"]:
        print(
            "\n  ACTION REQUIRED: Enable Settings → Allow Duplicate Contact\n"
            "  https://help.gohighlevel.com/support/solutions/articles/48001181714",
            file=sys.stderr,
        )
    if report["missing_fields"]:
        print(f"  Missing custom fields ({len(report['missing_fields'])}):")
        for f in report["missing_fields"]:
            print(f"    - {f}")
    else:
        print("  All required custom fields present.")
    if report["missing_tags"]:
        print(f"  Missing tags ({len(report['missing_tags'])}):")
        for t in report["missing_tags"]:
            print(f"    - {t}")
    else:
        print("  All required tags present.")
    print(
        "\n  Refunds: script tries GHL refund endpoints when a credit note links to an\n"
        "  imported invoice; otherwise records tag Refund + contact activity."
    )


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Migrate Zoho Books Excel to GoHighLevel")
    parser.add_argument("--audit", action="store_true", help="Audit GHL fields, tags, duplicates")
    parser.add_argument("--setup", action="store_true", help="Create missing custom fields and tags")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Run migration (default when neither --audit nor --setup alone)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without API writes")
    parser.add_argument(
        "--finalize-invoices",
        action="store_true",
        help="Mark draft invoices as paid (for already-imported data)",
    )
    parser.add_argument("--excel", default=os.getenv("GHL_EXCEL_PATH", "sample.xlsx"))
    args = parser.parse_args()

    api_key = os.getenv("GHL_API_KEY", "").strip()
    location_id = os.getenv("GHL_LOCATION_ID", "").strip()
    excel_path = Path(args.excel)

    if not api_key or not location_id:
        print("Set GHL_API_KEY and GHL_LOCATION_ID in .env", file=sys.stderr)
        return 1

    client = GHLClient(api_key, location_id)
    run_migrate = args.migrate or (
        not args.audit and not args.setup and not args.finalize_invoices
    )

    if args.finalize_invoices:
        print("=== Finalize draft invoices ===")
        try:
            errors = client.finalize_all_draft_invoices()
        except RuntimeError as exc:
            print(f"Failed: {exc}", file=sys.stderr)
            return 1
        print(f"\nDone. Errors: {errors}")
        return 1 if errors else 0

    if args.audit or args.setup or run_migrate:
        print("=== GHL Audit ===")
        try:
            report = client.audit()
        except RuntimeError as exc:
            print(f"Audit failed: {exc}", file=sys.stderr)
            return 1
        print_audit_report(report)

    if args.audit and not args.setup and not run_migrate:
        return 0

    if args.setup:
        print("\n=== GHL Setup ===")
        try:
            result = client.setup(dry_run=args.dry_run)
        except RuntimeError as exc:
            print(f"Setup failed: {exc}", file=sys.stderr)
            return 1
        print(f"  Created fields: {len(result['created_fields'])}")
        for f in result["created_fields"]:
            print(f"    + {f}")
        print(f"  Created tags: {len(result['created_tags'])}")
        for t in result["created_tags"]:
            print(f"    + {t}")

    if not run_migrate:
        return 0

    if not excel_path.is_file():
        print(f"Excel not found: {excel_path}", file=sys.stderr)
        return 1

    if args.setup and not args.dry_run:
        report = client.audit()
        if not report["duplicates_allowed"]:
            print(
                "\nWarning: duplicates still disabled — enable before migrating contacts.\n",
                file=sys.stderr,
            )

    data = load_all(excel_path)
    print(f"\n=== Migration ===")
    print(f"Excel: {excel_path}")
    print(f"  contact sheet rows: {sum(1 for c in data['contacts'] if 'zoho-contact' in c.tags)}")
    print(f"  invoices: {len(data['invoices'])}")
    print(f"  payments: {len(data['payments'])}")
    print(f"  refunds: {len(data['refunds'])}")
    print(f"  credits: {len(data['credits'])}")
    print(f"  log entries: {len(data['logs'])}")
    print(f"  total contact creates: {len(data['contacts'])}")

    errors = migrate(client, data, dry_run=args.dry_run)
    print(f"\nDone. Errors: {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
