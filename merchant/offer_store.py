"""SQLite offer lifecycle used by the payment endpoints."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .audit import append_audit_locked, AuditLogger
from .audit_events import append_audit_event_locked, AuditEventLogger
from .db import DB_PATH, connect


RESERVED = "RESERVED"
ACCEPTED = "ACCEPTED"
ORDER_CREATED = "ORDER_CREATED"
PAID = "PAID"
FAILED = "FAILED"
EXPIRED = "EXPIRED"

ALLOWED_TRANSITIONS = {
    RESERVED: {ACCEPTED, FAILED, EXPIRED},
    ACCEPTED: {ORDER_CREATED, FAILED},
    ORDER_CREATED: {PAID, FAILED},
    PAID: set(), FAILED: set(), EXPIRED: set(),
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


@dataclass(frozen=True)
class MerchantOffer:
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


class MerchantOfferStore:
    """Atomic offer state and callback idempotency over the merchant DB."""

    def __init__(self, db_path=DB_PATH) -> None:
        self.db_path = db_path
        self._audit_logger = AuditLogger(db_path)
        self._audit_event_logger = AuditEventLogger(db_path)

    def _conn(self) -> sqlite3.Connection:
        # A fresh connection per operation is safe for concurrent web requests.
        return connect(self.db_path)

    @staticmethod
    def _offer(row: sqlite3.Row) -> MerchantOffer:
        return MerchantOffer(
            offer_id=row["offer_id"], customer_id=row["customer_id"],
            price_paise=row["price_paise"], discount_value_paise=row["discount_value_paise"],
            campaign_id=row["campaign_id"], expires_at=_parse(row["expires_at"]),
            status=row["status"], currency=row["currency"],
            razorpay_order_id=row["razorpay_order_id"], failure_reason=row["failure_reason"],
        )

    @staticmethod
    def _row(conn: sqlite3.Connection, offer_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM merchant_offers WHERE offer_id = ?", (offer_id,)).fetchone()
        if row is None:
            raise KeyError(offer_id)
        return row

    def create_offer(self, offer: MerchantOffer, now: datetime | None = None) -> MerchantOffer:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            created_at = now or datetime.now(timezone.utc)
            conn.execute(
                """INSERT INTO merchant_offers
                (offer_id, customer_id, price_paise, discount_value_paise, campaign_id,
                 expires_at, status, currency, razorpay_order_id, failure_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (offer.offer_id, offer.customer_id, offer.price_paise, offer.discount_value_paise,
                 offer.campaign_id, _iso(offer.expires_at), offer.status, offer.currency,
                 offer.razorpay_order_id, offer.failure_reason),
            )
            conn.execute(
                "INSERT INTO offer_transitions (offer_id, from_status, to_status, occurred_at, reason) VALUES (?, NULL, ?, ?, ?)",
                (offer.offer_id, offer.status, _iso(created_at), "created"),
            )
            append_audit_locked(
                conn,
                timestamp=created_at,
                offer_id=offer.offer_id,
                event_type="offer_created",
                details={
                    "customer_id": offer.customer_id,
                    "price_paise": offer.price_paise,
                    "discount_value_paise": offer.discount_value_paise,
                    "campaign_id": offer.campaign_id,
                    "expires_at": _iso(offer.expires_at),
                    "status": offer.status,
                    "currency": offer.currency,
                },
            )
            append_audit_event_locked(
                conn,
                timestamp=created_at,
                event_type="OFFER_STORED",
                actor="system",
                subject_id=offer.customer_id,
                payload={
                    "offer_id": offer.offer_id,
                    "stored_price_paise": offer.price_paise,
                    "tier": None,
                    "add_on": None,
                    "seats": None,
                },
            )
            conn.commit()
            return offer
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError(f"offer already exists: {offer.offer_id!r}") from exc
        finally:
            conn.close()

    def get(self, offer_id: str) -> MerchantOffer:
        conn = self._conn()
        try:
            return self._offer(self._row(conn, offer_id))
        finally:
            conn.close()

    def transitions(self, offer_id: str) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT offer_id, from_status, to_status, occurred_at, reason FROM offer_transitions WHERE offer_id = ? ORDER BY id",
                (offer_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _transition_locked(self, conn: sqlite3.Connection, offer: MerchantOffer, to_status: str,
                           now: datetime, reason: str | None = None,
                           order_id: str | None = None) -> MerchantOffer:
        if to_status not in ALLOWED_TRANSITIONS.get(offer.status, set()):
            raise ValueError(f"invalid offer transition {offer.status} -> {to_status}")
        cursor = conn.execute(
            """UPDATE merchant_offers
               SET status = ?, razorpay_order_id = COALESCE(?, razorpay_order_id),
                   failure_reason = CASE WHEN ? = 'FAILED' THEN ? ELSE failure_reason END
             WHERE offer_id = ? AND status = ?""",
            (to_status, order_id, to_status, reason, offer.offer_id, offer.status),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("offer transition compare-and-set failed")
        conn.execute(
            "INSERT INTO offer_transitions (offer_id, from_status, to_status, occurred_at, reason) VALUES (?, ?, ?, ?, ?)",
            (offer.offer_id, offer.status, to_status, _iso(now), reason),
        )
        return self._offer(self._row(conn, offer.offer_id))

    def accept(self, offer_id: str, now: datetime) -> tuple[MerchantOffer, bool]:
        """Atomically resolve expiry or claim RESERVED for order creation."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            offer = self._offer(self._row(conn, offer_id))
            if offer.status == RESERVED and offer.expires_at <= _utc(now):
                offer = self._transition_locked(conn, offer, EXPIRED, now, "reservation_ttl_expired")
                append_audit_locked(
                    conn,
                    timestamp=now,
                    offer_id=offer.offer_id,
                    event_type="expired",
                    details={"reason": "reservation_ttl_expired"},
                )
                append_audit_event_locked(
                    conn,
                    timestamp=now,
                    event_type="RESERVATION_RELEASED",
                    actor="reservation_manager",
                    subject_id=offer.customer_id,
                    payload={"offer_id": offer.offer_id, "reason": "reservation_ttl_expired"},
                )
                conn.commit()
                return offer, True
            if offer.status != RESERVED:
                conn.commit()
                return offer, False
            offer = self._transition_locked(conn, offer, ACCEPTED, now, "customer_accepted")
            append_audit_locked(
                conn,
                timestamp=now,
                offer_id=offer.offer_id,
                event_type="accepted",
                details={"from_status": RESERVED, "to_status": ACCEPTED},
            )
            append_audit_event_locked(
                conn,
                timestamp=now,
                event_type="OFFER_ACCEPTED",
                actor="customer",
                subject_id=offer.customer_id,
                payload={
                    "offer_id": offer.offer_id,
                    "from_status": RESERVED,
                    "to_status": ACCEPTED,
                },
            )
            conn.commit()
            return offer, False
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def order_created(self, offer_id: str, order_id: str, now: datetime) -> MerchantOffer:
        return self.transition(offer_id, ORDER_CREATED, now, reason="razorpay_order_created", order_id=order_id)

    def failed(self, offer_id: str, now: datetime, reason: str) -> MerchantOffer:
        return self.transition(offer_id, FAILED, now, reason=reason)

    def transition(self, offer_id: str, to_status: str, now: datetime, *,
                   reason: str | None = None, order_id: str | None = None) -> MerchantOffer:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            offer = self._offer(self._row(conn, offer_id))
            result = self._transition_locked(conn, offer, to_status, now, reason, order_id)
            if to_status == ORDER_CREATED:
                append_audit_locked(
                    conn,
                    timestamp=now,
                    offer_id=offer_id,
                    event_type="order_created",
                    details={
                        "order_id": order_id,
                        "amount_paise": result.price_paise,
                        "currency": result.currency,
                    },
                )
                append_audit_event_locked(
                    conn,
                    timestamp=now,
                    event_type="ORDER_CREATED",
                    actor="payment_leg",
                    subject_id=result.customer_id,
                    payload={
                        "offer_id": result.offer_id,
                        "razorpay_order_id": order_id,
                        "amount_paise": result.price_paise,
                        "amount_source": "offer.price_paise",
                    },
                )
            elif to_status == FAILED:
                append_audit_event_locked(
                    conn,
                    timestamp=now,
                    event_type="OFFER_FAILED",
                    actor="payment_leg",
                    subject_id=result.customer_id,
                    payload={"offer_id": result.offer_id, "reason": reason},
                )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_event(self, event_type: str, detail: str, now: datetime,
                     offer_id: str | None = None, payment_id: str | None = None) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO offer_payment_events (offer_id, payment_id, event_type, detail, occurred_at) VALUES (?, ?, ?, ?, ?)",
                (offer_id, payment_id, event_type, detail, _iso(now)),
            )
            conn.commit()
        finally:
            conn.close()

    def append_audit(self, *, offer_id: str, event_type: str, timestamp: datetime,
                     details: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._audit_logger.append(
            timestamp=timestamp,
            offer_id=offer_id,
            event_type=event_type,
            details=details,
        )

    def append_audit_event(self, *, event_type: str, actor: str, subject_id: str,
                           timestamp: datetime, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._audit_event_logger.append(
            timestamp=timestamp,
            event_type=event_type,
            actor=actor,
            subject_id=subject_id,
            payload=payload,
        )

    def release_campaign_reservation(self, offer: MerchantOffer, now: datetime, reason: str) -> bool:
        if not offer.campaign_id:
            return False
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """INSERT OR IGNORE INTO campaign_reservation_releases
                   (offer_id, campaign_id, amount_paise, released_at, reason)
                   VALUES (?, ?, ?, ?, ?)""",
                (offer.offer_id, offer.campaign_id, offer.discount_value_paise, _iso(now), reason),
            )
            if cursor.rowcount == 1:
                append_audit_event_locked(
                    conn,
                    timestamp=now,
                    event_type="RESERVATION_RELEASED",
                    actor="reservation_manager",
                    subject_id=offer.campaign_id,
                    payload={
                        "offer_id": offer.offer_id,
                        "campaign_id": offer.campaign_id,
                        "amount_paise": offer.discount_value_paise,
                        "reason": reason,
                    },
                )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def record_paid(self, offer_id: str, payment_id: str, order_id: str,
                    amount_paise: int, currency: str, now: datetime) -> tuple[MerchantOffer, bool]:
        """Insert the payment key and PAID transition in one transaction."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT offer_id FROM offer_payment_callbacks WHERE payment_id = ?", (payment_id,)
            ).fetchone()
            if existing is not None:
                offer = self._offer(self._row(conn, offer_id))
                append_audit_locked(
                    conn,
                    timestamp=now,
                    offer_id=offer_id,
                    event_type="duplicate_ignored",
                    details={
                        "payment_id": payment_id,
                        "order_id": order_id,
                        "amount_paise": amount_paise,
                        "currency": currency,
                    },
                )
                conn.commit()
                return offer, False
            offer = self._offer(self._row(conn, offer_id))
            if offer.status != ORDER_CREATED:
                raise ValueError(f"offer is {offer.status}, not ORDER_CREATED")
            if offer.razorpay_order_id != order_id:
                raise ValueError("Razorpay order id does not match offer")
            conn.execute(
                "INSERT INTO offer_payment_callbacks (payment_id, offer_id, order_id, amount_paise, currency, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (payment_id, offer_id, order_id, amount_paise, currency, _iso(now)),
            )
            offer = self._transition_locked(conn, offer, PAID, now, "payment_verified")
            append_audit_event_locked(
                conn,
                timestamp=now,
                event_type="PAYMENT_VERIFIED",
                actor="payment_leg",
                subject_id=offer.customer_id,
                payload={
                    "offer_id": offer.offer_id,
                    "payment_id": payment_id,
                    "order_id": order_id,
                    "amount_paise": amount_paise,
                    "currency": currency,
                },
            )
            append_audit_event_locked(
                conn,
                timestamp=now,
                event_type="OFFER_PAID",
                actor="payment_leg",
                subject_id=offer.customer_id,
                payload={
                    "offer_id": offer.offer_id,
                    "payment_id": payment_id,
                    "order_id": order_id,
                },
            )
            append_audit_locked(
                conn,
                timestamp=now,
                offer_id=offer_id,
                event_type="paid",
                details={
                    "payment_id": payment_id,
                    "order_id": order_id,
                    "amount_paise": amount_paise,
                    "currency": currency,
                },
            )
            conn.commit()
            return offer, True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


__all__ = ["ACCEPTED", "EXPIRED", "FAILED", "MerchantOffer", "MerchantOfferStore", "ORDER_CREATED", "PAID", "RESERVED"]
