#!/usr/bin/env python3
"""OCR unmatched files, rename, move back to inbox for re-upload."""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from migration.matchers import extract_ids_from_text
from migration.ocr.extract import extract_text_from_file, match_text_to_contact, suggest_filename
from migration.registry import MigrationRegistry
from migration.utils import sha256_file

ATTACH_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="OCR + rename unmatched attachments")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", default=str(ROOT / "data" / "migration.db"))
    parser.add_argument("--use-claude", action="store_true", help="Allow Claude if ANTHROPIC_API_KEY set")
    args = parser.parse_args()

    registry = MigrationRegistry(Path(args.db))
    unmatched_dir = ROOT / "attachments" / "unmatched"
    failed_dir = ROOT / "attachments" / "failed"
    inbox = ROOT / "attachments" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    ocr_dir = ROOT / "data" / "ocr_text"
    ocr_dir.mkdir(parents=True, exist_ok=True)

    index = registry.contact_lookup_index(args.run_id)
    review: list[dict[str, str]] = []
    moved = 0

    sources = list(unmatched_dir.glob("*")) + list(failed_dir.glob("*"))
    files = [f for f in sources if f.is_file() and f.suffix.lower() in ATTACH_EXT]
    print(f"OCR queue: {len(files)} files")

    for fp in files:
        text, engine = extract_text_from_file(fp)
        if args.use_claude and engine == "none":
            from migration.ocr.extract import extract_text_claude

            text, engine = extract_text_claude(fp)

        ocr_path = ocr_dir / f"{fp.stem}.txt"
        ocr_path.write_text(text[:50000], encoding="utf-8")

        cid, method, conf = match_text_to_contact(text, index)
        if not cid:
            ids = extract_ids_from_text(text)
            review.append(
                {
                    "file": fp.name,
                    "engine": engine,
                    "method": method,
                    "confidence": str(conf),
                    "zoho_ids": ",".join(ids.get("zoho_ids", [])[:3]),
                }
            )
            registry.upsert_file(
                args.run_id,
                sha256_file(str(fp)),
                str(fp),
                status="unmatched",
                ocr_engine=engine,
                error="ocr_no_match",
            )
            print(f"  NO MATCH {fp.name} ({engine})")
            continue

        zoho_id = None
        for z in extract_ids_from_text(text)["zoho_ids"]:
            if z in index.get("by_zoho_contact", {}):
                zoho_id = z
                break
        if not zoho_id:
            for ent in index.get("contacts", []):
                if ent.get("ghl_id") == cid:
                    import json

                    try:
                        p = json.loads(ent.get("zoho_payload") or "{}")
                        zoho_id = p.get("zoho_contact_id") or p.get("zoho_customer_id")
                    except json.JSONDecodeError:
                        pass
                    break
        new_name = suggest_filename(zoho_id or "unknown", fp)
        dest = inbox / new_name
        shutil.move(str(fp), str(dest))
        registry.upsert_file(
            args.run_id,
            sha256_file(str(dest)),
            str(fp),
            current_path=str(dest),
            status="ocr_queued",
            ghl_contact_id=cid,
            match_method=method,
            match_confidence=conf,
            ocr_engine=engine,
        )
        moved += 1
        print(f"  REQUEUE {fp.name} -> {new_name} ({method})")

    review_path = ROOT / "reports" / f"ocr_manual_review_{args.run_id}.csv"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    if review:
        with review_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(review[0].keys()))
            w.writeheader()
            w.writerows(review)
        print(f"Manual review: {review_path} ({len(review)} rows)")

    print(f"Requeued: {moved}. Run: python scripts/03_attach_documents.py --run-id {args.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
