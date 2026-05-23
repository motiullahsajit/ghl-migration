"""Shared constants for Zoho → GHL migration."""

API_BASE = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"

REQUIRED_TAGS = [
    "zoho-import",
    "zoho-books-import",
    "zoho-contact",
    "zoho-invoice",
    "zoho-payment",
    "zoho-refund",
    "zoho-credit",
    "zoho-log",
    "Refund",
    "Paid",
]

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

ZOHO_TO_EXISTING_CF: dict[str, str] = {
    "CF.CodePermanent": "Code permanent",
    "CF.NAS": "Numéro d'assurance sociale",
    "CF.Programme Suivi": "Programme souhaité",
}

ENTITY_TYPES = (
    "contact",
    "invoice",
    "payment",
    "refund",
    "credit",
    "log",
)

STATUSES = ("pending", "in_progress", "success", "failed", "skipped")

FILE_STATUSES = ("pending", "matched", "uploaded", "unmatched", "failed", "ocr_queued")
