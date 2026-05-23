"""SQLite migration registry — idempotency, resume, audit."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MigrationRegistry:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS migration_run (
                    run_id TEXT PRIMARY KEY,
                    excel_path TEXT,
                    started_at TEXT NOT NULL,
                    status TEXT DEFAULT 'active',
                    notes TEXT
                );

                CREATE TABLE IF NOT EXISTS entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    zoho_key TEXT NOT NULL,
                    display_label TEXT,
                    zoho_payload TEXT,
                    ghl_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    retry_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, zoho_key)
                );

                CREATE INDEX IF NOT EXISTS idx_entities_run_type
                    ON entities(run_id, entity_type, status);

                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    current_path TEXT,
                    file_name TEXT,
                    match_method TEXT,
                    match_confidence REAL,
                    ghl_contact_id TEXT,
                    ghl_document_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    ocr_engine TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, sha256)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    entity_type TEXT,
                    zoho_key TEXT,
                    message TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id DESC);
                """
            )

    def ensure_run(self, run_id: str, excel_path: str | None = None) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT run_id FROM migration_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO migration_run (run_id, excel_path, started_at, status) VALUES (?, ?, ?, 'active')",
                    (run_id, excel_path, utc_now()),
                )
            elif excel_path:
                conn.execute(
                    "UPDATE migration_run SET excel_path = ? WHERE run_id = ?",
                    (excel_path, run_id),
                )

    def log_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        *,
        entity_type: str | None = None,
        zoho_key: str | None = None,
        detail: Any = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO events (run_id, event_type, entity_type, zoho_key, message, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event_type,
                    entity_type,
                    zoho_key,
                    message,
                    json.dumps(detail, ensure_ascii=False, default=str) if detail else None,
                    utc_now(),
                ),
            )

    def upsert_entity(
        self,
        run_id: str,
        entity_type: str,
        zoho_key: str,
        *,
        display_label: str | None = None,
        payload: Any = None,
        ghl_id: str | None = None,
        status: str = "pending",
        error: str | None = None,
    ) -> None:
        now = utc_now()
        payload_json = json.dumps(payload, ensure_ascii=False, default=str) if payload else None
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO entities (
                    run_id, entity_type, zoho_key, display_label, zoho_payload,
                    ghl_id, status, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, zoho_key) DO UPDATE SET
                    display_label = COALESCE(excluded.display_label, display_label),
                    zoho_payload = COALESCE(excluded.zoho_payload, zoho_payload),
                    ghl_id = COALESCE(excluded.ghl_id, ghl_id),
                    status = excluded.status,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    entity_type,
                    zoho_key,
                    display_label,
                    payload_json,
                    ghl_id,
                    status,
                    error,
                    now,
                    now,
                ),
            )

    def get_entity(self, run_id: str, zoho_key: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM entities WHERE run_id = ? AND zoho_key = ?",
                (run_id, zoho_key),
            ).fetchone()
        return dict(row) if row else None

    def get_ghl_id(self, run_id: str, zoho_key: str) -> str | None:
        ent = self.get_entity(run_id, zoho_key)
        if ent and ent["status"] == "success" and ent["ghl_id"]:
            return str(ent["ghl_id"])
        return None

    def mark_in_progress(self, run_id: str, zoho_key: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE entities SET status = 'in_progress', updated_at = ?, retry_count = retry_count + 1
                WHERE run_id = ? AND zoho_key = ?
                """,
                (utc_now(), run_id, zoho_key),
            )

    def mark_success(self, run_id: str, zoho_key: str, ghl_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE entities SET status = 'success', ghl_id = ?, error = NULL, updated_at = ?
                WHERE run_id = ? AND zoho_key = ?
                """,
                (ghl_id, utc_now(), run_id, zoho_key),
            )

    def mark_failed(self, run_id: str, zoho_key: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE entities SET status = 'failed', error = ?, updated_at = ?
                WHERE run_id = ? AND zoho_key = ?
                """,
                (error[:2000], utc_now(), run_id, zoho_key),
            )

    def mark_skipped(self, run_id: str, zoho_key: str, reason: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE entities SET status = 'skipped', error = ?, updated_at = ?
                WHERE run_id = ? AND zoho_key = ?
                """,
                (reason[:2000], utc_now(), run_id, zoho_key),
            )

    def list_entities(
        self,
        run_id: str,
        entity_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM entities WHERE run_id = ?"
        params: list[Any] = [run_id]
        if entity_type:
            q += " AND entity_type = ?"
            params.append(entity_type)
        if status:
            q += " AND status = ?"
            params.append(status)
        q += " ORDER BY id"
        with self._conn() as conn:
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def status_counts(self, run_id: str) -> dict[str, dict[str, int]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT entity_type, status, COUNT(*) as cnt
                FROM entities WHERE run_id = ?
                GROUP BY entity_type, status
                """,
                (run_id,),
            ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            et = r["entity_type"]
            out.setdefault(et, {})[r["status"]] = r["cnt"]
        return out

    def file_status_counts(self, run_id: str) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM files WHERE run_id = ? GROUP BY status",
                (run_id,),
            ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    def upsert_file(
        self,
        run_id: str,
        sha256: str,
        original_path: str,
        *,
        current_path: str | None = None,
        file_name: str | None = None,
        status: str = "pending",
        ghl_contact_id: str | None = None,
        ghl_document_id: str | None = None,
        match_method: str | None = None,
        match_confidence: float | None = None,
        error: str | None = None,
        ocr_engine: str | None = None,
    ) -> None:
        now = utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO files (
                    run_id, sha256, original_path, current_path, file_name,
                    match_method, match_confidence, ghl_contact_id, ghl_document_id,
                    status, error, ocr_engine, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, sha256) DO UPDATE SET
                    current_path = COALESCE(excluded.current_path, current_path),
                    match_method = COALESCE(excluded.match_method, match_method),
                    match_confidence = COALESCE(excluded.match_confidence, match_confidence),
                    ghl_contact_id = COALESCE(excluded.ghl_contact_id, ghl_contact_id),
                    ghl_document_id = COALESCE(excluded.ghl_document_id, ghl_document_id),
                    status = excluded.status,
                    error = excluded.error,
                    ocr_engine = COALESCE(excluded.ocr_engine, ocr_engine),
                    updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    sha256,
                    original_path,
                    current_path or original_path,
                    file_name or Path(original_path).name,
                    match_method,
                    match_confidence,
                    ghl_contact_id,
                    ghl_document_id,
                    status,
                    error,
                    ocr_engine,
                    now,
                    now,
                ),
            )

    def get_file_by_sha(self, run_id: str, sha256: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE run_id = ? AND sha256 = ?",
                (run_id, sha256),
            ).fetchone()
        return dict(row) if row else None

    def recent_events(self, run_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_failures(self, run_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            ent = conn.execute(
                """
                SELECT entity_type, zoho_key, display_label, error, updated_at
                FROM entities WHERE run_id = ? AND status = 'failed'
                ORDER BY updated_at DESC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
            files = conn.execute(
                """
                SELECT 'file' as entity_type, sha256 as zoho_key, file_name as display_label,
                       error, updated_at
                FROM files WHERE run_id = ? AND status = 'failed'
                ORDER BY updated_at DESC LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        combined = [dict(r) for r in ent] + [dict(r) for r in files]
        combined.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        return combined[:limit]

    def contact_lookup_index(self, run_id: str) -> dict[str, Any]:
        """Build indexes for attachment matching."""
        contacts = self.list_entities(run_id, entity_type="contact", status="success")
        by_zoho_contact: dict[str, str] = {}
        by_zoho_customer: dict[str, str] = {}
        by_name_tokens: dict[frozenset[str], list[dict[str, str]]] = {}
        for c in contacts:
            ghl_id = c["ghl_id"]
            if not ghl_id:
                continue
            try:
                payload = json.loads(c["zoho_payload"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            zcid = payload.get("zoho_contact_id")
            zcust = payload.get("zoho_customer_id")
            if zcid:
                by_zoho_contact[str(zcid)] = ghl_id
            if zcust:
                by_zoho_customer[str(zcust)] = ghl_id
            label = c.get("display_label") or ""
            from migration.utils import normalize_name_tokens

            tokens = frozenset(normalize_name_tokens(label))
            if len(tokens) >= 2:
                by_name_tokens.setdefault(tokens, []).append(
                    {"ghl_id": ghl_id, "label": label, "zoho_key": c["zoho_key"]}
                )
        return {
            "by_zoho_contact": by_zoho_contact,
            "by_zoho_customer": by_zoho_customer,
            "by_name_tokens": by_name_tokens,
            "contacts": contacts,
        }

    def export_summary(self, run_id: str) -> dict[str, Any]:
        with self._conn() as conn:
            run = conn.execute(
                "SELECT * FROM migration_run WHERE run_id = ?", (run_id,)
            ).fetchone()
        return {
            "run_id": run_id,
            "excel_path": dict(run)["excel_path"] if run else None,
            "started_at": dict(run)["started_at"] if run else None,
            "entities": self.status_counts(run_id),
            "files": self.file_status_counts(run_id),
        }
