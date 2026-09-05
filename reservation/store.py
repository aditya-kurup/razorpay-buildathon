"""SQLite-backed offer records and their one-way lifecycle."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

RESERVED = "RESERVED"
ACCEPTED = "ACCEPTED"
ORDER_CREATED = "ORDER_CREATED"
PAID = "PAID"
FAILED = "FAILED"
EXPIRED = "EXPIRED"

# Compatibility names used by the step-4 reservation manager.
ACTIVE = RESERVED
REDEEMED = PAID

TRANSITIONS = {
    RESERVED: {ACCEPTED, FAILED, EXPIRED},
    ACCEPTED: {ORDER_CREATED, FAILED},
    ORDER_CREATED: {PAID, FAILED},
    PAID: set(),
    FAILED: set(),
    EXPIRED: set(),
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat()


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


@dataclass(frozen=True)
class Offer:
    offer_id: str
    customer_id: str
    price_paise: int
    discount_value_paise: int
    campaign_id: str | None
    expires_at: datetime
    status: str = RESERVED
    currency: str = "INR"
    razorpay_order_id: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.offer_id or not self.customer_id:
            raise ValueError("offer and customer ids are required")
        for name in ("price_paise", "discount_value_paise"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be integer paise")
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.status not in TRANSITIONS:
            raise ValueError(f"unsupported offer status: {self.status!r}")
        if not self.currency:
            raise ValueError("currency is required")
        object.__setattr__(self, "expires_at", _utc(self.expires_at))


@dataclass(frozen=True)
class OfferTransition:
    offer_id: str
    from_status: str | None
    to_status: str
    occurred_at: datetime
    reason: str | None = None


@dataclass(frozen=True)
class Redemption:
    redeemed: bool
    reason: str
    offer: Offer

    def __bool__(self) -> bool:
        return self.redeemed


class OfferStore:
    """Persist offers and lifecycle transitions in SQLite.

    Each mutation uses ``BEGIN IMMEDIATE`` and writes the offer plus its
    transition history in one transaction. ``:memory:`` keeps existing
    reservation tests isolated; filesystem paths provide durable storage.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._lock = RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS offers (
                offer_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                price_paise INTEGER NOT NULL CHECK(price_paise >= 0),
                discount_value_paise INTEGER NOT NULL CHECK(discount_value_paise >= 0),
                campaign_id TEXT,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL,
                currency TEXT NOT NULL,
                razorpay_order_id TEXT,
                failure_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS offer_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                offer_id TEXT NOT NULL REFERENCES offers(offer_id),
                from_status TEXT,
                to_status TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                reason TEXT
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_offer(row: sqlite3.Row) -> Offer:
        return Offer(
            offer_id=row["offer_id"], customer_id=row["customer_id"],
            price_paise=row["price_paise"],
            discount_value_paise=row["discount_value_paise"],
            campaign_id=row["campaign_id"],
            expires_at=_datetime(row["expires_at"]), status=row["status"],
            currency=row["currency"], razorpay_order_id=row["razorpay_order_id"],
            failure_reason=row["failure_reason"],
        )

    def _get_locked(self, offer_id: str) -> Offer:
        row = self._conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown offer: {offer_id!r}")
        return self._row_to_offer(row)

    def put(self, offer: Offer) -> Offer:
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO offers
                    (offer_id, customer_id, price_paise, discount_value_paise,
                     campaign_id, expires_at, status, currency, razorpay_order_id,
                     failure_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (offer.offer_id, offer.customer_id, offer.price_paise,
                     offer.discount_value_paise, offer.campaign_id,
                     _timestamp(offer.expires_at), offer.status, offer.currency,
                     offer.razorpay_order_id, offer.failure_reason),
                )
                self._conn.execute(
                    """
                    INSERT INTO offer_transitions
                    (offer_id, from_status, to_status, occurred_at, reason)
                    VALUES (?, NULL, ?, ?, ?)
                    """,
                    (offer.offer_id, offer.status,
                     _timestamp(datetime.now(timezone.utc)), "created"),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ValueError(f"offer already exists: {offer.offer_id!r}") from exc
            return offer

    create = put

    def get(self, offer_id: str) -> Offer:
        with self._lock:
            return self._get_locked(offer_id)

    def all(self) -> tuple[Offer, ...]:
        with self._lock:
            return tuple(
                self._row_to_offer(row)
                for row in self._conn.execute("SELECT * FROM offers ORDER BY offer_id")
            )

    def transitions(self, offer_id: str | None = None) -> tuple[OfferTransition, ...]:
        with self._lock:
            query = "SELECT * FROM offer_transitions"
            params: tuple[Any, ...] = ()
            if offer_id is not None:
                query += " WHERE offer_id = ?"
                params = (offer_id,)
            rows = self._conn.execute(query + " ORDER BY id", params).fetchall()
            return tuple(
                OfferTransition(row["offer_id"], row["from_status"], row["to_status"],
                                _datetime(row["occurred_at"]), row["reason"])
                for row in rows
            )

    def transition(
        self, offer_id: str, to_status: str, now: datetime, *,
        reason: str | None = None, razorpay_order_id: str | None = None,
    ) -> Offer:
        if to_status not in TRANSITIONS:
            raise ValueError(f"unsupported offer status: {to_status!r}")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._get_locked(offer_id)
                if to_status not in TRANSITIONS[current.status]:
                    raise ValueError(
                        f"invalid offer transition {current.status} -> {to_status}"
                    )
                cursor = self._conn.execute(
                    """
                    UPDATE offers
                    SET status = ?, razorpay_order_id = COALESCE(?, razorpay_order_id),
                        failure_reason = CASE WHEN ? = 'FAILED' THEN ? ELSE failure_reason END
                    WHERE offer_id = ? AND status = ?
                    """,
                    (to_status, razorpay_order_id, to_status, reason,
                     offer_id, current.status),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("offer transition lost its compare-and-set")
                self._conn.execute(
                    """
                    INSERT INTO offer_transitions
                    (offer_id, from_status, to_status, occurred_at, reason)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (offer_id, current.status, to_status, _timestamp(now), reason),
                )
                self._conn.commit()
                return self._get_locked(offer_id)
            except Exception:
                self._conn.rollback()
                raise

    def mark_expired(self, offer_id: str, now: datetime) -> Offer:
        with self._lock:
            offer = self._get_locked(offer_id)
            if offer.status == RESERVED and offer.expires_at <= _utc(now):
                return self.transition(offer_id, EXPIRED, now, reason="reservation_ttl_expired")
            return offer

    def redeem(self, offer_id: str, now: datetime) -> Redemption:
        """Legacy reservation-layer redemption retained for step-4 callers."""

        with self._lock:
            offer = self._get_locked(offer_id)
            if offer.status == PAID:
                return Redemption(False, "already_redeemed", offer)
            if offer.status == EXPIRED or offer.expires_at <= _utc(now):
                if offer.status == RESERVED:
                    offer = self.transition(offer_id, EXPIRED, now, reason="reservation_ttl_expired")
                return Redemption(False, "expired", offer)
            if offer.status != RESERVED:
                return Redemption(False, "already_redeemed", offer)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._get_locked(offer_id)
                current = self._transition_locked_legacy(current, ACCEPTED, now, "legacy_redemption")
                current = self._transition_locked_legacy(current, ORDER_CREATED, now, "legacy_redemption")
                offer = self._transition_locked_legacy(current, PAID, now, "legacy_redemption")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return Redemption(True, "redeemed", offer)

    def _transition_locked_legacy(self, offer: Offer, to_status: str,
                                   now: datetime, reason: str) -> Offer:
        if to_status not in TRANSITIONS[offer.status]:
            raise ValueError(f"invalid offer transition {offer.status} -> {to_status}")
        cursor = self._conn.execute(
            "UPDATE offers SET status = ? WHERE offer_id = ? AND status = ?",
            (to_status, offer.offer_id, offer.status),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("offer transition compare-and-set failed")
        self._conn.execute(
            "INSERT INTO offer_transitions (offer_id, from_status, to_status, occurred_at, reason) VALUES (?, ?, ?, ?, ?)",
            (offer.offer_id, offer.status, to_status, _timestamp(now), reason),
        )
        return self._get_locked(offer.offer_id)


__all__ = [
    "ACCEPTED", "ACTIVE", "EXPIRED", "FAILED", "ORDER_CREATED", "PAID",
    "REDEEMED", "RESERVED", "Offer", "OfferStore", "OfferTransition",
    "Redemption", "TRANSITIONS",
]
