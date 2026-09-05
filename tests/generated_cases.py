"""Systematic generated policy cases and an independent evaluation report.

The valid matrix is exactly 20 account shapes x 28 proposal shapes = 560
cases. It is a cross-product of named, finite dimensions, not free-form random
generation. A further 24 malformed inputs exercise the proposal boundary.

Run from the repository root::

    python3 -m tests.generated_cases

This writes ``tests/evaluation_report.md``.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from data import catalog
from policy.checker import CheckResult, check_proposal
from policy.rules import CampaignState, Proposal, RULE_IDS


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_PATH = ROOT / "data" / "accounts.json"
REPORT_PATH = Path(__file__).resolve().parent / "evaluation_report.md"
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
PAISE_PER_RUPEE = 100
HAND_ASSERTIONS = 51
HAND_EXPECTED_REJECTED = 21
HAND_ASSERTIONS_BY_RULE = {
    "R1": 9,
    "R2": 9,
    "R3": 6,
    "R4": 9,
    "R5": 7,
    "R6": 6,
    "R7": 5,
}

# Literal oracle values are from sections 2 and 3 of the specification. They
# intentionally do not import thresholds from catalog.py or policy config.
ORACLE_MARGIN_FLOOR = Decimal("0.55")
ORACLE_ANNUAL_DISCOUNT = Decimal("0.20")
ORACLE_PER_OFFER_CEILINGS = {
    "starter": Decimal("0.10"),
    "pro": Decimal("0.15"),
    "enterprise": Decimal("0.20"),
}
ORACLE_COMBINED_CEILINGS = {
    "starter": Decimal("0.25"),
    "pro": Decimal("0.30"),
    "enterprise": Decimal("0.40"),
}
ORACLE_PLANS = {
    "starter": (Decimal("250"), Decimal("85")),
    "pro": (Decimal("600"), Decimal("180")),
    "enterprise": (Decimal("1200"), Decimal("320")),
}
ORACLE_ADDONS = {
    "unlimited_message_history": {"available_on": {"pro", "enterprise"}, "price": Decimal("80"), "per_seat": True},
    "guest_access_pack": {"available_on": {"pro", "enterprise"}, "price": Decimal("4000"), "per_seat": False},
    "compliance_export_audit_log": {"available_on": {"enterprise"}, "price": Decimal("15000"), "per_seat": False},
}


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    kind: str
    account_shape: str
    proposal_shape: str
    account: Any
    proposal: Any
    campaign_state: Any
    expected_verdict: str
    expected_fired_rules: tuple[str, ...]
    expected_exception: bool = False
    expectation_note: str = ""


def _paise(amount: Decimal) -> int:
    return int((amount * PAISE_PER_RUPEE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _price_from_paise(paise: int) -> Decimal:
    return Decimal(paise) / Decimal(PAISE_PER_RUPEE)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _active_offer(expires_at: datetime, offer_id: str = "generated-active") -> dict[str, Any]:
    return {
        "offer_id": offer_id,
        "created_at": NOW.isoformat(),
        "expires_at": expires_at.isoformat(),
        "redeemed": False,
        "status": "active",
    }


def _synthetic_account(name: str, tier: str, cycle: str) -> dict[str, Any]:
    return {
        "customer_id": f"synthetic-{name}",
        "tier": tier,
        "billing_cycle": cycle,
        "standing_discount": Decimal("0.20") if cycle == "annual" else Decimal("0"),
        "existing_discount": Decimal("0"),
        "seats_in_use": 10,
        "seats_contracted": 10,
        "offer_history": [],
    }


def _load_real_accounts() -> list[dict[str, Any]]:
    return json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))


def _account_shapes() -> list[tuple[str, dict[str, Any]]]:
    """Return 20 complete account shapes, using real data where it fits."""

    real = _load_real_accounts()
    by_tier_cycle: dict[tuple[str, str], dict[str, Any]] = {}
    for account in real:
        by_tier_cycle.setdefault((account["tier"], account["billing_cycle"]), account)

    specs = [
        ("starter_monthly", "starter", "monthly", {}),
        ("pro_monthly", "pro", "monthly", {}),
        ("enterprise_monthly", "enterprise", "monthly", {}),
        ("starter_annual", "starter", "annual", {}),
        ("pro_annual", "pro", "annual", {}),
        ("enterprise_annual", "enterprise", "annual", {}),
        ("starter_monthly_active", "starter", "monthly", {"active": True}),
        ("pro_monthly_active", "pro", "monthly", {"active": True}),
        ("enterprise_monthly_active", "enterprise", "monthly", {"active": True}),
        ("pro_annual_active", "pro", "annual", {"active": True}),
        ("starter_annual_combined", "starter", "annual", {"combined": True}),
        ("pro_annual_combined", "pro", "annual", {"combined": True}),
        ("enterprise_annual_combined", "enterprise", "annual", {"combined": True}),
        ("starter_annual_thin", "starter", "annual", {"thin": True}),
        ("starter_annual_thin_active", "starter", "annual", {"thin": True, "active": True}),
        ("pro_monthly_cap", "pro", "monthly", {"cap": True}),
        ("enterprise_monthly_cap", "enterprise", "monthly", {"cap": True}),
        ("enterprise_monthly_real", "enterprise", "monthly", {"real_only": True}),
        ("pro_monthly_hostile", "pro", "monthly", {"hostile": True}),
        ("starter_monthly_synthetic", "starter", "monthly", {"synthetic": True}),
    ]
    shapes: list[tuple[str, dict[str, Any]]] = []
    for name, tier, cycle, flags in specs:
        source = by_tier_cycle.get((tier, cycle))
        account = copy.deepcopy(source) if source and not flags.get("synthetic") else _synthetic_account(name, tier, cycle)
        account["customer_id"] = f"shape-{name}"
        account["tier"] = tier
        account["billing_cycle"] = cycle
        account["standing_discount"] = Decimal("0.20") if cycle == "annual" else Decimal("0")
        account["existing_discount"] = Decimal("0")
        account["seats_in_use"] = 10
        account["seats_contracted"] = 10
        account["offer_history"] = []
        if flags.get("cap"):
            account["existing_discount"] = ORACLE_PER_OFFER_CEILINGS[tier]
        if flags.get("combined"):
            account["existing_discount"] = ORACLE_COMBINED_CEILINGS[tier] - Decimal("0.20")
        if flags.get("thin"):
            account["existing_discount"] = Decimal("0.02")
        if flags.get("active"):
            account["offer_history"] = [_active_offer(NOW + timedelta(seconds=60))]
        if flags.get("real_only"):
            account["seats_in_use"] = 43
            account["seats_contracted"] = 111
        if flags.get("hostile"):
            account["escalation_flag"] = True
        account["shape_flags"] = sorted(flag for flag, enabled in flags.items() if enabled)
        shapes.append((name, account))
    return shapes


def _proposal_shapes() -> list[tuple[str, dict[str, Any]]]:
    """Return 28 named proposal shapes spanning the requested dimensions."""

    return [
        ("upgrade_zero_valid", {"offer_type": "upgrade", "target_tier": "enterprise", "discount": "0", "seats": 11, "campaign": "none"}),
        ("upgrade_small_valid_remaining", {"offer_type": "upgrade", "target_tier": "enterprise", "discount": "0.05", "seats": 11, "campaign": "remaining"}),
        ("upgrade_exact_ceiling_valid", {"offer_type": "upgrade", "target_tier": "enterprise", "discount": "0.20", "seats": 11, "campaign": "none"}),
        ("upgrade_one_over_ceiling", {"offer_type": "upgrade", "target_tier": "enterprise", "discount": "0.21", "seats": 11, "campaign": "nearly"}),
        ("upgrade_absurd", {"offer_type": "upgrade", "target_tier": "enterprise", "discount": "0.90", "seats": 11, "campaign": "none"}),
        ("upgrade_same_tier", {"offer_type": "upgrade", "target_tier": "pro", "discount": "0", "seats": 10, "campaign": "none"}),
        ("upgrade_lower_tier", {"offer_type": "upgrade", "target_tier": "starter", "discount": "0.05", "seats": 10, "campaign": "none"}),
        ("upgrade_invalid_target", {"offer_type": "upgrade", "target_tier": "not-a-tier", "discount": "0.05", "seats": 11, "campaign": "none"}),
        ("upgrade_small_nearly_exhausted", {"offer_type": "upgrade", "target_tier": "enterprise", "discount": "0.01", "seats": 11, "campaign": "nearly"}),
        ("upgrade_zero_budget_remaining", {"offer_type": "upgrade", "target_tier": "pro", "discount": "0", "seats": 11, "campaign": "remaining"}),
        ("upgrade_small_same_tier_seat_increase", {"offer_type": "upgrade", "target_tier": "pro", "discount": "0.05", "seats": 11, "campaign": "none"}),
        ("upgrade_small_below_usage", {"offer_type": "upgrade", "target_tier": "enterprise", "discount": "0.05", "seats": 9, "campaign": "none"}),
        ("addon_guest_zero_valid", {"offer_type": "addon", "target_tier": None, "addon": "guest_access_pack", "discount": "0", "seats": None, "campaign": "none"}),
        ("addon_guest_small_remaining", {"offer_type": "addon", "target_tier": None, "addon": "guest_access_pack", "discount": "0.05", "seats": None, "campaign": "remaining"}),
        ("addon_guest_exact_ceiling", {"offer_type": "addon", "target_tier": None, "addon": "guest_access_pack", "discount": "0.20", "seats": None, "campaign": "none"}),
        ("addon_guest_one_over", {"offer_type": "addon", "target_tier": None, "addon": "guest_access_pack", "discount": "0.21", "seats": None, "campaign": "nearly"}),
        ("addon_compliance_valid", {"offer_type": "addon", "target_tier": None, "addon": "compliance_export_audit_log", "discount": "0", "seats": None, "campaign": "none"}),
        ("addon_compliance_invalid", {"offer_type": "addon", "target_tier": None, "addon": "compliance_export_audit_log", "discount": "0.05", "seats": None, "campaign": "none"}),
        ("addon_history_valid", {"offer_type": "addon", "target_tier": None, "addon": "unlimited_message_history", "discount": "0.05", "seats": None, "campaign": "remaining"}),
        ("addon_history_invalid", {"offer_type": "addon", "target_tier": None, "addon": "unlimited_message_history", "discount": "0.05", "seats": None, "campaign": "none"}),
        ("addon_guest_absurd", {"offer_type": "addon", "target_tier": None, "addon": "guest_access_pack", "discount": "0.90", "seats": None, "campaign": "none"}),
        ("addon_guest_nearly", {"offer_type": "addon", "target_tier": None, "addon": "guest_access_pack", "discount": "0.01", "seats": None, "campaign": "nearly"}),
        ("seat_zero_increase", {"offer_type": "seat_expansion", "target_tier": None, "discount": "0", "seats": 11, "campaign": "none"}),
        ("seat_small_remaining", {"offer_type": "seat_expansion", "target_tier": None, "discount": "0.05", "seats": 11, "campaign": "remaining"}),
        ("seat_exact_ceiling", {"offer_type": "seat_expansion", "target_tier": None, "discount": "0.15", "seats": 11, "campaign": "none"}),
        ("seat_one_over", {"offer_type": "seat_expansion", "target_tier": None, "discount": "0.16", "seats": 11, "campaign": "nearly"}),
        ("seat_absurd", {"offer_type": "seat_expansion", "target_tier": None, "discount": "0.90", "seats": 11, "campaign": "none"}),
        ("seat_below_usage", {"offer_type": "seat_expansion", "target_tier": None, "discount": "0.05", "seats": 9, "campaign": "none"}),
    ]


def _make_proposal(shape: dict[str, Any], account: dict[str, Any]) -> Proposal:
    offer_type = shape["offer_type"]
    target_tier = shape["target_tier"]
    if offer_type == "seat_expansion":
        target_tier = account["tier"]
    return Proposal(
        customer_id=account["customer_id"],
        offer_type=offer_type,
        target_tier=target_tier,
        target_addon=shape.get("addon"),
        proposed_discount=Decimal(shape["discount"]),
        campaign_id=None if shape["campaign"] == "none" else "generated-campaign",
        reason="Systematic generated fixture.",
        proposed_seats=shape["seats"],
    )


def _campaign_state(shape: dict[str, Any]) -> CampaignState:
    if shape["campaign"] == "none":
        return CampaignState(now=NOW)
    if shape["campaign"] == "remaining":
        return CampaignState(campaign_id="generated-campaign", allocated_budget_paise=1_000_000, reserved_discount_paise=0, now=NOW)
    return CampaignState(campaign_id="generated-campaign", allocated_budget_paise=100_000, reserved_discount_paise=99_999, now=NOW)


def _active(account: dict[str, Any]) -> int:
    count = 0
    for offer in account.get("offer_history", []):
        expires = datetime.fromisoformat(offer["expires_at"].replace("Z", "+00:00"))
        if not offer.get("redeemed", False) and offer.get("status") not in {"expired", "consumed"} and expires > NOW:
            count += 1
    return count


def _oracle(account: dict[str, Any], proposal: Proposal, state: CampaignState) -> tuple[str, tuple[str, ...], str]:
    """Independent literal oracle based on the written specification."""

    failures: list[str] = []
    tier = proposal.target_tier or account["tier"]
    discount = Decimal(str(proposal.proposed_discount))
    if tier not in ORACLE_PLANS:
        # Unknown targets fail the arithmetic rules. Continue through the
        # independent checks whose inputs remain well-defined.
        failures.extend(("R1", "R2"))
        if account["billing_cycle"] == "annual":
            failures.append("R4")
        if _active(account) >= 1:
            failures.append("R5")
        if proposal.offer_type in {"upgrade", "seat_expansion"}:
            below_usage = proposal.proposed_seats is not None and proposal.proposed_seats < account["seats_in_use"]
            seats_up = proposal.proposed_seats is not None and proposal.proposed_seats > account["seats_contracted"]
            if below_usage or not seats_up:
                failures.append("R7")
        return "rejected", tuple(dict.fromkeys(failures)), "Unknown target tier."

    list_price, cost = ORACLE_PLANS[tier]
    cycle = Decimal("0.20") if account["billing_cycle"] == "annual" else Decimal("0")
    existing = Decimal(str(account.get("existing_discount", 0)))
    price_paise = _paise(list_price * (Decimal("1") - cycle) * (Decimal("1") - existing) * (Decimal("1") - discount))
    price = _price_from_paise(price_paise)
    margin = (price - cost) / price if price > 0 else Decimal("-1")
    if margin < ORACLE_MARGIN_FLOOR:
        failures.append("R1")
    if discount < 0 or discount > ORACLE_PER_OFFER_CEILINGS[tier]:
        failures.append("R2")
    if proposal.offer_type == "addon":
        addon = ORACLE_ADDONS.get(proposal.target_addon)
        if not addon or tier not in addon["available_on"]:
            failures.append("R3")
    if account["billing_cycle"] == "annual":
        standing = Decimal(str(account.get("standing_discount", ORACLE_ANNUAL_DISCOUNT))) or ORACLE_ANNUAL_DISCOUNT
        if standing + existing + discount > ORACLE_COMBINED_CEILINGS[tier]:
            failures.append("R4")
    if _active(account) >= 1:
        failures.append("R5")
    if proposal.campaign_id:
        if proposal.offer_type == "addon":
            addon = ORACLE_ADDONS.get(proposal.target_addon)
            base_value = addon["price"] * (Decimal(str(account["seats_contracted"])) if addon and addon["per_seat"] else Decimal("1")) if addon else Decimal("0")
        else:
            base_value = list_price * Decimal(str(proposal.proposed_seats or account["seats_contracted"]))
        value = _paise(base_value * discount)
        budget = state.allocated_budget_paise or 0
        if state.reserved_discount_paise + value > budget:
            failures.append("R6")
    if proposal.offer_type in {"upgrade", "seat_expansion"}:
        order = ("starter", "pro", "enterprise")
        tier_up = tier in order and account["tier"] in order and order.index(tier) > order.index(account["tier"])
        seats_up = proposal.proposed_seats is not None and proposal.proposed_seats > account["seats_contracted"]
        below_usage = proposal.proposed_seats is not None and proposal.proposed_seats < account["seats_in_use"]
        if below_usage or not (tier_up or seats_up):
            failures.append("R7")
    return ("rejected" if failures else "approved", tuple(dict.fromkeys(failures)), "Literal oracle verdict.")


def build_valid_cases() -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    for account_shape, account in _account_shapes():
        for proposal_shape, shape in _proposal_shapes():
            proposal = _make_proposal(shape, account)
            state = _campaign_state(shape)
            expected_verdict, expected_fired, note = _oracle(account, proposal, state)
            cases.append(EvaluationCase(
                case_id=f"{account_shape}__{proposal_shape}", kind="generated",
                account_shape=account_shape, proposal_shape=proposal_shape,
                account=account, proposal=proposal, campaign_state=state,
                expected_verdict=expected_verdict, expected_fired_rules=expected_fired,
                expectation_note=note,
            ))
    assert len(cases) == 560
    return cases


def build_malformed_cases() -> list[EvaluationCase]:
    account = _synthetic_account("malformed", "pro", "monthly")
    base = {
        "customer_id": account["customer_id"], "offer_type": "upgrade",
        "target_tier": "enterprise", "proposed_discount": Decimal("0"),
        "proposed_seats": 11, "reason": "Malformed fixture.",
    }
    raw_cases = [
        ("negative_discount", {**base, "proposed_discount": Decimal("-0.01")}, "reject", ("R1", "R2")),
        ("null_discount", {**base, "proposed_discount": None}, "raise", ()),
        ("float_discount", {**base, "proposed_discount": 0.1}, "raise", ()),
        ("excessive_decimal_discount", {**base, "proposed_discount": Decimal("0.123")}, "raise", ()),
        ("nonnumeric_discount", {**base, "proposed_discount": "free"}, "raise", ()),
        ("null_customer_id", {**base, "customer_id": None}, "raise", ()),
        ("empty_customer_id", {**base, "customer_id": ""}, "raise", ()),
        ("null_offer_type", {**base, "offer_type": None}, "raise", ()),
        ("unsupported_offer_type", {**base, "offer_type": "coupon"}, "raise", ()),
        ("null_target_tier", {**base, "target_tier": None}, "raise", ()),
        ("unknown_target_tier", {**base, "target_tier": "bogus"}, "reject", ("R1", "R2")),
        ("null_reason", {**base, "reason": None}, "raise", ()),
        ("seat_below_usage", {**base, "proposed_seats": 9}, "reject", ("R7",)),
        ("negative_seats", {**base, "proposed_seats": -1}, "reject", ("R7",)),
        ("null_seats", {**base, "proposed_seats": None}, "raise", ()),
        ("addon_with_target_tier", {**base, "offer_type": "addon", "target_tier": "pro", "target_addon": "guest_access_pack", "proposed_seats": None}, "raise", ()),
        ("addon_without_target_addon", {**base, "offer_type": "addon", "target_tier": None, "target_addon": None, "proposed_seats": None}, "raise", ()),
        ("unknown_addon", {**base, "offer_type": "addon", "target_tier": None, "target_addon": "unknown", "proposed_seats": None}, "reject", ("R3",)),
        ("nonexistent_campaign", {**base, "campaign_id": "does-not-exist"}, "reject", ("R6",)),
        ("campaign_without_budget", {**base, "campaign_id": "campaign-x"}, "reject", ("R6",)),
        ("boolean_discount", {**base, "proposed_discount": True}, "raise", ()),
        ("boolean_seats", {**base, "proposed_seats": True}, "raise", ()),
        ("missing_customer_id_key", {key: value for key, value in base.items() if key != "customer_id"}, "raise", ()),
        ("null_target_addon_for_addon", {**base, "offer_type": "addon", "target_tier": None, "target_addon": None, "proposed_seats": None}, "raise", ()),
    ]
    cases: list[EvaluationCase] = []
    for name, raw, expected_action, expected_fired_rules in raw_cases:
        expected_verdict = "rejected" if expected_action == "reject" else "raise"
        cases.append(EvaluationCase(
            case_id=f"malformed__{name}", kind="malformed",
            account_shape="malformed_account", proposal_shape=name,
            account=account, proposal=raw, campaign_state=CampaignState(now=NOW),
            expected_verdict=expected_verdict, expected_fired_rules=expected_fired_rules,
            expected_exception=expected_action == "raise",
            expectation_note=f"Malformed input should {expected_action}; it must not be accepted.",
        ))
    assert len(cases) == 24
    return cases


def evaluate(cases: Iterable[EvaluationCase]) -> dict[str, Any]:
    totals = {"total": 0, "expected_rejected": 0, "expected_raised": 0, "actual_rejected": 0, "actual_approved": 0, "crashes": 0, "false_allows": 0, "false_blocks": 0}
    per_rule_expected = {rule_id: 0 for rule_id in RULE_IDS}
    per_rule_actual = {rule_id: 0 for rule_id in RULE_IDS}
    crash_cases: list[dict[str, str]] = []
    false_allow_cases: list[str] = []
    false_block_cases: list[str] = []
    records: list[dict[str, Any]] = []
    for case in cases:
        totals["total"] += 1
        if case.expected_verdict == "rejected":
            totals["expected_rejected"] += 1
        if case.expected_exception:
            totals["expected_raised"] += 1
        for rule_id in case.expected_fired_rules:
            per_rule_expected[rule_id] += 1
        try:
            result: CheckResult = check_proposal(case.proposal, case.account, catalog, case.campaign_state)
            actual_verdict = result.verdict
            actual_fired = tuple(rule.rule_id for rule in result.fired_rules)
            totals["actual_rejected"] += actual_verdict == "rejected"
            totals["actual_approved"] += actual_verdict == "approved"
            for rule_id in actual_fired:
                if rule_id in per_rule_actual:
                    per_rule_actual[rule_id] += 1
            if case.expected_verdict == "rejected" and actual_verdict == "approved":
                totals["false_allows"] += 1
                false_allow_cases.append(case.case_id)
            elif case.expected_verdict == "approved" and actual_verdict == "rejected":
                totals["false_blocks"] += 1
                false_block_cases.append(case.case_id)
            elif case.expected_exception and actual_verdict != "rejected":
                totals["false_allows"] += 1
                false_allow_cases.append(case.case_id)
            records.append({"case_id": case.case_id, "expected": case.expected_verdict, "actual": actual_verdict, "expected_fired": list(case.expected_fired_rules), "actual_fired": list(actual_fired)})
        except Exception as exc:  # noqa: BLE001 - crash reporting is part of this evaluator.
            totals["crashes"] += 1
            crash_cases.append({"case_id": case.case_id, "type": type(exc).__name__, "message": str(exc)})
            records.append({"case_id": case.case_id, "expected": case.expected_verdict, "actual": "crashed", "expected_fired": list(case.expected_fired_rules), "actual_fired": []})
    return {
        "totals": totals,
        "per_rule_expected": per_rule_expected,
        "per_rule_actual": per_rule_actual,
        "crash_cases": crash_cases,
        "false_allow_cases": false_allow_cases,
        "false_block_cases": false_block_cases,
        "records": records,
    }


def write_report(
    report: dict[str, Any],
    hand_assertions: int = HAND_ASSERTIONS,
    hand_expected_rejected: int = HAND_EXPECTED_REJECTED,
) -> None:
    totals = report["totals"]
    lines = [
        "# Policy checker evaluation",
        "",
        "The valid generated matrix is 20 account shapes × 28 proposal shapes = 560 cases.",
        "Malformed inputs add 24 cases. Expected verdicts come from the literal oracle in `generated_cases.py`.",
        "The oracle does not call `rules.py`, `checker.py`, or `catalog.py`; those are used only to produce the actual checker result. This is therefore a correctness comparison, not merely a consistency sweep.",
        "",
        "## Case totals",
        "",
        "| Set | Cases | Expected rejected | Expected raise |",
        "|---|---:|---:|---:|",
        f"| Hand-written boundary assertions | {hand_assertions} individual assertions | {hand_expected_rejected} | 0 |",
        f"| Generated valid matrix | 560 | {sum(1 for r in report['records'][:560] if r['expected'] == 'rejected')} | 0 |",
        f"| Malformed | 24 | {sum(1 for r in report['records'][560:] if r['expected'] == 'rejected')} | {sum(1 for r in report['records'][560:] if r['expected'] == 'raise')} |",
        f"| Evaluated total | {totals['total']} | {totals['expected_rejected']} | {totals['expected_raised']} |",
        "",
        "## Hand-written assertion count",
        "",
        "| Rule | Individual assertions |",
        "|---|---:|",
        f"| R1 | {HAND_ASSERTIONS_BY_RULE['R1']} |",
        f"| R2 | {HAND_ASSERTIONS_BY_RULE['R2']} |",
        f"| R3 | {HAND_ASSERTIONS_BY_RULE['R3']} |",
        f"| R4 | {HAND_ASSERTIONS_BY_RULE['R4']} |",
        f"| R5 | {HAND_ASSERTIONS_BY_RULE['R5']} |",
        f"| R6 | {HAND_ASSERTIONS_BY_RULE['R6']} |",
        f"| R7 | {HAND_ASSERTIONS_BY_RULE['R7']} |",
        f"| Total | {sum(HAND_ASSERTIONS_BY_RULE.values())} |",
        "",
        "## Outcomes",
        "",
        f"- Actual rejected: **{totals['actual_rejected']}**",
        f"- Actual approved: **{totals['actual_approved']}**",
        f"- False allows: **{totals['false_allows']}** — target zero.",
        f"- False blocks: **{totals['false_blocks']}** — reported honestly.",
        f"- Checker crashes: **{totals['crashes']}** — reported separately below.",
        "",
        "## Per-rule firing counts",
        "",
        "| Rule | Expected fires | Actual fires | Status |",
        "|---|---:|---:|---|",
    ]
    for rule_id in RULE_IDS:
        expected = report["per_rule_expected"][rule_id]
        actual = report["per_rule_actual"][rule_id]
        status = "FLAG: never fires" if actual == 0 else "covered"
        lines.append(f"| {rule_id} | {expected} | {actual} | {status} |")
    lines.extend(["", "## Crash cases", ""])
    if report["crash_cases"]:
        lines.extend(f"- `{item['case_id']}`: `{item['type']}: {item['message']}`" for item in report["crash_cases"])
    else:
        lines.append("None.")
    lines.extend(["", "## False allows", ""])
    lines.extend(f"- `{case_id}`" for case_id in report["false_allow_cases"]) or lines.append("None.")
    lines.extend(["", "## False blocks", ""])
    lines.extend(f"- `{case_id}`" for case_id in report["false_block_cases"]) or lines.append("None.")
    lines.extend([
        "",
        "## Malformed expectations",
        "",
        "Expected rejection: " + ", ".join(
            f"`{record['case_id']}`"
            for record in report["records"][560:]
            if record["expected"] == "rejected"
        ) + ".",
        "Expected raise: " + ", ".join(
            f"`{record['case_id']}`"
            for record in report["records"][560:]
            if record["expected"] == "raise"
        ) + ".",
        "",
        "## Boundary semantics",
        "",
        "- R1 margin floor, R2 per-offer ceiling, R4 combined authority, and R6 budget are inclusive at exact equality.",
        "- R5 offer expiry is deliberately exclusive: exact expiry is expired. R5 offer-cap comparison is inclusive.",
        "- Hand-written expected verdicts are literal booleans reasoned from the spec; generated expected verdicts use the independent literal oracle in this file.",
    ])
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    valid = build_valid_cases()
    malformed = build_malformed_cases()
    report = evaluate([*valid, *malformed])
    write_report(report)
    print(json.dumps({"valid": len(valid), "malformed": len(malformed), **report["totals"], "per_rule_actual": report["per_rule_actual"]}, indent=2))
    print(f"Wrote {REPORT_PATH}")


if __name__ == "__main__":
    main()
