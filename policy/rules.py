"""Pure policy rules for proposal adjudication.

Proposal schema
---------------
``Proposal`` is the boundary object between an agent and the policy layer::

    customer_id: str
    offer_type: "upgrade" | "addon"
    target_tier: str | None
    target_addon: str | None
    proposed_discount: Decimal  # fraction, e.g. Decimal("0.10") is 10%
    campaign_id: str | None
    reason: str                  # model-stated reason, never trusted as policy
    proposed_seats: int | None   # required when changing seat count

The proposal contains no price. Prices and discount values are derived here
from the catalog and the account. Monetary amounts in this module are integer
paise; percentages and other rates use ``Decimal``.

``campaign_state`` is a mapping supplied by the caller. It may contain a
``policy_config`` value and, for a campaign proposal, either direct campaign
fields or a ``campaigns`` mapping keyed by campaign id::

    {
        "policy_config": DEFAULT_POLICY_CONFIG,
        "campaign_id": "campaign-1",
        "allocated_budget_paise": 5_000_000,
        "reserved_discount_paise": 125_000,
        "now": datetime(...),
    }

The rules do not mutate this state. Reservation and persistence belong to a
later enforcement layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from data.catalog import discount_headroom, margin_at_price, price_for_discount_paise


ZERO = Decimal("0")
ONE = Decimal("1")
PAISE_PER_RUPEE = 100

_CATALOG_RATE_HELPERS: Mapping[str, Callable[..., Any]] = {
    "discount_headroom": discount_headroom,
    "margin_at_price": margin_at_price,
    "price_for_discount_paise": price_for_discount_paise,
}


@dataclass(frozen=True)
class PolicyConfig:
    """All provisional policy thresholds in one immutable configuration."""

    margin_floor: Decimal = Decimal("0.55")
    per_offer_discount_ceilings: Mapping[str, Decimal] = field(
        default_factory=lambda: MappingProxyType(
            {
                "starter": Decimal("0.10"),
                "pro": Decimal("0.15"),
                "enterprise": Decimal("0.20"),
            }
        )
    )
    combined_authority_ceilings: Mapping[str, Decimal] = field(
        default_factory=lambda: MappingProxyType(
            {
                "starter": Decimal("0.25"),
                "pro": Decimal("0.30"),
                "enterprise": Decimal("0.40"),
            }
        )
    )
    margin_floor_comparison: str = "inclusive"
    per_offer_ceiling_comparison: str = "inclusive"
    combined_authority_comparison: str = "inclusive"
    offer_ttl_comparison: str = "exclusive"
    offer_cap_comparison: str = "inclusive"
    campaign_budget_comparison: str = "inclusive"
    offer_ttl_seconds: int = 600
    per_customer_offer_cap: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "margin_floor", Decimal(str(self.margin_floor)))
        object.__setattr__(
            self,
            "per_offer_discount_ceilings",
            MappingProxyType(
                {
                    tier: Decimal(str(ceiling))
                    for tier, ceiling in self.per_offer_discount_ceilings.items()
                }
            ),
        )
        for field_name in (
            "margin_floor_comparison",
            "per_offer_ceiling_comparison",
            "combined_authority_comparison",
            "offer_ttl_comparison",
            "offer_cap_comparison",
            "campaign_budget_comparison",
        ):
            comparison = getattr(self, field_name)
            if comparison not in {"inclusive", "exclusive"}:
                raise ValueError(
                    f"{field_name} must be 'inclusive' or 'exclusive', got {comparison!r}"
                )
        object.__setattr__(
            self,
            "combined_authority_ceilings",
            MappingProxyType(
                {
                    tier: Decimal(str(ceiling))
                    for tier, ceiling in self.combined_authority_ceilings.items()
                }
            ),
        )

    @property
    def tier_discount_ceilings(self) -> Mapping[str, Decimal]:
        """Backward-compatible alias for the per-offer ceiling map."""

        return self.per_offer_discount_ceilings


DEFAULT_POLICY_CONFIG = PolicyConfig()
POLICY_CONFIG = DEFAULT_POLICY_CONFIG


@dataclass(frozen=True)
class Proposal:
    """Typed proposal shape produced by an upstream agent or pipeline."""

    customer_id: str
    offer_type: str
    target_tier: str | None = None
    target_addon: str | None = None
    proposed_discount: Decimal = ZERO
    campaign_id: str | None = None
    reason: str = ""
    proposed_seats: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "proposed_discount", _as_decimal(self.proposed_discount))


@dataclass(frozen=True)
class RuleResult:
    """Result emitted by every rule in the registry."""

    rule_id: str
    passed: bool
    reason: str | None = None

    @property
    def failed(self) -> bool:
        return not self.passed


@dataclass(frozen=True)
class CampaignState:
    """Optional typed form of the campaign-state mapping accepted by rules."""

    campaign_id: str | None = None
    allocated_budget_paise: int | None = None
    reserved_discount_paise: int = 0
    now: datetime | None = None
    policy_config: PolicyConfig = DEFAULT_POLICY_CONFIG


Rule = Callable[[Any, Any, Any, Any], RuleResult]


def _as_decimal(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _as_paise(value: Any) -> int:
    """Convert a rupee amount or paise integer to integer paise."""

    if isinstance(value, int):
        return value
    amount = _as_decimal(value)
    return int((amount * PAISE_PER_RUPEE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _state_value(state: Any, name: str, default: Any = None) -> Any:
    if isinstance(state, Mapping):
        return state.get(name, default)
    return getattr(state, name, default)


def _config(campaign_state: Any) -> PolicyConfig:
    configured = _state_value(campaign_state, "policy_config")
    if isinstance(configured, PolicyConfig):
        return configured
    configured = _state_value(campaign_state, "config")
    if isinstance(configured, PolicyConfig):
        return configured
    return DEFAULT_POLICY_CONFIG


def _tiers(catalog: Any) -> Mapping[str, Mapping[str, Any]]:
    return _attr(catalog, "TIERS", {}) or {}


def _addons(catalog: Any) -> Mapping[str, Mapping[str, Any]]:
    return _attr(catalog, "ADD_ONS", {}) or {}


def _account_tier(account: Any) -> str | None:
    return _attr(account, "tier")


def _billing_cycle(account: Any) -> str:
    return str(_attr(account, "billing_cycle", "monthly"))


def _existing_discount(account: Any) -> Decimal:
    return _as_decimal(_attr(account, "existing_discount", ZERO))


def _target_tier(proposal: Any, account: Any) -> str | None:
    return _attr(proposal, "target_tier") or _account_tier(account)


def _catalog_rate(catalog: Any, function_name: str, *args: Any) -> Decimal:
    """Call a catalog helper and normalize its exact Decimal result."""

    helper = _attr(catalog, function_name)
    if not callable(helper):
        helper = _CATALOG_RATE_HELPERS.get(function_name)
    if not callable(helper):
        return ZERO
    return _as_decimal(helper(*args))


def _current_price_paise(
    proposal: Any,
    account: Any,
    catalog: Any,
    target_tier: str,
) -> int:
    """Derive an offer price in paise from catalog values, never proposal price."""

    helper = _attr(catalog, "price_for_discount_paise")
    if not callable(helper):
        helper = price_for_discount_paise
    return int(
        helper(
            target_tier,
            _as_decimal(_attr(proposal, "proposed_discount")),
            _billing_cycle(account),
            _existing_discount(account),
        )
    )


def _margin_for_price(tier: str, price_paise: int, catalog: Any) -> Decimal:
    """Use catalog's margin helper at the compatibility boundary."""

    margin = _catalog_rate(catalog, "margin_at_price", tier, price_paise)
    if margin == ZERO:
        margin = _catalog_rate(catalog, "gross_margin", tier, price_paise)
    return margin


