"""Immutable, hash-chained audit entries for merchant offer lifecycle events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .db import DB_PATH, connect


GENESIS_HASH = "0" * 64


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_timestamp(value: datetime) -> str:
    return _utc(value).isoformat()


def canonical_details(details: Mapping[str, Any] | None) -> str:
    """Use one stable JSON representation for hashing and later verification."""

    return json.dumps(
        details or {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def compute_entry_hash(
    prev_hash: str,
    timestamp: str,
    offer_id: str,
    event_type: str,
    details: str,
) -> str:
    material = f"{prev_hash}{timestamp}{offer_id}{event_type}{details}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def append_audit_locked(
    conn: sqlite3.Connection,
    *,
    timestamp: datetime,
    offer_id: str,
    event_type: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one entry while the caller owns a write transaction."""

    timestamp_text = iso_timestamp(timestamp)
    details_text = canonical_details(details)
    previous = conn.execute(
        "SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_hash = previous[0] if previous is not None else GENESIS_HASH
    entry_hash = compute_entry_hash(
        prev_hash, timestamp_text, offer_id, event_type, details_text
    )
    cursor = conn.execute(
        """
        INSERT INTO audit_log
            (timestamp, offer_id, event_type, details, prev_hash, entry_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (timestamp_text, offer_id, event_type, details_text, prev_hash, entry_hash),
    )
    return {
        "id": cursor.lastrowid,
        "timestamp": timestamp_text,
        "offer_id": offer_id,
        "event_type": event_type,
        "details": details_text,
        "prev_hash": prev_hash,
        "entry_hash": entry_hash,
    }


class AuditLogger:
    """Append-only audit writer using SQLite's write lock for chain ordering."""

    def __init__(self, db_path: str | Path = DB_PATH) -> None:
        self.db_path = db_path

    def append(
        self,
        *,
        timestamp: datetime,
        offer_id: str,
        event_type: str,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            entry = append_audit_locked(
                conn,
                timestamp=timestamp,
                offer_id=offer_id,
                event_type=event_type,
                details=details,
            )
            conn.commit()
            return entry
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


__all__ = [
    "AuditLogger",
    "GENESIS_HASH",
    "append_audit_locked",
    "canonical_details",
    "compute_entry_hash",
    "iso_timestamp",
]
