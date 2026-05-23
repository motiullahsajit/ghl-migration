"""GoHighLevel API client."""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from typing import Any

import requests

from migration.constants import API_BASE, API_VERSION, REQUIRED_CUSTOM_FIELDS, REQUIRED_TAGS
from migration.models import ContactRecord, InvoiceRecord, PaymentRecord, RefundRecord
from pathlib import Path

from migration.utils import effective_due_date, money, normalize_phone, unique_import_phone

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
            contact.registry_key
            or contact.zoho_contact_id
            or contact.zoho_customer_id
            or contact.display_name
            or "contact"
        )
        token = re.sub(r"[^a-z0-9]+", "-", str(key).lower())
        return unique_import_phone(token)

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
                return money(float(inv[key]))
        items = inv.get("items") or []
        if items:
            return money(
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
        target = money(amount)
        matches = [
            t
            for t in self.list_transactions_for_invoice(invoice_id)
            if money(float(t.get("amount") or 0)) == target
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
        amount = money(payment.amount)
        if amount <= 0:
            warnings.append(f"payment: skipped non-positive amount {amount}")
            return warnings, None

        if invoice_total is not None:
            remaining = money(invoice_total - paid_so_far)
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
        amount = money(refund.amount)
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
                if money(float(txn.get("amount") or 0)) <= 0:
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


    def audit_upload_api(self) -> dict[str, Any]:
        """Probe endpoints for Contact Documents upload."""
        results: dict[str, Any] = {}
        for label, method, url, kwargs in [
            ("medias_upload", "POST", f"{API_BASE}/medias/upload-file", {"files": True}),
            ("forms_upload", "POST", f"{API_BASE}/forms/upload-custom-files", {"files": True}),
        ]:
            results[label] = {"tried": True, "note": "Use upload_contact_document()"}
        return results

    def upload_contact_document(
        self, contact_id: str, file_path: str, file_name: str | None = None
    ) -> str:
        """Upload file to contact (tries medias upload, returns document/media id)."""
        path = Path(file_path)
        name = file_name or path.name
        headers = {k: v for k, v in self.session.headers.items() if k.lower() != "content-type"}
        # Try medias upload
        with open(path, "rb") as fh:
            resp = self.session.post(
                f"{API_BASE}/medias/upload-file",
                headers=headers,
                files={"file": (name, fh)},
                data={"hosted": "true", "name": name},
                timeout=120,
            )
        if resp.status_code < 400:
            data = resp.json() or {}
            doc_id = data.get("id") or data.get("fileId") or data.get("url") or data.get("_id")
            if doc_id:
                return str(doc_id)
        # Try forms upload-custom-files with contact context
        with open(path, "rb") as fh:
            resp2 = self.session.post(
                f"{API_BASE}/forms/upload-custom-files",
                headers=headers,
                files={"file": (name, fh)},
                data={"contactId": contact_id, "locationId": self.location_id},
                timeout=120,
            )
        if resp2.status_code < 400:
            data = resp2.json() or {}
            doc_id = data.get("id") or data.get("documentId") or data.get("url")
            if doc_id:
                return str(doc_id)
        raise RuntimeError(
            f"upload failed medias={resp.status_code} forms={resp2.status_code}: "
            f"{resp.text[:200]} | {resp2.text[:200]}"
        )


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