def _campaign_record(campaign_state: Any, campaign_id: str) -> Any:
    campaigns = _state_value(campaign_state, "campaigns", {}) or {}
    if isinstance(campaigns, Mapping) and campaign_id in campaigns:
        return campaigns[campaign_id]
    state_campaign_id = _state_value(campaign_state, "campaign_id")
    if state_campaign_id in (None, campaign_id):
        return campaign_state
    return None


def _first_value(value: Any, names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        found = _attr(value, name)
        if found is not None:
            return found
    return default


def _discount_value_paise(proposal: Any, account: Any, catalog: Any) -> int | None:
    proposed = _as_decimal(_attr(proposal, "proposed_discount"))
    if proposed < ZERO:
        return None
    offer_type = _attr(proposal, "offer_type")
    if offer_type == "addon":
        addon_name = _attr(proposal, "target_addon")
        addon = _addons(catalog).get(addon_name, {})
        if not addon:
            return None
        quantity = 1
        if addon.get("pricing") == "per_seat":
            quantity = _attr(proposal, "proposed_seats") or _attr(account, "seats_contracted", 0)
        base_value = _as_decimal(addon.get("price")) * _as_decimal(quantity)
    else:
        tier = _target_tier(proposal, account)
        plan = _tiers(catalog).get(tier, {})
        if not plan:
            return None
        seats = _attr(proposal, "proposed_seats") or _attr(account, "seats_contracted", 0)
        base_value = _as_decimal(plan.get("list_price")) * _as_decimal(seats)
    return _as_paise(base_value * proposed)


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


def _now(campaign_state: Any) -> datetime:
    supplied = _parse_datetime(_state_value(campaign_state, "now"))
    return supplied or datetime.now(timezone.utc)


def _offer_is_active(
    offer: Any,
    now: datetime,
    ttl_seconds: int,
    ttl_comparison: str = "exclusive",
) -> bool:
    if bool(_attr(offer, "redeemed", False)) or _attr(offer, "status") in {"expired", "consumed"}:
        return False
    expires_at = _parse_datetime(_attr(offer, "expires_at"))
    if expires_at is None:
        created_at = _parse_datetime(_attr(offer, "created_at"))
        if created_at is None:
            return False
        expires_at = created_at + timedelta(seconds=ttl_seconds)
    return expires_at > now if ttl_comparison == "exclusive" else expires_at >= now


def _pass(rule_id: str) -> RuleResult:
    return RuleResult(rule_id=rule_id, passed=True)


def _fail(rule_id: str, reason: str) -> RuleResult:
    return RuleResult(rule_id=rule_id, passed=False, reason=reason)


def _passes_floor(value: Decimal, threshold: Decimal, comparison: str) -> bool:
    return value >= threshold if comparison == "inclusive" else value > threshold


def _passes_ceiling(value: Decimal, threshold: Decimal, comparison: str) -> bool:
    return value <= threshold if comparison == "inclusive" else value < threshold


def r1_margin_floor(proposal: Any, account: Any, catalog: Any, campaign_state: Any) -> RuleResult:
    """R1: the resulting per-seat plan price must preserve the margin floor."""

    rule_id = "R1"
    config = _config(campaign_state)
    tier = _target_tier(proposal, account)
    if tier not in _tiers(catalog):
        return _fail(rule_id, f"Unknown target tier {tier!r}; cannot establish a margin.")
    proposed = _as_decimal(_attr(proposal, "proposed_discount"))
    if proposed < ZERO or proposed > ONE:
        return _fail(rule_id, "Proposed discount must be between 0% and 100%.")
    price_paise = _current_price_paise(proposal, account, catalog, tier)
    margin = _margin_for_price(tier, price_paise, catalog)
    # Call the catalog headroom helper as the shared arithmetic source for the
    # available band; the actual rule verdict is the catalog margin result.
    catalog_headroom = _catalog_rate(
        catalog,
        "discount_headroom",
        tier,
        _billing_cycle(account),
        _existing_discount(account),
    )
    if not _passes_floor(margin, config.margin_floor, config.margin_floor_comparison):
        return _fail(
            rule_id,
            f"Gross margin {margin:.2%} is below the {config.margin_floor:.2%} floor "
            f"at {price_paise} paise (catalog headroom: {catalog_headroom:.2%}).",
        )
    return _pass(rule_id)


def r2_tier_discount_ceiling(proposal: Any, account: Any, catalog: Any, campaign_state: Any) -> RuleResult:
    """R2: the newly proposed discount fits the per-offer tier cap."""

    rule_id = "R2"
    config = _config(campaign_state)
    tier = _target_tier(proposal, account)
    ceiling = config.per_offer_discount_ceilings.get(tier)
    if ceiling is None:
        return _fail(rule_id, f"No discount ceiling is configured for tier {tier!r}.")
    proposed = _as_decimal(_attr(proposal, "proposed_discount"))
    if proposed < ZERO or not _passes_ceiling(
        proposed, ceiling, config.per_offer_ceiling_comparison
    ):
        return _fail(
            rule_id,
            f"Proposed discount {proposed:.2%} exceeds the per-offer "
            f"{ceiling:.2%} {tier} ceiling.",
        )
    return _pass(rule_id)


def r3_addon_tier_gating(proposal: Any, account: Any, catalog: Any, campaign_state: Any) -> RuleResult:
    """R3: an add-on is available on the resulting tier."""

    rule_id = "R3"
    if _attr(proposal, "offer_type") != "addon":
        return _pass(rule_id)
    addon_name = _attr(proposal, "target_addon")
    addon = _addons(catalog).get(addon_name, {})
    tier = _target_tier(proposal, account)
    available_on = addon.get("available_on", ())
    if not addon:
        return _fail(rule_id, f"Unknown add-on {addon_name!r}.")
    if tier not in available_on:
        return _fail(rule_id, f"Add-on {addon_name!r} is not available on tier {tier!r}.")
    return _pass(rule_id)


def r4_no_stacking(proposal: Any, account: Any, catalog: Any, campaign_state: Any) -> RuleResult:
    """R4: annual standing discount cannot be stacked beyond combined authority."""

    rule_id = "R4"
    config = _config(campaign_state)
    if _billing_cycle(account) != "annual":
        return _pass(rule_id)
    tier = _target_tier(proposal, account)
    ceiling = config.combined_authority_ceilings.get(tier)
    if ceiling is None:
        return _fail(rule_id, f"No discount ceiling is configured for tier {tier!r}.")
    standing = _as_decimal(_attr(account, "standing_discount"))
    if standing == ZERO:
        standing = _catalog_rate(catalog, "cycle_discount", "annual")
    combined = standing + _existing_discount(account) + _as_decimal(_attr(proposal, "proposed_discount"))
    if not _passes_ceiling(
        combined, ceiling, config.combined_authority_comparison
    ):
        return _fail(
            rule_id,
            f"Annual standing discount plus existing and proposed discounts "
            f"total {combined:.2%}, beyond the {ceiling:.2%} {tier} ceiling.",
        )
    return _pass(rule_id)


def r5_one_active_offer(proposal: Any, account: Any, catalog: Any, campaign_state: Any) -> RuleResult:
    """R5: do not issue above the configured unexpired offer cap per customer."""

    rule_id = "R5"
    config = _config(campaign_state)
    now = _now(campaign_state)
    offers = _attr(account, "offer_history", ()) or ()
    active_count = sum(
        1
        for offer in offers
        if _offer_is_active(
            offer,
            now,
            config.offer_ttl_seconds,
            config.offer_ttl_comparison,
        )
    )
    cap_reached = (
        active_count >= config.per_customer_offer_cap
        if config.offer_cap_comparison == "inclusive"
        else active_count > config.per_customer_offer_cap
    )
    if cap_reached:
        return _fail(
            rule_id,
            f"Customer already has {active_count} unexpired, unredeemed offer(s); "
            f"the configured cap is {config.per_customer_offer_cap}.",
        )
    return _pass(rule_id)


def r6_campaign_budget(proposal: Any, account: Any, catalog: Any, campaign_state: Any) -> RuleResult:
    """R6: proposed discount value fits the campaign's remaining reservation."""

    rule_id = "R6"
    config = _config(campaign_state)
    campaign_id = _attr(proposal, "campaign_id")
    if not campaign_id:
        return _pass(rule_id)
    campaign = _campaign_record(campaign_state, campaign_id)
    if campaign is None:
        return _fail(rule_id, f"Campaign state for {campaign_id!r} is missing or mismatched.")
    budget = _first_value(campaign, ("allocated_budget_paise", "budget_paise"))
    reserved = _first_value(campaign, ("reserved_discount_paise", "reserved_total_paise"), 0)
    value = _discount_value_paise(proposal, account, catalog)
    if budget is None or value is None:
        return _fail(rule_id, "Campaign budget or deterministic discount value is unavailable.")
    budget_paise = _as_paise(budget)
    reserved_paise = _as_paise(reserved)
    budget_reached = (
        reserved_paise + value > budget_paise
        if config.campaign_budget_comparison == "inclusive"
        else reserved_paise + value >= budget_paise
    )
    if budget_reached:
        return _fail(
            rule_id,
            f"Discount reservation of {value} paise would exceed the campaign's "
            f"remaining budget of {max(0, budget_paise - reserved_paise)} paise.",
        )
    return _pass(rule_id)


def r7_upgrade_direction(proposal: Any, account: Any, catalog: Any, campaign_state: Any) -> RuleResult:
    """R7: upgrades move up a tier or to more seats; add-ons are not tier moves."""

    rule_id = "R7"
    if _attr(proposal, "offer_type") not in {"upgrade", "seat_expansion"}:
        return _pass(rule_id)
    current_tier = _account_tier(account)
    target_tier = _attr(proposal, "target_tier")
    tiers = _tiers(catalog)
    order = tuple(_attr(catalog, "TIER_ORDER", ("starter", "pro", "enterprise")))
    tier_up = current_tier in order and target_tier in order and order.index(target_tier) > order.index(current_tier)
    current_seats = _attr(account, "seats_contracted")
    proposed_seats = _attr(proposal, "proposed_seats")
    current_usage = _attr(account, "seats_in_use")
    if (
        proposed_seats is not None
        and current_usage is not None
        and proposed_seats < current_usage
    ):
        return _fail(
            rule_id,
            f"Proposed seat count {proposed_seats} is below current usage {current_usage}.",
        )
    seats_up = proposed_seats is not None and current_seats is not None and proposed_seats > current_seats
    if not tier_up and not seats_up:
        return _fail(
            rule_id,
            f"Upgrade must move above tier {current_tier!r} or increase seats "
            f"above {current_seats}; proposed tier={target_tier!r}, seats={proposed_seats!r}.",
        )
    return _pass(rule_id)


for _rule, _rule_id in (
    (r1_margin_floor, "R1"),
    (r2_tier_discount_ceiling, "R2"),
    (r3_addon_tier_gating, "R3"),
    (r4_no_stacking, "R4"),
    (r5_one_active_offer, "R5"),
    (r6_campaign_budget, "R6"),
    (r7_upgrade_direction, "R7"),
):
    _rule.rule_id = _rule_id  # type: ignore[attr-defined]


RULE_IDS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")
RULES: list[Rule] = [
    r1_margin_floor,
    r2_tier_discount_ceiling,
    r3_addon_tier_gating,
    r4_no_stacking,
    r5_one_active_offer,
    r6_campaign_budget,
    r7_upgrade_direction,
]
RULE_REGISTRY = RULES


__all__ = [
    "CampaignState",
    "DEFAULT_POLICY_CONFIG",
    "POLICY_CONFIG",
    "PolicyConfig",
    "Proposal",
    "RuleResult",
    "RULES",
    "RULE_REGISTRY",
    "RULE_IDS",
    "Rule",
    "r1_margin_floor",
    "r2_tier_discount_ceiling",
    "r3_addon_tier_gating",
    "r4_no_stacking",
    "r5_one_active_offer",
    "r6_campaign_budget",
    "r7_upgrade_direction",
]
