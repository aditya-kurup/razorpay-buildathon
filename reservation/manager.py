"""Atomic campaign reservations and deterministic offer expiry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, Mapping
from uuid import uuid4

from .store import ACTIVE, EXPIRED, REDEEMED, Offer, OfferStore, Redemption


DEFAULT_TTL_SECONDS = 600


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class Campaign:
    campaign_id: str
    budget_paise: int
    held_paise: int = 0
    consumed_paise: int = 0

    @property
    def remaining_paise(self) -> int:
        return self.budget_paise - self.held_paise - self.consumed_paise

    @property
    def ledger_total_paise(self) -> int:
        return self.consumed_paise


@dataclass
class Reservation:
    reservation_id: str
    offer_id: str
    campaign_id: str
    amount_paise: int
    expires_at: datetime
    status: str = ACTIVE


@dataclass(frozen=True)
class Approval:
    approved: bool
    offer: Offer | None = None
    reservation: Reservation | None = None
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.approved


@dataclass(frozen=True)
class RedemptionResult:
    redeemed: bool
    reason: str
    offer: Offer | None = None

    def __bool__(self) -> bool:
        return self.redeemed


class ReservationManager:
    """Coordinate campaigns, reservations, offers, and redemption.

    A single manager-instance ``RLock`` protects the complete balance
    check-and-hold operation. Expiry is resolved synchronously at the
    beginning of every read or mutation; there is deliberately no sweeper or
    timer thread. Callers that need cross-process coordination must place this
    transaction behind a database or distributed lock.
    """

    def __init__(
        self,
        store: OfferStore | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
        audit_writer: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        self.store = store or OfferStore()
        self.ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.audit_writer = audit_writer
        self._lock = RLock()
        self._campaigns: dict[str, Campaign] = {}
        self._reservations: dict[str, Reservation] = {}

    def _emit_event(self, event_type: str, actor: str, subject_id: str, payload: Mapping[str, Any]) -> None:
        if self.audit_writer is not None:
            self.audit_writer({
                "event_type": event_type,
                "actor": actor,
                "subject_id": subject_id,
                "payload": dict(payload),
            })

    def register_campaign(self, campaign_id: str, budget_paise: int) -> Campaign:
        if not campaign_id:
            raise ValueError("campaign_id is required")
        if isinstance(budget_paise, bool) or not isinstance(budget_paise, int):
            raise TypeError("budget_paise must be integer paise")
        if budget_paise < 0:
            raise ValueError("budget_paise cannot be negative")
        with self._lock:
            if campaign_id in self._campaigns:
                raise ValueError(f"campaign already exists: {campaign_id!r}")
            campaign = Campaign(campaign_id, budget_paise)
            self._campaigns[campaign_id] = campaign
            return campaign

    create_campaign = register_campaign

    def _now(self, now: datetime | None) -> datetime:
        return _utc(now if now is not None else self._clock())

    def _campaign(self, campaign_id: str) -> Campaign:
        try:
            return self._campaigns[campaign_id]
        except KeyError as exc:
            raise KeyError(f"unknown campaign: {campaign_id!r}") from exc

    def _resolve_expired_locked(self, now: datetime) -> None:
        """Return all expired holds before exposing or spending balance."""

        for reservation in self._reservations.values():
            if reservation.status != ACTIVE or reservation.expires_at > now:
                continue
            campaign = self._campaign(reservation.campaign_id)
            reservation.status = EXPIRED
            campaign.held_paise -= reservation.amount_paise
            self.store.mark_expired(reservation.offer_id, now)
            self._emit_event(
                "RESERVATION_RELEASED",
                "reservation_manager",
                reservation.campaign_id,
                {
                    "offer_id": reservation.offer_id,
                    "reason": "reservation_ttl_expired",
                },
            )

    def resolve_expiry(self, now: datetime | None = None) -> None:
        with self._lock:
            self._resolve_expired_locked(self._now(now))

    def remaining_budget(self, campaign_id: str, now: datetime | None = None) -> int:
        with self._lock:
            current = self._now(now)
            self._resolve_expired_locked(current)
            return self._campaign(campaign_id).remaining_paise

    def campaign_snapshot(self, campaign_id: str, now: datetime | None = None) -> dict[str, int | str]:
        with self._lock:
            current = self._now(now)
            self._resolve_expired_locked(current)
            campaign = self._campaign(campaign_id)
            return {
                "campaign_id": campaign.campaign_id,
                "budget_paise": campaign.budget_paise,
                "held_paise": campaign.held_paise,
                "consumed_paise": campaign.consumed_paise,
                "remaining_paise": campaign.remaining_paise,
                "ledger_total_paise": campaign.ledger_total_paise,
            }

    def reserve(
        self,
        campaign_id: str,
        customer_id: str,
        price_paise: int,
        discount_value_paise: int,
        *,
        offer_id: str | None = None,
        now: datetime | None = None,
        ttl_seconds: int | None = None,
    ) -> Approval:
        """Atomically hold budget and create the corresponding active offer."""

        if isinstance(discount_value_paise, bool) or not isinstance(discount_value_paise, int):
            raise TypeError("discount_value_paise must be integer paise")
        if discount_value_paise < 0:
            raise ValueError("discount_value_paise cannot be negative")
        if isinstance(price_paise, bool) or not isinstance(price_paise, int):
            raise TypeError("price_paise must be integer paise")
        if price_paise < 0:
            raise ValueError("price_paise cannot be negative")
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        with self._lock:
            current = self._now(now)
            self._resolve_expired_locked(current)
            campaign = self._campaign(campaign_id)
            if discount_value_paise > campaign.remaining_paise:
                return Approval(False, reason="campaign_budget_exhausted")
            resolved_offer_id = offer_id or f"offer-{uuid4().hex}"
            expires_at = current + timedelta(seconds=ttl)
            offer = Offer(
                offer_id=resolved_offer_id,
                customer_id=customer_id,
                price_paise=price_paise,
                discount_value_paise=discount_value_paise,
                campaign_id=campaign_id,
                expires_at=expires_at,
            )
            reservation = Reservation(
                reservation_id=f"reservation-{uuid4().hex}",
                offer_id=resolved_offer_id,
                campaign_id=campaign_id,
                amount_paise=discount_value_paise,
                expires_at=expires_at,
            )
            self.store.put(offer)
            self._reservations[reservation.reservation_id] = reservation
            campaign.held_paise += discount_value_paise
            self._emit_event(
                "OFFER_STORED",
                "reservation_manager",
                customer_id,
                {
                    "offer_id": resolved_offer_id,
                    "stored_price_paise": price_paise,
                    "tier": None,
                    "add_on": None,
                    "seats": None,
                },
            )
            self._emit_event(
                "RESERVATION_CREATED",
                "reservation_manager",
                campaign_id,
                {
                    "offer_id": resolved_offer_id,
                    "campaign_id": campaign_id,
                    "amount_paise": discount_value_paise,
                    "ttl_expiry": _utc(expires_at).isoformat(),
                },
            )
            return Approval(True, offer, reservation)

    approve_offer = reserve

    def redeem(self, offer_id: str, now: datetime | None = None) -> RedemptionResult:
        """Atomically redeem an offer or perform an idempotent no-op."""

        with self._lock:
            current = self._now(now)
            self._resolve_expired_locked(current)
            offer = self.store.get(offer_id)
            if offer.status == REDEEMED:
                return RedemptionResult(False, "already_redeemed", offer)
            if offer.status == EXPIRED:
                return RedemptionResult(False, "expired", offer)
            redemption: Redemption = self.store.redeem(offer_id, current)
            if not redemption.redeemed:
                return RedemptionResult(False, redemption.reason, redemption.offer)
            reservation = next(
                reservation
                for reservation in self._reservations.values()
                if reservation.offer_id == offer_id
            )
            campaign = self._campaign(reservation.campaign_id)
            if reservation.status != ACTIVE:
                raise RuntimeError("redeemed offer did not have an active reservation")
            reservation.status = REDEEMED
            campaign.held_paise -= reservation.amount_paise
            campaign.consumed_paise += reservation.amount_paise
            return RedemptionResult(True, "redeemed", redemption.offer)

    redeem_offer = redeem

    def get_offer(self, offer_id: str, now: datetime | None = None) -> Offer:
        with self._lock:
            current = self._now(now)
            self._resolve_expired_locked(current)
            return self.store.get(offer_id)

    def reservations(self) -> tuple[Reservation, ...]:
        with self._lock:
            return tuple(self._reservations.values())


__all__ = [
    "Approval",
    "Campaign",
    "DEFAULT_TTL_SECONDS",
    "RedemptionResult",
    "Reservation",
    "ReservationManager",
]
