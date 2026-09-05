"""The single source of truth for plans, add-ons, and pricing arithmetic.

Rates and rupee amounts use ``Decimal``. Policy-facing money helpers use
integer paise, so no monetary calculation crosses a float boundary.

``existing_discount`` means a merchant promotional discount already attached
to an account. The annual billing discount is tracked independently, so the
policy layer can enforce no-stacking explicitly.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping


MARGIN_FLOOR = Decimal("0.55")
ANNUAL_DISCOUNT = Decimal("0.20")
CURRENCY = "INR"
BILLING_CYCLES = ("monthly", "annual")


TIERS: dict[str, dict[str, object]] = {
    "starter": {
        "list_price": Decimal("250"),
        "serving_cost": Decimal("85"),
        "gross_margin": Decimal("0.66"),
        "max_discount": Decimal("0.10"),
    },
    "pro": {
        "list_price": Decimal("600"),
        "serving_cost": Decimal("180"),
        "gross_margin": Decimal("0.70"),
        "max_discount": Decimal("0.15"),
    },
    "enterprise": {
        "list_price": Decimal("1200"),
        "serving_cost": Decimal("320"),
        "gross_margin": Decimal("0.73"),
        "max_discount": Decimal("0.20"),
    },
}


ADD_ONS: dict[str, dict[str, object]] = {
    "unlimited_message_history": {
        "available_on": ("pro", "enterprise"),
        "price": Decimal("80"),
        "cost": Decimal("20"),
        "pricing": "per_seat",
    },
    "guest_access_pack": {
        "available_on": ("pro", "enterprise"),
        "price": Decimal("4000"),
        "cost": Decimal("900"),
        "pricing": "flat",
    },
    "compliance_export_audit_log": {
        "available_on": ("enterprise",),
        "price": Decimal("15000"),
        "cost": Decimal("3500"),
        "pricing": "flat",
    },
}


TIER_ORDER = ("starter", "pro", "enterprise")


def _validate_tier(tier: str) -> Mapping[str, object]:
    try:
        return TIERS[tier]
    except KeyError as exc:
        raise ValueError(f"Unknown tier: {tier!r}") from exc


def _validate_cycle(billing_cycle: str) -> None:
    if billing_cycle not in BILLING_CYCLES:
        raise ValueError(f"Unknown billing cycle: {billing_cycle!r}")


def cycle_discount(billing_cycle: str) -> Decimal:
    """Return the standing discount supplied by the billing cycle."""

    _validate_cycle(billing_cycle)
    return ANNUAL_DISCOUNT if billing_cycle == "annual" else Decimal("0")


def cycle_price(tier: str, billing_cycle: str = "monthly") -> Decimal:
    """Return the per-seat monthly-equivalent price before a promo discount."""

    plan = _validate_tier(tier)
    _validate_cycle(billing_cycle)
    price = Decimal(str(plan["list_price"])) * (Decimal("1") - cycle_discount(billing_cycle))
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def price_for_discount(
    tier: str,
    discount: Decimal,
    billing_cycle: str = "monthly",
    existing_discount: Decimal = Decimal("0"),
) -> Decimal:
    """Return the price after standing and promotional discounts.

    The standing annual discount is applied first, followed by any existing
    and newly proposed promotional discounts. This preserves the useful
    distinction between the annual cycle authority and merchant promotions.
    """

    plan = _validate_tier(tier)
    _validate_cycle(billing_cycle)
    discount_decimal = Decimal(str(discount))
    existing_decimal = Decimal(str(existing_discount))
    if not Decimal("0") <= discount_decimal <= Decimal("1"):
        raise ValueError("discount must be between 0 and 1")
    if not Decimal("0") <= existing_decimal <= Decimal("1"):
        raise ValueError("existing_discount must be between 0 and 1")
    price = Decimal(str(plan["list_price"]))
    price *= Decimal("1") - cycle_discount(billing_cycle)
    price *= Decimal("1") - existing_decimal
    price *= Decimal("1") - discount_decimal
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def margin_at_price(tier: str, price_paise: int | Decimal) -> Decimal:
    """Return gross margin for a per-seat monthly price supplied in paise.

    The helper deliberately accepts only integer paise or ``Decimal`` paise;
    a money value never crosses this API as a float.
    """

    plan = _validate_tier(tier)
    if isinstance(price_paise, float):
        raise TypeError("price_paise must be integer paise or Decimal paise")
    price = Decimal(price_paise)
    serving_cost = Decimal(str(plan["serving_cost"])) * Decimal("100")
    if price <= 0:
        raise ValueError("price must be greater than zero")
    margin = (price - serving_cost) / price
    return margin.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def gross_margin(tier: str, price_paise: int | Decimal) -> Decimal:
    """Alias for :func:`margin_at_price` used by downstream policy code."""

    return margin_at_price(tier, price_paise)


def price_for_discount_paise(
    tier: str,
    discount: Decimal,
    billing_cycle: str = "monthly",
    existing_discount: Decimal = Decimal("0"),
) -> int:
    """Return a discounted per-seat price as integer paise.

    This paise-native companion is used by policy callers that must not pass
    rupee floats through the pricing boundary.
    """

    plan = _validate_tier(tier)
    _validate_cycle(billing_cycle)
    discount_decimal = Decimal(str(discount))
    existing_decimal = Decimal(str(existing_discount))
    if not 0 <= discount_decimal <= 1:
        raise ValueError("discount must be between 0 and 1")
    if not 0 <= existing_decimal <= 1:
        raise ValueError("existing_discount must be between 0 and 1")
    cycle_decimal = cycle_discount(billing_cycle)
    price = Decimal(str(plan["list_price"]))
    price *= 1 - cycle_decimal
    price *= 1 - existing_decimal
    price *= 1 - discount_decimal
    return int((price * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def margin_discount_limit(
    tier: str,
    billing_cycle: str = "monthly",
    existing_discount: float | Decimal = Decimal("0"),
) -> Decimal:
    """Return the maximum additional discount allowed by the margin floor.

    The limit starts at the price after the billing-cycle discount and any
    existing promotion. This makes annual Starter's remaining margin headroom
    approximately five percentage points, as described in the specification.
    """

    plan = _validate_tier(tier)
    _validate_cycle(billing_cycle)
    existing = Decimal(str(existing_discount))
    if not Decimal("0") <= existing <= Decimal("1"):
        raise ValueError("existing_discount must be between 0 and 1")
    current_price = Decimal(str(plan["list_price"]))
    current_price *= Decimal("1") - Decimal(str(cycle_discount(billing_cycle)))
    current_price *= Decimal("1") - existing
    floor_price = Decimal(str(plan["serving_cost"])) / (
        Decimal("1") - Decimal(str(MARGIN_FLOOR))
    )
    if current_price <= 0:
        return Decimal("0")
    limit = Decimal("1") - (floor_price / current_price)
    return max(Decimal("0"), limit).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def discount_headroom(
    tier: str,
    billing_cycle: str = "monthly",
    existing_discount: float | Decimal = Decimal("0"),
) -> Decimal:
    """Return maximum new promotional discount satisfying both limits."""

    _validate_tier(tier)
    existing = Decimal(str(existing_discount))
    if not Decimal("0") <= existing <= Decimal("1"):
        raise ValueError("existing_discount must be between 0 and 1")
    tier_limit = Decimal(str(TIERS[tier]["max_discount"]))
    headroom = min(
        max(Decimal("0"), tier_limit - existing),
        margin_discount_limit(tier, billing_cycle, existing),
    )
    return max(Decimal("0"), headroom).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def addon_is_available(addon: str, tier: str) -> bool:
    """Return whether an add-on is permitted on a tier."""

    _validate_tier(tier)
    try:
        return tier in ADD_ONS[addon]["available_on"]  # type: ignore[operator]
    except KeyError as exc:
        raise ValueError(f"Unknown add-on: {addon!r}") from exc


def available_addons(tier: str) -> list[str]:
    """Return add-ons permitted on ``tier`` in catalog order."""

    _validate_tier(tier)
    return [addon for addon in ADD_ONS if addon_is_available(addon, tier)]


def round_money(value: Decimal | int) -> Decimal:
    """Round a rupee amount to paise precision without converting to float."""

    if isinstance(value, float):
        raise TypeError("money values must be Decimal or integer rupees")
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
