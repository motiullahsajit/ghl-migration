"""Data models for Zoho Excel rows."""

from __future__ import annotations

from dataclasses import dataclass, field


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
    registry_key: str | None = None


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
    registry_key: str | None = None


@dataclass
class PaymentRecord:
    invoice_number: str
    payment_id: str | None
    amount: float
    date: str | None
    mode: str | None
    customer_name: str | None
    registry_key: str | None = None


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
    registry_key: str | None = None


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
    registry_key: str | None = None


@dataclass
class LogRecord:
    activity_id: str | None
    date: str | None
    description: str | None
    customer_name: str | None
    transaction_type: str | None
    transaction_name: str | None
    registry_key: str | None = None
