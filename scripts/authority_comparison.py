#!/usr/bin/env python3
"""Run the three authority-level proposer variants against the 13 triggers.

This is an evaluation runner, not an offer-creation path. Approved proposals
are adjudicated and audited but are deliberately not inserted into
``merchant_offers``; otherwise the result would measure variant ordering and
R5 state carry-over instead of authority level.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SERVICE_ROOT = Path(__file__).resolve().parents[1]
GROWTH_ROOT = Path(
    __import__("os").environ.get(
        "GROWTH_AGENT_ROOT",
        str(SERVICE_ROOT),
    )
)
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))
if str(GROWTH_ROOT) not in sys.path:
    sys.path.insert(0, str(GROWTH_ROOT))

from agent.eligibility import filter_account  # noqa: E402
from agent.model_client import (  # noqa: E402
    GeminiModelClient,
    ModelClientConfig,
    ModelResponseError,
)
from agent.proposer import AgentConfig, ModelOutputError, Proposer  # noqa: E402
from merchant.api import (  # noqa: E402
    AS_OF_DATETIME,
    _expire_active_offers,
    _load_accounts,
    _reconcile_account,
    _trigger_info,
)
from merchant.audit_events import AuditEventLogger  # noqa: E402
from policy.checker import check_proposal  # noqa: E402
from policy.rules import CampaignState  # noqa: E402


VARIANTS = (
    "options_narrowed_in_code",
    "constraints_in_prompt",
    "no_constraints",
)
AS_OF_DATE = date(2026, 8, 29)
REPORT_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Counters:
    api_calls: int = 0
    cache_hits: int = 0
    retries: int = 0
    rate_limit_retries: int = 0


@dataclass
class VariantStats:
    name: str
    generated: int = 0
    approved: int = 0
    rejected: int = 0
    rule_fires: Counter[str] = field(default_factory=Counter)
    discounts: list[float] = field(default_factory=list)
    band_ceiling_hits: int = 0
    field_discards: Counter[str] = field(default_factory=Counter)
    schema_failures: Counter[str] = field(default_factory=Counter)
    gemini_errors: Counter[str] = field(default_factory=Counter)
    gemini_error_messages: list[str] = field(default_factory=list)
    eligibility_skips: Counter[str] = field(default_factory=Counter)
    counters: Counters = field(default_factory=Counters)


def _triggered(account: Mapping[str, Any]) -> bool:
    return any(_trigger_info(account).values())


def _jsonable(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_jsonable(item) for item in value]
    return value


def _log_lines(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return 0


def _error_bucket(message: str) -> str:
    lower = message.lower()
    if "429" in lower or "rate limit" in lower or "quota" in lower or "resource exhausted" in lower:
        return "rate_limit_or_quota"
    if "timeout" in lower or "timed out" in lower:
        return "timeout"
    if "json" in lower or "field" in lower or "discount" in lower or "option" in lower or "offer_type" in lower or "validation" in lower:
        return "schema"
    return "other"


def _new_model(counters: Counters, response_log: Path) -> GeminiModelClient:
    def sleep(seconds: float) -> None:
        counters.rate_limit_retries += 1
        time.sleep(seconds)

    config = ModelClientConfig(response_log_path=response_log)
    model = GeminiModelClient(config, sleep=sleep)
    # Keep the SDK Models object attached to its owning Client. Replacing
    # model.client with a proxy makes the SDK see a closed/detached client.
    models = model.client.models
    original_generate_content = models.generate_content

    def counted_generate_content(*args: Any, **kwargs: Any) -> Any:
        counters.api_calls += 1
        return original_generate_content(*args, **kwargs)

    models.generate_content = counted_generate_content
    return model


def _audit_writer(
    logger: AuditEventLogger,
    run_id: str,
    variant: str,
    record: Mapping[str, Any],
) -> None:
    event_type = record.get("event_type")
    if not event_type:
        return
    payload = dict(record.get("payload", {}))
    payload["run_id"] = run_id
    payload["variant"] = variant
    logger.append(
        timestamp=datetime.now(timezone.utc),
        event_type=str(event_type),
        actor=str(record["actor"]),
        subject_id=str(record["subject_id"]),
        payload=payload,
    )


def _append_policy_audit(
    logger: AuditEventLogger,
    run_id: str,
    variant: str,
    customer_id: str,
    result: Any,
) -> None:
    logger.append(
        timestamp=datetime.now(timezone.utc),
        event_type="POLICY_VERDICT",
        actor="policy_engine",
        subject_id=customer_id,
        payload={
            "run_id": run_id,
            "variant": variant,
            "verdict": result.verdict,
            "rules_fired": [item.rule_id for item in result.fired_rules],
            "rule_reasons": {
                item.rule_id: item.reason
                for item in result.fired_rules
                if item.reason is not None
            },
        },
    )


def _active_offer_invariant(account: Mapping[str, Any]) -> bool:
    """Independent R5 invariant check over the reconciled account shape."""
    for offer in account.get("offer_history", []) or []:
        if offer.get("redeemed") or offer.get("status") in {"expired", "consumed", "redeemed"}:
            continue
        expires = offer.get("expires_at")
        if not expires:
            continue
        parsed = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.astimezone(timezone.utc) > AS_OF_DATETIME:
            return True
    return False


def _classify_schema_failure(stats: VariantStats, message: str) -> None:
    stats.schema_failures[message] += 1


def _run_variant(
    accounts: list[dict[str, Any]],
    variant: str,
    run_id: str,
    logger: AuditEventLogger,
) -> VariantStats:
    stats = VariantStats(variant)
    log_path = Path(tempfile.mkstemp(prefix=f"authority-{variant}-", suffix=".jsonl")[1])
    cache_dir = Path(tempfile.mkdtemp(prefix=f"authority-cache-{variant}-"))
    model = _new_model(stats.counters, log_path)
    def write_audit(record: Mapping[str, Any]) -> None:
        if record.get("event_type") == "FIELD_DISCARDED":
            field_name = str(record.get("payload", {}).get("field", "unknown"))
            stats.field_discards[field_name] += 1
        _audit_writer(logger, run_id, variant, record)

    proposer = Proposer(
        model,
        config=AgentConfig(authority_level=variant, cache_dir=cache_dir),
        audit_writer=write_audit,
    )

    for original in accounts:
        account = _reconcile_account(original)
        customer_id = str(account["customer_id"])
        eligibility = filter_account(account, now=AS_OF_DATETIME)
        if not eligibility.eligible:
            stats.eligibility_skips[eligibility.reason or "ineligible"] += 1
            continue

        before_log = _log_lines(log_path)
        before_failures = len(proposer.model_failures)
        original_call = proposer.cache.call

        def tracked_call(*args: Any, _original_call=original_call, **kwargs: Any) -> Any:
            response, hit, key = _original_call(*args, **kwargs)
            if hit:
                stats.counters.cache_hits += 1
            return response, hit, key

        proposer.cache.call = tracked_call  # type: ignore[method-assign]
        try:
            proposal = proposer.propose(
                account,
                now=AS_OF_DATETIME,
                campaign_state=CampaignState(now=AS_OF_DATETIME),
            )
        except (ModelOutputError, ValueError) as exc:
            _classify_schema_failure(stats, str(exc))
            proposal = None
        except ModelResponseError:
            proposal = None
        except Exception as exc:  # provider/network errors must be reported
            message = f"{type(exc).__name__}: {exc}"
            stats.gemini_errors[_error_bucket(message)] += 1
            stats.gemini_error_messages.append(message)
            proposal = None
        finally:
            proposer.cache.call = original_call  # type: ignore[method-assign]

        after_log = _log_lines(log_path)
        attempts = max(0, after_log - before_log)
        if attempts > 1:
            stats.counters.retries += attempts - 1
        if len(proposer.model_failures) > before_failures:
            for message in proposer.model_failures[before_failures:]:
                _classify_schema_failure(stats, message)
        if proposal is None:
            continue

        stats.generated += 1
        try:
            result = check_proposal(proposal, account, campaign_state=CampaignState(now=AS_OF_DATETIME))
        except (TypeError, ValueError) as exc:
            _classify_schema_failure(stats, str(exc))
            continue
        _append_policy_audit(logger, run_id, variant, customer_id, result)
        if result.approved:
            stats.approved += 1
            discount = float(proposal.proposed_discount)
            stats.discounts.append(discount)
            if proposal.proposed_discount == eligibility.discount_band.maximum:
                stats.band_ceiling_hits += 1
            if _active_offer_invariant(account):
                raise RuntimeError(
                    f"FALSE_ALLOW {variant} {customer_id}: approved with an active offer"
                )
        else:
            stats.rejected += 1
            for failure in result.fired_rules:
                stats.rule_fires[failure.rule_id] += 1

    return stats


def _format_stats(stats: VariantStats) -> list[str]:
    if stats.discounts:
        minimum = min(stats.discounts)
        maximum = max(stats.discounts)
        mean = sum(stats.discounts) / len(stats.discounts)
        distribution = f"min={minimum:.2%}, max={maximum:.2%}, mean={mean:.2%}, at_band_ceiling={stats.band_ceiling_hits}"
    else:
        distribution = "min=n/a, max=n/a, mean=n/a, at_band_ceiling=0"
    rule_text = ", ".join(f"{rule}={stats.rule_fires.get(rule, 0)}" for rule in ("R1", "R2", "R3", "R4", "R5", "R6", "R7"))
    discards = ", ".join(f"{field}={count}" for field, count in sorted(stats.field_discards.items())) or "none"
    schemas = "; ".join(f"{message} ({count})" for message, count in stats.schema_failures.items()) or "none"
    errors = ", ".join(f"{kind}={count}" for kind, count in stats.gemini_errors.items()) or "none"
    error_details = "; ".join(stats.gemini_error_messages) or "none"
    return [
        f"{stats.name}: generated={stats.generated}, approved={stats.approved}, rejected={stats.rejected}",
        f"  rules: {rule_text}",
        f"  discounts: {distribution}",
        f"  field_discards: {discards}",
        f"  schema_failures: {schemas}",
        f"  gemini_errors: {errors}",
        f"  gemini_error_details: {error_details}",
        f"  api_calls={stats.counters.api_calls}, cache_hits={stats.counters.cache_hits}, retries={stats.counters.retries}, rate_limit_retries={stats.counters.rate_limit_retries}",
    ]


def main() -> int:
    run_id = str(uuid.uuid4())
    accounts = _load_accounts()
    triggered = [account for account in accounts if _triggered(account)]
    print(f"run_id: {run_id}")
    print(f"accounts loaded: {len(accounts)}")
    print(f"triggered accounts ({len(triggered)}): {', '.join(str(a['customer_id']) for a in triggered)}")
    print("approved proposals are not persisted as offers during this comparison")

    print("expiring existing active reservations:")
    expired_total = 0
    for account in triggered:
        expired = _expire_active_offers(str(account["customer_id"]))
        for offer in expired:
            expired_total += 1
            print(f"  expired {offer.offer_id} for {offer.customer_id} at {offer.expires_at.isoformat()}")
    print(f"expired reservations: {expired_total}")

    logger = AuditEventLogger()
    all_stats: list[VariantStats] = []
    try:
        for variant in VARIANTS:
            print(f"running variant: {variant}")
            all_stats.append(_run_variant(triggered, variant, run_id, logger))
    except RuntimeError as exc:
        if str(exc).startswith("FALSE_ALLOW"):
            print(str(exc))
            print("stopped on false allow")
            return 2
        raise

    print("\nPER-VARIANT RESULTS")
    for stats in all_stats:
        print("\n".join(_format_stats(stats)))
    print("\nREADME SUMMARY")
    print("| variant | generated | approved | rejected | containment events |")
    print("|---|---:|---:|---:|---:|")
    for stats in all_stats:
        discards = sum(stats.field_discards.values())
        print(f"| {stats.name} | {stats.generated} | {stats.approved} | {stats.rejected} | {discards} |")
    print("\nfalse_allows: 0")
    print("offers written by comparison: 0")
    print("schema failures and Gemini errors are reported above; no proposal was silently dropped")
    from merchant.audit_events import verify_audit_chain

    print(f"audit_chain: {verify_audit_chain()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
