"""Deterministic narrowing of the options shown to an agent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from data import catalog
from policy.rules import DEFAULT_POLICY_CONFIG, PolicyConfig


ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class DiscountBand:
    """Inclusive lower and upper discount limits as Decimal fractions."""

    minimum: Decimal = ZERO
    maximum: Decimal = ZERO

    def __post_init__(self) -> None:
        minimum = Decimal(str(self.minimum))
        maximum = Decimal(str(self.maximum))
        if minimum < ZERO or maximum < minimum:
            raise ValueError("discount band must be ordered and non-negative")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)


@dataclass(frozen=True)
class AllowedOption:
    """An option the model may select; add-ons are represented explicitly."""

    option_id: str
    offer_type: str
    target_tier: str | None = None
    target_addon: str | None = None
    minimum_seats: int | None = None

    def __post_init__(self) -> None:
        if self.offer_type == "addon":
            if self.target_addon is None or self.target_tier is not None:
                raise ValueError("add-on options require only target_addon")
        elif self.offer_type in {"upgrade", "seat_expansion"}:
            if self.target_tier is None or self.target_addon is not None:
                raise ValueError("tier options require only target_tier")
        else:
            raise ValueError(f"unsupported option type: {self.offer_type!r}")


@dataclass(frozen=True)
class EligibilityResult:
    customer_id: str
    eligible: bool
    allowed_options: tuple[AllowedOption, ...]
    discount_band: DiscountBand
    reason: str | None = None

    @property
    def options(self) -> tuple[AllowedOption, ...]:
        return self.allowed_options


def _decimal(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _has_active_offer(
    account: Any,
    now: datetime,
    ttl_seconds: int,
    ttl_comparison: str = "exclusive",
) -> bool:
    for offer in _attr(account, "offer_history", ()) or ():
        if _attr(offer, "redeemed", False) or _attr(offer, "status") in {"expired", "consumed", "redeemed"}:
            continue
        expires_at = _parse_datetime(_attr(offer, "expires_at"))
        if expires_at is None:
            created_at = _parse_datetime(_attr(offer, "created_at"))
            if created_at is None:
                continue
            expires_at = created_at + timedelta(seconds=ttl_seconds)
        is_active = (
            expires_at > now
            if ttl_comparison == "exclusive"
            else expires_at >= now
        )
        if is_active:
            return True
    return False


def _standing_discount(account: Any) -> Decimal:
    value = _attr(account, "standing_discount")
    if value is None:
        return catalog.ANNUAL_DISCOUNT if _attr(account, "billing_cycle", "monthly") == "annual" else ZERO
    return _decimal(value)


def filter_account(
    account: Any,
    *,
    config: PolicyConfig = DEFAULT_POLICY_CONFIG,
    now: datetime | None = None,
) -> EligibilityResult:
    """Compute one account's options and inclusive discount band.

    Add-on options are built from ``catalog.available_addons`` for the
    account's tier and therefore cannot be invented by the default proposer.
    """

    customer_id = str(_attr(account, "customer_id", ""))
    tier = _attr(account, "tier")
    billing_cycle = str(_attr(account, "billing_cycle", "monthly"))
    existing = _decimal(_attr(account, "existing_discount"))
    standing = _standing_discount(account)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    if tier not in catalog.TIERS:
        return EligibilityResult(customer_id, False, (), DiscountBand(), "unknown account tier")

    per_offer = config.per_offer_discount_ceilings.get(tier, ZERO)
    combined = config.combined_authority_ceilings.get(tier, ZERO) - standing - existing
    margin_limit = catalog.margin_discount_limit(tier, billing_cycle, existing)
    maximum = max(ZERO, min(per_offer, combined, margin_limit))
    band = DiscountBand(ZERO, maximum)

    if _has_active_offer(
        account,
        current,
        config.offer_ttl_seconds,
        config.offer_ttl_comparison,
    ):
        return EligibilityResult(customer_id, False, (), band, "customer already holds an active offer")

    options: list[AllowedOption] = []
    order = tuple(catalog.TIER_ORDER)
    current_index = order.index(tier)
    for target_tier in order[current_index + 1 :]:
        options.append(
            AllowedOption(
                option_id=f"upgrade:{target_tier}",
                offer_type="upgrade",
                target_tier=target_tier,
                minimum_seats=int(_attr(account, "seats_contracted", 0)) + 1,
            )
        )
    options.append(
        AllowedOption(
            option_id=f"seat_expansion:{tier}",
            offer_type="seat_expansion",
            target_tier=tier,
            minimum_seats=int(_attr(account, "seats_contracted", 0)) + 1,
        )
    )
    active_addons = set(_attr(account, "active_addons", ()) or ())
    for addon_name in catalog.available_addons(tier):
        if addon_name not in active_addons:
            options.append(
                AllowedOption(
                    option_id=f"addon:{addon_name}",
                    offer_type="addon",
                    target_addon=addon_name,
                )
            )

    reason = None if options and maximum > ZERO else "no discount headroom"
    return EligibilityResult(customer_id, bool(options), tuple(options), band, reason)


eligibility_filter = filter_account


__all__ = [
    "AllowedOption",
    "DiscountBand",
    "EligibilityResult",
    "eligibility_filter",
    "filter_account",
]
