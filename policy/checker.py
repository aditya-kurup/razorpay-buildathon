"""Registry-driven proposal checker."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from data import catalog as default_catalog

from .rules import Proposal, Rule, RuleResult, RULE_REGISTRY


@dataclass(frozen=True)
class CheckResult:
    """Complete adjudication, including every rule result and every failure."""

    approved: bool
    rule_results: tuple[RuleResult, ...]

    @property
    def verdict(self) -> str:
        return "approved" if self.approved else "rejected"

    @property
    def fired_rules(self) -> tuple[RuleResult, ...]:
        return tuple(result for result in self.rule_results if result.failed)

    @property
    def failures(self) -> tuple[RuleResult, ...]:
        return self.fired_rules


def _proposal_from_mapping(proposal: Any) -> Any:
    if isinstance(proposal, Proposal):
        return proposal
    if isinstance(proposal, Mapping):
        fields = {
            "customer_id",
            "offer_type",
            "target_tier",
            "target_addon",
            "proposed_discount",
            "campaign_id",
            "reason",
            "proposed_seats",
        }
        return Proposal(**{field: proposal.get(field) for field in fields if field in proposal})
    return proposal


def _validate_proposal_shape(proposal: Any) -> None:
    """Raise on malformed boundary input rather than silently normalizing it."""

    required = {
        "customer_id": "customer id",
        "offer_type": "offer type",
        "proposed_discount": "proposed discount",
        "reason": "model reason",
    }
    for field, label in required.items():
        value = proposal.get(field) if isinstance(proposal, Mapping) else getattr(proposal, field, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"Malformed proposal: {label} is required")

    offer_type = proposal.get("offer_type") if isinstance(proposal, Mapping) else proposal.offer_type
    if offer_type not in {"upgrade", "addon", "seat_expansion"}:
        raise ValueError(f"Malformed proposal: unsupported offer type {offer_type!r}")

    discount = proposal.get("proposed_discount") if isinstance(proposal, Mapping) else proposal.proposed_discount
    if isinstance(discount, float):
        raise ValueError("Malformed proposal: proposed discount must not be a float")
    try:
        decimal_discount = Decimal(str(discount))
    except Exception as exc:
        raise ValueError("Malformed proposal: proposed discount is not numeric") from exc
    if decimal_discount.as_tuple().exponent < -2:
        raise ValueError("Malformed proposal: proposed discount has more than two decimal places")

    target_tier = proposal.get("target_tier") if isinstance(proposal, Mapping) else proposal.target_tier
    target_addon = proposal.get("target_addon") if isinstance(proposal, Mapping) else proposal.target_addon
    if offer_type == "addon" and target_addon is None:
        raise ValueError("Malformed proposal: add-on proposal requires target_addon")
    if offer_type in {"upgrade", "seat_expansion"} and target_tier is None:
        raise ValueError(f"Malformed proposal: {offer_type} proposal requires target_tier")
    if offer_type == "addon" and target_tier is not None:
        raise ValueError("Malformed proposal: add-on proposal must not name target_tier")
    if offer_type in {"upgrade", "seat_expansion"}:
        proposed_seats = proposal.get("proposed_seats") if isinstance(proposal, Mapping) else proposal.proposed_seats
        if proposed_seats is None:
            raise ValueError(f"Malformed proposal: {offer_type} proposal requires proposed_seats")
        if isinstance(proposed_seats, bool) or not isinstance(proposed_seats, int):
            raise ValueError("Malformed proposal: proposed_seats must be an integer")


def check_proposal(
    proposal: Any,
    account: Any,
    catalog: Any = default_catalog,
    campaign_state: Any = None,
    registry: list[Rule] | tuple[Rule, ...] = RULE_REGISTRY,
) -> CheckResult:
    """Run the registry completely and return all rule failures.

    ``registry`` is injectable for focused tests, while production callers use
    the seven-rule registry. The checker has no rule-specific branching.
    """

    _validate_proposal_shape(proposal)
    normalized = _proposal_from_mapping(proposal)
    results = tuple(
        rule(normalized, account, catalog, campaign_state) for rule in registry
    )
    return CheckResult(
        approved=all(result.passed for result in results),
        rule_results=results,
    )


check = check_proposal


__all__ = ["CheckResult", "check", "check_proposal"]
