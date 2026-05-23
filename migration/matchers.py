"""Match attachment files to registry contacts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from migration.utils import normalize_name_tokens

ZOHO_ID_PATTERN = re.compile(r"\b(\d{10,20})\b")
INV_PATTERN = re.compile(r"\b(INV-\d+)\b", re.IGNORECASE)


def extract_ids_from_text(text: str) -> dict[str, list[str]]:
    return {
        "zoho_ids": ZOHO_ID_PATTERN.findall(text),
        "invoice_numbers": [x.upper() for x in INV_PATTERN.findall(text)],
    }


def extract_ids_from_filename(path: str | Path) -> dict[str, list[str]]:
    name = Path(path).stem
    return extract_ids_from_text(name)


def match_file_to_contact(
    path: str | Path,
    index: dict[str, Any],
    *,
    min_name_tokens: int = 2,
    min_confidence: float = 0.85,
) -> tuple[str | None, str, float]:
    """
    Returns (ghl_contact_id, match_method, confidence).
    """
    by_contact = index.get("by_zoho_contact") or {}
    by_customer = index.get("by_zoho_customer") or {}
    by_tokens: dict[frozenset[str], list] = index.get("by_name_tokens") or {}

    ids = extract_ids_from_filename(path)
    for zid in ids["zoho_ids"]:
        if zid in by_contact:
            return by_contact[zid], "filename_contact_id", 1.0
        if zid in by_customer:
            return by_customer[zid], "filename_customer_id", 0.98

    name_part = Path(path).stem.replace("_", " ").replace("-", " ")
    tokens = frozenset(normalize_name_tokens(name_part))
    if len(tokens) >= min_name_tokens:
        best: tuple[str, str, float] | None = None
        for reg_tokens, entries in by_tokens.items():
            overlap = tokens & reg_tokens
            if len(overlap) < min_name_tokens:
                continue
            score = len(overlap) / max(len(tokens), len(reg_tokens))
            if score >= min_confidence and entries:
                cand = (entries[0]["ghl_id"], "filename_name", score)
                if not best or score > best[2]:
                    best = cand
        if best:
            return best

    return None, "none", 0.0
