#!/usr/bin/env python3
"""Compute synthetic revenue metrics from the existing merchant evidence.

This is intentionally an analysis-only reader.  It opens SQLite in read-only
mode and writes only the JSON report; it never calls a pipeline, payment
endpoint, model client, or state-changing merchant helper.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote


SERVICE_ROOT = Path(__file__).resolve().parents[1]
GROWTH_ROOT = Path(
    os.environ.get(
        "GROWTH_AGENT_ROOT",
        str(SERVICE_ROOT),
    )
)
DB_PATH = SERVICE_ROOT / "merchant.sqlite3"
ACCOUNTS_PATH = GROWTH_ROOT / "data" / "accounts.json"
REPORT_PATH = SERVICE_ROOT / "notes" / "revenue-metrics.json"
AS_OF_DATE = date(2026, 8, 29)
AS_OF_DATETIME = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
CAMPAIGN_ADDON = "guest_access_pack"
GENESIS = "GENESIS"

# The repository does not document a gemini-3.5-flash-lite price card and the
# response log has no token usage.  Keep these explicit and configurable so a
# later run can replace them without changing the computation code.
DEFAULT_INPUT_USD_PER_MILLION = Decimal("0.10")
DEFAULT_OUTPUT_USD_PER_MILLION = Decimal("0.40")


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _rate(value: str | None, default: Decimal) -> Decimal:
    return _decimal(value, default)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    """Open SQLite without invoking merchant schema initialization."""

    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _load_catalog() -> Any:
    if str(GROWTH_ROOT) not in sys.path:
        sys.path.insert(0, str(GROWTH_ROOT))
    from data import catalog

    return catalog


def _load_accounts() -> dict[str, dict[str, Any]]:
    rows = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    return {str(row["customer_id"]): row for row in rows}


def _billing_multiplier(account: Mapping[str, Any]) -> Decimal:
    return Decimal("12") if account.get("billing_cycle", "monthly") == "monthly" else Decimal("1")


def _current_revenue(account: Mapping[str, Any]) -> Decimal:
    invoices = account.get("invoices") or []
    latest = invoices[-1] if invoices else {}
    seats = _decimal(account.get("seats_in_use"))
    effective_price = _decimal(latest.get("price_per_seat"))
    return seats * effective_price * _billing_multiplier(account)


def _renewal_triggered(account: Mapping[str, Any]) -> bool:
    """Match the existing trigger behavior when renewal_date is absent.

    The synthetic accounts store signup_date plus invoice history rather than
    a renewal_date.  The service derives the next anniversary from signup_date.
    """

    raw = account.get("renewal_date")
    if raw:
        renewal = _iso_datetime(str(raw))
        if renewal is not None:
            return 0 <= (renewal.date() - AS_OF_DATE).days <= 30
        try:
            renewal_date = date.fromisoformat(str(raw))
            return 0 <= (renewal_date - AS_OF_DATE).days <= 30
        except ValueError:
            return False
    try:
        signup = date.fromisoformat(str(account["signup_date"]))
        anniversary = signup.replace(year=AS_OF_DATE.year)
        if anniversary < AS_OF_DATE:
            anniversary = anniversary.replace(year=AS_OF_DATE.year + 1)
        return 0 <= (anniversary - AS_OF_DATE).days <= 30
    except (KeyError, TypeError, ValueError):
        return False


def _trigger_info(account: Mapping[str, Any]) -> dict[str, bool]:
    contracted = int(account.get("seats_contracted") or 0)
    in_use = int(account.get("seats_in_use") or 0)
    return {
        "seat_ceiling": contracted > 0 and in_use / contracted >= 0.90,
        "renewal_window": _renewal_triggered(account),
    }


def _campaign_members(accounts: Mapping[str, Mapping[str, Any]], catalog: Any) -> dict[str, list[str]]:
    cohorts: dict[str, list[Mapping[str, Any]]] = {"small": [], "medium": [], "large": []}
    for account in accounts.values():
        tier = account.get("tier")
        active = set(account.get("active_addons") or [])
        if (
            tier not in catalog.TIERS
            or not catalog.addon_is_available(CAMPAIGN_ADDON, tier)
            or CAMPAIGN_ADDON in active
        ):
            continue
        seats = int(account.get("seats_contracted") or 0)
        band = "small" if seats <= 20 else "medium" if seats <= 60 else "large"
        cohorts[band].append(account)
    return {
        f"seats_{band}": [str(a["customer_id"]) for a in sorted(rows, key=lambda a: str(a["customer_id"]))]
        for band, rows in cohorts.items()
    }


def _band_ceiling(account: Mapping[str, Any], catalog: Any) -> Decimal:
    """Reproduce the default eligibility discount band without changing it."""

    tier = str(account.get("tier"))
    plan = catalog.TIERS[tier]
    existing = _decimal(account.get("existing_discount"))
    standing = _decimal(account.get("standing_discount"))
    if "standing_discount" not in account:
        standing = catalog.cycle_discount(str(account.get("billing_cycle", "monthly")))
    per_offer = _decimal(plan.get("max_discount"))
    combined_ceiling = {
        "starter": Decimal("0.25"),
        "pro": Decimal("0.30"),
        "enterprise": Decimal("0.40"),
    }[tier]
    margin_limit = catalog.margin_discount_limit(
        tier,
        str(account.get("billing_cycle", "monthly")),
        existing,
    )
    return max(Decimal("0"), min(per_offer, combined_ceiling - standing - existing, margin_limit))


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        decoded = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _offer_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT o.*,
               (SELECT t.occurred_at
                  FROM offer_transitions AS t
                 WHERE t.offer_id = o.offer_id
                 ORDER BY t.id LIMIT 1) AS created_at
          FROM merchant_offers AS o
         ORDER BY created_at, o.offer_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _audit_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT seq,timestamp,event_type,actor,subject_id,payload_json FROM audit_events ORDER BY seq"
    ).fetchall()


def _match_proposal(
    offer: Mapping[str, Any],
    proposals: Mapping[str, list[dict[str, Any]]],
    stored_seq: int | None = None,
) -> dict[str, Any] | None:
    if stored_seq is not None:
        # For older dashboard runs, wall-clock timestamps were occasionally
        # recorded out of order.  The append-only audit sequence is the
        # stronger relationship when the offer's own audit row is present.
        prior = [
            proposal
            for proposal in proposals.get(str(offer["customer_id"]), [])
            if proposal["seq"] < stored_seq
        ]
        if prior:
            return max(prior, key=lambda item: item["seq"])
    created = _iso_datetime(offer.get("created_at"))
    if created is None:
        return None
    candidates = []
    for proposal in proposals.get(str(offer["customer_id"]), []):
        event_time = proposal["timestamp"]
        if event_time <= created:
            delta = (created - event_time).total_seconds()
            # A stored offer is written immediately after its proposal.  A
            # narrow window prevents a later unrelated run from being linked.
            if delta <= 10:
                candidates.append((delta, proposal))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _proposal_events(rows: Iterable[sqlite3.Row]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["event_type"] != "PROPOSAL_GENERATED":
            continue
        data = _payload(row)
        parsed = data.get("parsed_proposal_fields")
        if not isinstance(parsed, dict):
            continue
        result[str(row["subject_id"])].append(
            {
                "seq": int(row["seq"]),
                "timestamp": _iso_datetime(row["timestamp"]),
                "actor": row["actor"],
                "pipeline": data.get("pipeline"),
                "parsed": parsed,
            }
        )
    for values in result.values():
        values.sort(key=lambda item: item["timestamp"])
    return result


def _offer_stored_ids(rows: Iterable[sqlite3.Row]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for row in rows:
        if row["event_type"] != "OFFER_STORED":
            continue
        data = _payload(row)
        if row["actor"] == "system" and data.get("offer_id"):
            ids[str(data["offer_id"])] = int(row["seq"])
    return ids


def _offer_metrics(
    offer: Mapping[str, Any],
    proposal: Mapping[str, Any] | None,
    accounts: Mapping[str, Mapping[str, Any]],
    catalog: Any,
    stored_seq: int | None = None,
) -> dict[str, Any]:
    customer_id = str(offer["customer_id"])
    account = accounts.get(customer_id)
    pipeline = "campaign" if offer.get("campaign_id") else "upsell"
    result: dict[str, Any] = {
        "offer_id": offer["offer_id"],
        "customer_id": customer_id,
        "pipeline": pipeline,
        "status": offer["status"],
        "created_at": offer.get("created_at"),
        "stored_price_paise": offer["price_paise"],
        "stored_discount_value_paise": offer["discount_value_paise"],
        "currency": offer["currency"],
        "campaign_id": offer.get("campaign_id"),
        "terms_reconstructed": proposal is not None and account is not None,
        "expected_arr_uplift_rupees": None,
        "margin_retained_rupees": None,
    }
    if proposal is None or account is None:
        result["unresolved_reason"] = "no matching PROPOSAL_GENERATED audit row for this legacy offer"
        return result

    parsed = proposal["parsed"]
    offer_type = parsed.get("offer_type")
    target_tier = parsed.get("target_tier") or account.get("tier")
    target_addon = parsed.get("target_addon")
    actual_discount = _decimal(parsed.get("proposed_discount"))
    proposed_seats = parsed.get("proposed_seats")
    multiplier = _billing_multiplier(account)
    result.update(
        {
            "matched_proposal_seq": proposal["seq"],
            "offer_type": offer_type,
            "target_tier": target_tier,
            "target_addon": target_addon,
            "proposed_discount": str(actual_discount),
            "proposed_seats": proposed_seats,
            "model_proposal_pipeline": proposal.get("pipeline"),
            "discount_band_ceiling": str(_band_ceiling(account, catalog)),
        }
    )

    if offer_type in {"upgrade", "seat_expansion"} and proposed_seats is not None and target_tier in catalog.TIERS:
        list_price = _decimal(catalog.TIERS[target_tier]["list_price"])
        current_arr = _current_revenue(account)
        new_arr = _decimal(proposed_seats) * list_price * (Decimal("1") - actual_discount) * multiplier
        result["current_annualized_revenue_rupees"] = _money(current_arr)
        result["new_annualized_revenue_rupees"] = _money(new_arr)
        result["expected_arr_uplift_rupees"] = _money(new_arr - current_arr)
        result["revenue_method"] = "seat_formula"
        result["list_price_per_seat_rupees"] = _money(list_price)
        result["billing_multiplier"] = int(multiplier)
        result["margin_retained_rupees"] = _money(
            (_band_ceiling(account, catalog) - actual_discount)
            * list_price
            * _decimal(proposed_seats)
            * multiplier
        )
        result["margin_contract_value_at_stake_rupees"] = _money(
            list_price * _decimal(proposed_seats) * multiplier
        )
        result["margin_method"] = "per_seat_formula"
        return result

    if offer_type == "addon" and target_addon in catalog.ADD_ONS:
        addon = catalog.ADD_ONS[target_addon]
        addon_price = _decimal(addon["price"])
        if addon.get("pricing") == "flat":
            quantity = Decimal("1")
            result["revenue_method"] = "flat_addon_per_account_billing_period"
        else:
            quantity = _decimal(proposed_seats, Decimal("1")) or Decimal("1")
            result["revenue_method"] = "per_seat_addon_formula"
        new_arr = addon_price * quantity * (Decimal("1") - actual_discount) * multiplier
        result["current_annualized_revenue_rupees"] = _money(_current_revenue(account))
        result["new_annualized_revenue_rupees"] = _money(new_arr)
        result["expected_arr_uplift_rupees"] = _money(new_arr)
        result["addon_list_price_rupees"] = _money(addon_price)
        result["addon_quantity"] = int(quantity)
        result["billing_multiplier"] = int(multiplier)
        if addon.get("pricing") == "per_seat":
            result["margin_retained_rupees"] = _money(
                (_band_ceiling(account, catalog) - actual_discount)
                * addon_price * quantity * multiplier
            )
            result["margin_contract_value_at_stake_rupees"] = _money(addon_price * quantity * multiplier)
            result["margin_method"] = "per_seat_formula"
        else:
            result["margin_excluded_reason"] = "flat add-on has no seat-based contract value"
        return result

    result["unresolved_reason"] = "unsupported or incomplete proposal terms"
    return result


def _distribution(values: list[Decimal]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min_rupees": None, "median_rupees": None, "max_rupees": None}
    return {
        "count": len(values),
        "min_rupees": _money(min(values)),
        "median_rupees": _money(Decimal(str(statistics.median(values)))),
        "max_rupees": _money(max(values)),
    }


def _uplift_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    values = [_decimal(r["expected_arr_uplift_rupees"]) for r in records if r.get("expected_arr_uplift_rupees") is not None]
    total = sum(values, Decimal("0"))
    return {
        "offers_with_reconstructed_terms": len(values),
        "expected_uplift_at_full_acceptance_rupees": _money(total),
        "expected_uplift_at_20_percent_acceptance_rupees": _money(total * Decimal("0.20")),
        "expected_uplift_at_40_percent_acceptance_rupees": _money(total * Decimal("0.40")),
        "mean_uplift_per_offer_rupees": _money(total / len(values)) if values else None,
        "distribution_rupees": _distribution(values),
    }


def _margin_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    discounts = [_decimal(r["proposed_discount"]) for r in records if r.get("proposed_discount") is not None]
    ceilings = [_decimal(r["discount_band_ceiling"]) for r in records if r.get("discount_band_ceiling") is not None]
    applicable = [r for r in records if r.get("margin_retained_rupees") is not None]
    retained = sum((_decimal(r["margin_retained_rupees"]) for r in applicable), Decimal("0"))
    stake = sum((_decimal(r.get("margin_contract_value_at_stake_rupees")) for r in applicable), Decimal("0"))
    return {
        "offers_with_discount_terms": len(discounts),
        "mean_actual_discount": str(sum(discounts, Decimal("0")) / len(discounts)) if discounts else None,
        "mean_band_ceiling_discount": str(sum(ceilings, Decimal("0")) / len(ceilings)) if ceilings else None,
        "applicable_per_seat_offer_count": len(applicable),
        "total_margin_retained_rupees": _money(retained),
        "total_contract_value_at_stake_rupees": _money(stake),
        "margin_retained_as_percent_of_contract_value": _money(retained / stake * 100) if stake else None,
        "framing": f"Policy gating plus a constrained model preserved {_money(retained):.2f} rupees of margin versus an unconstrained max-discount agent, for applicable per-seat offers.",
    }


def _telemetry(log_path: Path, audit_rows: list[sqlite3.Row], accounts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    log_rows: list[dict[str, Any]] = []
    if log_path.is_file():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                log_rows.append(value)

    api_calls_by_kind = Counter(str(row.get("kind", "unknown")) for row in log_rows)
    retries_by_kind = Counter(
        str(row.get("kind", "unknown"))
        for row in log_rows
        if int(row.get("attempt", 1) or 1) > 1
    )
    strategy_rows = sorted(
        [row for row in log_rows if row.get("kind") == "strategy" and _iso_datetime(row.get("timestamp"))],
        key=lambda row: _iso_datetime(row["timestamp"]),
    )
    campaign_clusters: list[list[dict[str, Any]]] = []
    for row in strategy_rows:
        when = _iso_datetime(row["timestamp"])
        if not campaign_clusters or (when - _iso_datetime(campaign_clusters[-1][-1]["timestamp"])).total_seconds() > 30:
            campaign_clusters.append([row])
        else:
            campaign_clusters[-1].append(row)

    proposer_audit = [
        row for row in audit_rows
        if row["event_type"] == "PROPOSAL_GENERATED" and row["actor"] == "proposer"
    ]
    triggered_count = sum(1 for account in accounts.values() if any(_trigger_info(account).values()))
    # A full sweep has no run_id in the historical audit schema.  State the
    # expected one-call-per-trigger count separately from observed telemetry.
    return {
        "response_log": str(log_path),
        "logged_response_rows": len(log_rows),
        "logged_api_calls_by_kind": dict(api_calls_by_kind),
        "logged_retry_rows_by_kind": dict(retries_by_kind),
        "full_campaign_runs_detected_from_strategy_timestamps": len(campaign_clusters),
        "campaign_calls_per_detected_run": [
            {
                "logged_strategy_responses": len(cluster),
                "logical_strategist_calls": 3,
                "retry_rows": sum(1 for row in cluster if int(row.get("attempt", 1) or 1) > 1),
            }
            for cluster in campaign_clusters
        ],
        "full_upsell_sweep": {
            "observed_api_call_count": None,
            "expected_one_call_per_triggered_account": triggered_count,
            "persisted_proposal_audit_rows_available": len(proposer_audit),
            "reason": "The historical response log and audit payloads do not carry a sweep identifier or request-start timestamps, so an exact full-sweep call count cannot be reconstructed.",
        },
        "mean_model_latency_ms": None,
        "latency_reason": "Existing Gemini response logs contain completion timestamps but no request-start or duration field; audit events also contain no latency.",
        "gemini_errors_observed_in_persisted_audit": 0,
        "schema_failures_observed_in_persisted_audit": 0,
        "cost": {
            "model": "gemini-3.5-flash-lite",
            "input_usd_per_million_tokens": _money(_rate(os.environ.get("GEMINI_INPUT_USD_PER_MILLION"), DEFAULT_INPUT_USD_PER_MILLION)),
            "output_usd_per_million_tokens": _money(_rate(os.environ.get("GEMINI_OUTPUT_USD_PER_MILLION"), DEFAULT_OUTPUT_USD_PER_MILLION)),
            "estimated_cost_per_full_run_usd": None,
            "reason": "No token usage is logged and full prompt text is not recoverable from model_input_hash values; exact input/output cost cannot be computed without inventing token counts.",
        },
    }


def build_report(db_path: Path = DB_PATH, log_path: Path | None = None) -> dict[str, Any]:
    catalog = _load_catalog()
    accounts = _load_accounts()
    log_path = log_path or (GROWTH_ROOT / "logs" / "gemini_responses.jsonl")
    conn = _read_only_connection(db_path)
    try:
        audit_rows = _audit_rows(conn)
        offers = _offer_rows(conn)
        stored_ids = _offer_stored_ids(audit_rows)
    finally:
        conn.close()

    proposals = _proposal_events(audit_rows)
    records = []
    for offer in offers:
        stored_seq = stored_ids.get(offer["offer_id"])
        proposal = _match_proposal(offer, proposals, stored_seq)
        record = _offer_metrics(offer, proposal, accounts, catalog, stored_seq)
        record["offer_stored_audit_present"] = stored_seq is not None
        records.append(record)

    reconstructed = [r for r in records if r["terms_reconstructed"]]
    upsell = [r for r in reconstructed if r["pipeline"] == "upsell"]
    campaign = [r for r in reconstructed if r["pipeline"] == "campaign"]
    triggered = [
        account for account in accounts.values()
        if any(_trigger_info(account).values())
    ]
    campaign_cohorts = _campaign_members(accounts, catalog)
    campaign_ids = sorted({customer_id for ids in campaign_cohorts.values() for customer_id in ids})
    total_book = sum((_current_revenue(account) for account in accounts.values()), Decimal("0"))
    triggered_book = sum((_current_revenue(account) for account in triggered), Decimal("0"))
    campaign_book = sum((_current_revenue(accounts[customer_id]) for customer_id in campaign_ids), Decimal("0"))

    return {
        "synthetic": True,
        "label": "All figures are synthetic and derived from the 50 synthetic accounts.",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "as_of_date": AS_OF_DATE.isoformat(),
        "sources": {
            "accounts": str(ACCOUNTS_PATH),
            "merchant_db": str(db_path),
            "audit_table": "audit_events",
            "offer_table": "merchant_offers",
            "gemini_response_log": str(log_path),
        },
        "offer_coverage": {
            "merchant_offers_rows": len(records),
            "offers_with_matching_proposal_terms": len(reconstructed),
            "offers_without_matching_proposal_terms": len(records) - len(reconstructed),
            "unresolved_offer_ids": [r["offer_id"] for r in records if not r["terms_reconstructed"]],
            "note": "The store is the complete offer inventory, but six legacy rows predate matching PROPOSAL_GENERATED audit records. They are retained in offer_records and excluded from term-dependent aggregates rather than guessed.",
        },
        "expected_arr_uplift": {
            "acceptance_note": "Expected uplift at full acceptance assumes 100% acceptance; 20% and 40% scenarios are shown separately and are not real-world forecasts.",
            "upsell": _uplift_summary(upsell),
            "campaign": _uplift_summary(campaign),
            "all_reconstructed_offers": _uplift_summary(reconstructed),
        },
        "margin_retained_vs_naive_max_discount": _margin_summary(reconstructed),
        "trigger_coverage": {
            "total_accounts": len(accounts),
            "triggered_upsell_accounts": len(triggered),
            "triggered_account_ids": [str(account["customer_id"]) for account in triggered],
            "total_book_arr_rupees": _money(total_book),
            "triggered_book_arr_rupees": _money(triggered_book),
            "triggered_book_arr_share_percent": _money(triggered_book / total_book * 100) if total_book else None,
            "campaign_eligible_member_count": len(campaign_ids),
            "campaign_eligible_member_ids": campaign_ids,
            "campaign_cohorts": campaign_cohorts,
            "campaign_eligible_book_arr_rupees": _money(campaign_book),
            "campaign_eligible_book_arr_share_percent": _money(campaign_book / total_book * 100) if total_book else None,
            "trigger_definition": "seat_ceiling >= 90% OR renewal anniversary/date within 30 days of 2026-08-29; accounts without renewal_date use the service's signup anniversary derivation.",
        },
        "cost_and_latency": _telemetry(log_path, audit_rows, accounts),
        "offer_records": records,
    }


def _print_summary(report: Mapping[str, Any], output_path: Path) -> None:
    uplift = report["expected_arr_uplift"]
    coverage = report["trigger_coverage"]
    margin = report["margin_retained_vs_naive_max_discount"]
    telemetry = report["cost_and_latency"]
    print("SYNTHETIC REVENUE METRICS (50 synthetic accounts; analysis only)")
    print("Metric | Value")
    print("--- | ---")
    print(f"Approved offer inventory | {report['offer_coverage']['merchant_offers_rows']} ({report['offer_coverage']['offers_with_matching_proposal_terms']} term-reconstructed)")
    print(f"Expected ARR uplift at 100% acceptance, upsell | ₹{uplift['upsell']['expected_uplift_at_full_acceptance_rupees']:,.2f}")
    print(f"Expected ARR uplift at 100% acceptance, campaign | ₹{uplift['campaign']['expected_uplift_at_full_acceptance_rupees']:,.2f}")
    print(f"Expected ARR uplift at 20% / 40% acceptance | ₹{uplift['all_reconstructed_offers']['expected_uplift_at_20_percent_acceptance_rupees']:,.2f} / ₹{uplift['all_reconstructed_offers']['expected_uplift_at_40_percent_acceptance_rupees']:,.2f}")
    print(f"Uplift distribution, reconstructed offers | min ₹{uplift['all_reconstructed_offers']['distribution_rupees']['min_rupees']:,.2f} / median ₹{uplift['all_reconstructed_offers']['distribution_rupees']['median_rupees']:,.2f} / max ₹{uplift['all_reconstructed_offers']['distribution_rupees']['max_rupees']:,.2f}")
    print(f"Mean actual / ceiling discount | {Decimal(margin['mean_actual_discount']):.2%} / {Decimal(margin['mean_band_ceiling_discount']):.2%}")
    print(f"Margin retained vs naive max-discount baseline | ₹{margin['total_margin_retained_rupees']:,.2f} ({margin['margin_retained_as_percent_of_contract_value']}% of applicable contract value)")
    print(f"Triggered upsell accounts | {coverage['triggered_upsell_accounts']}/{coverage['total_accounts']} ({coverage['triggered_book_arr_share_percent']}% of book ARR)")
    print(f"Campaign-eligible members | {coverage['campaign_eligible_member_count']} ({coverage['campaign_eligible_book_arr_share_percent']}% of book ARR)")
    print(f"Gemini logged response rows / retries | {telemetry['logged_response_rows']} / {sum(telemetry['logged_retry_rows_by_kind'].values())}")
    print(f"Full campaign logical calls per run | 3; detected runs: {telemetry['full_campaign_runs_detected_from_strategy_timestamps']}")
    print("Mean model latency | unavailable from existing logs")
    print(f"Estimated Gemini cost | unavailable: token usage is not logged (assumed rates ${telemetry['cost']['input_usd_per_million_tokens']:.2f}/M input, ${telemetry['cost']['output_usd_per_million_tokens']:.2f}/M output)")
    print(f"Terms unavailable | {report['offer_coverage']['offers_without_matching_proposal_terms']} offer rows; excluded from term-dependent totals")
    print(f"Report JSON | {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--log", type=Path, default=GROWTH_ROOT / "logs" / "gemini_responses.jsonl")
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    args = parser.parse_args()
    report = build_report(args.db, args.log)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _print_summary(report, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
