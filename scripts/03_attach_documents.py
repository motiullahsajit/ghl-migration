#!/usr/bin/env python3
"""Match and upload attachments to GHL Contact Documents."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from migration.attachment_index import (
    discover_attachment_files,
    load_csv_mappings,
    match_file_with_csv_index,
)
from migration.ghl_client import GHLClient
from migration.registry import MigrationRegistry
from migration.utils import sha256_file


def ensure_dirs(root: Path) -> dict[str, Path]:
    d = {
        "inbox": root / "attachments" / "inbox",
        "uploaded": root / "attachments" / "uploaded",
        "unmatched": root / "attachments" / "unmatched",
        "failed": root / "attachments" / "failed",
    }
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d


def _archive_path(dirs: dict[str, Path], fp: Path, sub: str) -> Path:
    """Avoid basename collisions when archiving from nested folders."""
    safe = fp.name
    if sub == "uploaded":
        dest = dirs["uploaded"] / safe
    elif sub == "unmatched":
        dest = dirs["unmatched"] / safe
    else:
        dest = dirs["failed"] / safe
    if dest.exists():
        stem = fp.stem[:40]
        dest = dest.parent / f"{stem}_{sha256_file(str(fp))[:8]}{fp.suffix.lower()}"
    return dest


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Upload attachments to GHL contacts")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", default=str(ROOT / "data" / "migration.db"))
    parser.add_argument(
        "--attachments-dir",
        default=os.getenv("ATTACHMENTS_DIR", ""),
        help="Folder with PDFs/images + CSV mapping files (scanned recursively)",
    )
    parser.add_argument(
        "--inbox",
        default="",
        help="Legacy: single inbox folder (used if --attachments-dir not set)",
    )
    parser.add_argument(
        "--move-after-upload",
        action="store_true",
        help="Move file out of source folder after upload (default: copy to archive only)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--audit-upload-api", action="store_true")
    parser.add_argument("--min-confidence", type=float, default=0.85)
    args = parser.parse_args()

    api_key = os.getenv("GHL_API_KEY", "").strip()
    location_id = os.getenv("GHL_LOCATION_ID", "").strip()
    if not api_key or not location_id:
        print("Set GHL_API_KEY and GHL_LOCATION_ID", file=sys.stderr)
        return 1

    client = GHLClient(api_key, location_id)
    if args.audit_upload_api:
        print(client.audit_upload_api())
        return 0

    registry = MigrationRegistry(Path(args.db))
    dirs = ensure_dirs(ROOT)

    if args.attachments_dir:
        source_root = Path(args.attachments_dir)
    elif args.inbox:
        source_root = Path(args.inbox)
    else:
        source_root = ROOT / "attachments" / "inbox"

    if not source_root.is_dir():
        print(f"Attachments folder not found: {source_root}", file=sys.stderr)
        return 1

    registry_index = registry.contact_lookup_index(args.run_id)
    csv_lookup, csv_warnings = load_csv_mappings(source_root, registry, args.run_id)
    for w in csv_warnings[:20]:
        print(f"  CSV warn: {w}", file=sys.stderr)
    if len(csv_warnings) > 20:
        print(f"  ... and {len(csv_warnings) - 20} more CSV warnings", file=sys.stderr)

    files = discover_attachment_files(source_root)
    print(f"Source folder: {source_root}")
    print(f"Attachment files found: {len(files)}")
    print(f"CSV mappings loaded: {len(csv_lookup)} keys")

    errors = 0
    for fp in files:
        digest = sha256_file(str(fp))
        prev = registry.get_file_by_sha(args.run_id, digest)
        if prev and prev["status"] == "uploaded":
            print(f"  SKIP (uploaded) {fp.relative_to(source_root)}")
            continue

        cid, method, conf = match_file_with_csv_index(
            fp,
            source_root,
            csv_lookup,
            registry_index,
            min_confidence=args.min_confidence,
        )
        rel = fp.relative_to(source_root)

        if not cid:
            dest = _archive_path(dirs, fp, "unmatched")
            if not args.dry_run:
                if args.move_after_upload:
                    shutil.move(str(fp), str(dest))
                else:
                    shutil.copy2(str(fp), str(dest))
            registry.upsert_file(
                args.run_id,
                digest,
                str(fp),
                current_path=str(dest),
                status="unmatched",
                match_method=method,
                match_confidence=conf,
            )
            print(f"  UNMATCHED {rel}")
            continue

        if args.dry_run:
            print(f"  would upload {rel} -> {cid} ({method} {conf:.2f})")
            continue

        try:
            doc_id = client.upload_contact_document(cid, str(fp))
            dest = _archive_path(dirs, fp, "uploaded")
            if not args.dry_run:
                if args.move_after_upload:
                    shutil.move(str(fp), str(dest))
                else:
                    shutil.copy2(str(fp), str(dest))
            registry.upsert_file(
                args.run_id,
                digest,
                str(fp),
                current_path=str(dest),
                status="uploaded",
                ghl_contact_id=cid,
                ghl_document_id=doc_id,
                match_method=method,
                match_confidence=conf,
            )
            registry.log_event(
                args.run_id,
                "file_uploaded",
                str(rel),
                detail={"contact_id": cid, "doc_id": doc_id, "method": method},
            )
            print(f"  OK {rel} -> {cid} doc={doc_id}")
        except Exception as exc:
            dest = _archive_path(dirs, fp, "failed")
            if not args.dry_run:
                if args.move_after_upload and fp.exists():
                    shutil.move(str(fp), str(dest))
                elif fp.exists():
                    shutil.copy2(str(fp), str(dest))
            registry.upsert_file(
                args.run_id,
                digest,
                str(fp),
                current_path=str(dest),
                status="failed",
                ghl_contact_id=cid,
                match_method=method,
                error=str(exc)[:500],
            )
            print(f"  FAIL {rel}: {exc}", file=sys.stderr)
            errors += 1

    print(f"Done. Errors: {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
