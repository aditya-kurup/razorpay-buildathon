"""Append-only hash-chained audit events for the merchant service."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .db import DB_PATH, connect


GENESIS = "GENESIS"
SEPARATOR = "\x1f"
EVENT_TYPES = frozenset({
    "PROPOSAL_GENERATED",
    "FIELD_DISCARDED",
    "POLICY_VERDICT",
    "OFFER_STORED",
    "RESERVATION_CREATED",
    "RESERVATION_RELEASED",
    "COHORT_FORMED",
    "STRATEGY_GENERATED",
    "OFFER_ACCEPTED",
    "ORDER_CREATED",
    "PAYMENT_CALLBACK_RECEIVED",
    "PAYMENT_VERIFIED",
    "PAYMENT_REJECTED",
    "OFFER_PAID",
    "OFFER_FAILED",
})
ACTORS = frozenset({
    "system",
    "policy_engine",
    "proposer",
    "strategist",
    "reservation_manager",
    "payment_leg",
    "customer",
})


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def payload_json(payload: Mapping[str, Any] | None) -> str:
    return json.dumps(
        payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )


def compute_row_hash(
    seq: int,
    timestamp: str,
    event_type: str,
    actor: str,
    subject_id: str,
    payload: str,
    prev_hash: str,
) -> str:
    material = SEPARATOR.join(
        (str(seq), timestamp, event_type, actor, subject_id, payload, prev_hash)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def append_audit_event_locked(
    conn: sqlite3.Connection,
    *,
    timestamp: datetime,
    event_type: str,
    actor: str,
    subject_id: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append while the caller owns a BEGIN IMMEDIATE transaction."""

    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported audit event type: {event_type!r}")
    if actor not in ACTORS:
        raise ValueError(f"unsupported audit actor: {actor!r}")
    if not isinstance(subject_id, str) or not subject_id:
        raise ValueError("audit subject_id is required")

    latest = conn.execute(
        "SELECT seq, row_hash FROM audit_events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    seq = (int(latest[0]) + 1) if latest is not None else 1
    prev_hash = latest[1] if latest is not None else GENESIS
    timestamp_text = utc_iso(timestamp)
    payload_text = payload_json(payload)
    row_hash = compute_row_hash(
        seq, timestamp_text, event_type, actor, subject_id, payload_text, prev_hash
    )
    conn.execute(
        """
        INSERT INTO audit_events
            (seq, timestamp, event_type, actor, subject_id, payload_json, prev_hash, row_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (seq, timestamp_text, event_type, actor, subject_id, payload_text, prev_hash, row_hash),
    )
    return {
        "seq": seq,
        "timestamp": timestamp_text,
        "event_type": event_type,
        "actor": actor,
        "subject_id": subject_id,
        "payload_json": payload_text,
        "prev_hash": prev_hash,
        "row_hash": row_hash,
    }


class AuditEventLogger:
    """Synchronous writer; every append reads the chain tail from SQLite."""

    def __init__(self, db_path: str | Path = DB_PATH) -> None:
        self.db_path = db_path

    def append(
        self,
        *,
        timestamp: datetime,
        event_type: str,
        actor: str,
        subject_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            entry = append_audit_event_locked(
                conn,
                timestamp=timestamp,
                event_type=event_type,
                actor=actor,
                subject_id=subject_id,
                payload=payload,
            )
            conn.commit()
            return entry
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def verify_audit_chain(db_path: str | Path = DB_PATH) -> str:
    conn = connect(db_path)
    try:
        previous = GENESIS
        expected_seq = 1
        rows = conn.execute(
            """
            SELECT seq, timestamp, event_type, actor, subject_id,
                   payload_json, prev_hash, row_hash
            FROM audit_events ORDER BY seq
            """
        )
        for row in rows:
            if row["seq"] != expected_seq or row["prev_hash"] != previous:
                return f"chain broken at seq={row['seq']}"
            recomputed = compute_row_hash(
                row["seq"], row["timestamp"], row["event_type"], row["actor"],
                row["subject_id"], row["payload_json"], row["prev_hash"],
            )
            if recomputed != row["row_hash"]:
                return f"chain broken at seq={row['seq']}"
            previous = row["row_hash"]
            expected_seq += 1
        return "chain intact"
    finally:
        conn.close()


__all__ = [
    "ACTORS",
    "AuditEventLogger",
    "EVENT_TYPES",
    "GENESIS",
    "SEPARATOR",
    "append_audit_event_locked",
    "compute_row_hash",
    "payload_json",
    "utc_iso",
    "verify_audit_chain",
]
