"""Tiered text extraction: PyMuPDF → Tesseract (optional Claude via env)."""

from __future__ import annotations

import os
import re
from pathlib import Path

from migration.matchers import extract_ids_from_text
from migration.utils import normalize_name_tokens


def extract_text_pymupdf(path: Path, max_pages: int = 2) -> str:
    try:
        import fitz  # pymupdf
    except ImportError:
        return ""
    text_parts: list[str] = []
    try:
        doc = fitz.open(path)
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            text_parts.append(page.get_text())
        doc.close()
    except Exception:
        return ""
    return "\n".join(text_parts)


def extract_text_tesseract(path: Path) -> tuple[str, str]:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return "", ""
    tess_cmd = os.getenv("TESSERACT_CMD")
    if tess_cmd:
        pytesseract.pytesseract.tesseract_cmd = tess_cmd
    try:
        if path.suffix.lower() == ".pdf":
            try:
                import fitz

                doc = fitz.open(path)
                page = doc[0]
                pix = page.get_pixmap()
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                doc.close()
            except Exception:
                return "", ""
        else:
            img = Image.open(path)
        lang = os.getenv("OCR_LANG", "fra+eng")
        text = pytesseract.image_to_string(img, lang=lang)
        return text, "tesseract"
    except Exception:
        return "", ""


def extract_text_claude(path: Path) -> tuple[str, str]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return "", ""
    try:
        import base64
        import json
        import urllib.request

        suffix = path.suffix.lower()
        media = "image/png" if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp") else "application/pdf"
        data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        body = {
            "model": os.getenv("CLAUDE_OCR_MODEL", "claude-sonnet-4-20250514"),
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image" if media.startswith("image") else "document",
                            "source": {
                                "type": "base64",
                                "media_type": media,
                                "data": data,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Extract plain text. Return JSON only: "
                                '{"zoho_contact_id":null,"invoice_number":null,"person_name":null}'
                            ),
                        },
                    ],
                }
            ],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode())
        blocks = payload.get("content") or []
        text = blocks[0].get("text", "") if blocks else ""
        return text, "claude"
    except Exception:
        return "", ""


def extract_text_from_file(path: Path) -> tuple[str, str]:
    """Returns (text, engine_name)."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        text = extract_text_pymupdf(path)
        if len(text.strip()) >= 30:
            return text, "pymupdf"
    else:
        text = ""
    tess, eng = extract_text_tesseract(path)
    if len(tess.strip()) >= 20:
        return tess, eng
    if os.getenv("ANTHROPIC_API_KEY"):
        claude_text, eng = extract_text_claude(path)
        if claude_text.strip():
            return claude_text, eng
    return text or tess, "none"


def match_text_to_contact(
    text: str,
    index: dict,
    *,
    min_name_tokens: int = 2,
) -> tuple[str | None, str, float]:
    from migration.matchers import match_file_to_contact

    ids = extract_ids_from_text(text)
    by_contact = index.get("by_zoho_contact") or {}
    by_customer = index.get("by_zoho_customer") or {}
    for zid in ids["zoho_ids"]:
        if zid in by_contact:
            return by_contact[zid], "ocr_contact_id", 0.95
        if zid in by_customer:
            return by_customer[zid], "ocr_customer_id", 0.93

    tokens = frozenset(normalize_name_tokens(text[:2000]))
    by_name = index.get("by_name_tokens") or {}
    best = None
    for reg_tokens, entries in by_name.items():
        overlap = tokens & reg_tokens
        if len(overlap) < min_name_tokens:
            continue
        score = len(overlap) / max(len(tokens), len(reg_tokens))
        if entries and (not best or score > best[2]):
            best = (entries[0]["ghl_id"], "ocr_name", score)
    if best and best[2] >= 0.75:
        return best
    return None, "ocr_none", 0.0


def suggest_filename(zoho_contact_id: str, original: Path) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", original.stem)[:40]
    return f"{zoho_contact_id}_{slug}{original.suffix.lower()}"
