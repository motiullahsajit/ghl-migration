"""Shared utilities."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, timedelta
from typing import Any

import pandas as pd


def is_empty(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, float) and pd.isna(val):
        return True
    if isinstance(val, str) and not val.strip():
        return True
    return False


def str_val(val: Any) -> str | None:
    if is_empty(val):
        return None
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val).strip()


def date_str(val: Any) -> str | None:
    if is_empty(val):
        return None
    return pd.Timestamp(val).strftime("%Y-%m-%d")


def money(val: float) -> float:
    return round(float(val), 2)


def normalize_phone(phone: Any) -> str | None:
    raw = str_val(phone)
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


def unique_import_phone(zoho_key: str) -> str:
    """Deterministic unique phone per registry key (avoids GHL duplicate-phone collisions)."""
    n = int(hashlib.sha256(zoho_key.encode("utf-8")).hexdigest()[:7], 16) % 9_000_000 + 1_000_000
    return f"+1{n}"


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


def name_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def normalize_name_tokens(name: str) -> set[str]:
    """Accent-insensitive tokens for matching (min length 2)."""
    nfd = unicodedata.normalize("NFD", name.lower())
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    stripped = re.sub(
        r"^(m\.|mme|mr\.|mrs\.|ms\.|dr\.|mlle)\s+",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    tokens = {t for t in re.split(r"[\s\-_]+", stripped) if len(t) >= 2}
    return tokens


def effective_due_date(zoho_due: str | None, zoho_issue: str | None) -> str:
    today = date.today()
    for candidate in (zoho_due, zoho_issue):
        if candidate:
            d = pd.Timestamp(candidate).date()
            if d >= today:
                return d.isoformat()
    return (today + timedelta(days=30)).isoformat()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
