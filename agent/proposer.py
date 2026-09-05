"""Cached, single-call upsell proposal generation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from policy.checker import CheckResult, check_proposal
from policy.rules import DEFAULT_POLICY_CONFIG, CampaignState, PolicyConfig, Proposal

from .eligibility import AllowedOption, DiscountBand, EligibilityResult, filter_account
from .model_client import ModelResponseError
from .prompts import AUTHORITY_LEVELS, get_prompt


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_DIR = ROOT / "tests" / "fixtures"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for authority-level experiments and response caching."""

    authority_level: str = "options_narrowed_in_code"
    cache_dir: Path = DEFAULT_CACHE_DIR

    def __post_init__(self) -> None:
        if self.authority_level not in AUTHORITY_LEVELS:
            raise ValueError(f"unknown authority level: {self.authority_level!r}")
        object.__setattr__(self, "cache_dir", Path(self.cache_dir))


class ModelOutputError(ValueError):
    """The model did not return a proposal conforming to its contract."""


ModelClient = Callable[[str, Mapping[str, Any]], Any]
AuditWriter = Callable[[Mapping[str, Any]], None]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_jsonable(item) for item in value]
    return value


class ResponseCache:
    """Hash-keyed JSON cache; only a cache miss invokes the model client."""

    def __init__(self, cache_dir: Path = DEFAULT_CACHE_DIR) -> None:
        self.cache_dir = Path(cache_dir)

    def call(
        self,
        model: ModelClient | Any,
        prompt: str,
        payload: Mapping[str, Any],
    ) -> tuple[Any, bool, str]:
        cache_input = {"prompt": prompt, "payload": _jsonable(payload)}
        encoded = json.dumps(cache_input, sort_keys=True, separators=(",", ":")).encode()
        key = hashlib.sha256(encoded).hexdigest()
        path = self.cache_dir / f"response-{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["response"], True, key
        if model is None:
            raise RuntimeError("model client is required on a cache miss")
        if hasattr(model, "complete"):
            response = model.complete(prompt, payload)
        else:
            response = model(prompt, payload)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(
            {"response": _jsonable(response)}, sort_keys=True, indent=2
        ) + "\n"
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.cache_dir,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(serialized)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = temporary.name
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass
        return response, False, key


def _audit_record(proposal: Proposal, *, phase: str, verdict: str | None = None, result: CheckResult | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "phase": phase,
        "proposal": _jsonable(asdict(proposal)),
        "verdict": verdict,
    }
    if result is not None:
        record["fired_rules"] = [failure.rule_id for failure in result.fired_rules]
    return record


def _event_record(
    event_type: str,
    actor: str,
    subject_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "actor": actor,
        "subject_id": subject_id,
        "payload": payload,
    }


def _policy_event_payload(result: CheckResult) -> dict[str, Any]:
    return {
        "verdict": result.verdict,
        "rules_fired": [failure.rule_id for failure in result.fired_rules],
        "rule_reasons": {
            item.rule_id: item.reason
            for item in result.fired_rules
            if item.reason is not None
        },
    }


def _parse_discount(value: Any) -> Decimal:
    try:
        discount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ModelOutputError("model proposed a non-numeric discount") from exc
    if not discount.is_finite():
        raise ModelOutputError("model proposed a non-finite discount")
    return discount


def _option_by_id(options: tuple[AllowedOption, ...], option_id: Any) -> AllowedOption:
    for option in options:
        if option.option_id == option_id:
            return option
    raise ModelOutputError(f"model selected an option outside the allowed list: {option_id!r}")


def max_proposed_seats(account: Any) -> int:
    """Return the per-account ceiling for one upsell seat proposal."""

    contracted = int(_value(account, "seats_contracted", 0) or 0)
    growth_rate = Decimal(str(_value(account, "seat_growth_rate", 0) or 0))
    projected_growth = (
        Decimal(12) * growth_rate * Decimal(contracted)
    ).to_integral_value(rounding=ROUND_CEILING)
    return max(contracted * 2, contracted + int(projected_growth))


def _clamp_proposed_seats(
    proposal: Proposal,
    account: Any,
    *,
    on_discard: Callable[[Mapping[str, Any]], None] | None = None,
) -> Proposal:
    """Clamp only an oversized seat count and report the discarded value."""

    proposed_seats = proposal.proposed_seats
    if proposed_seats is None or isinstance(proposed_seats, bool) or not isinstance(proposed_seats, int):
        return proposal
    ceiling = max_proposed_seats(account)
    if proposed_seats <= ceiling:
        return proposal
    if on_discard is not None:
        on_discard(
            _event_record(
                "FIELD_DISCARDED",
                "proposer",
                proposal.customer_id,
                {
                    "field": "proposed_seats",
                    "value": proposed_seats,
                    "discarded_value": proposed_seats,
                    "replaced_with": ceiling,
                    "reason": "exceeded per-account seat ceiling",
                },
            )
        )
    return replace(proposal, proposed_seats=ceiling)


def _proposal_from_response(
    response: Any,
    account: Any,
    eligibility: EligibilityResult,
    authority_level: str,
) -> Proposal:
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ModelOutputError("model response was not valid JSON") from exc
    if not isinstance(response, Mapping):
        raise ModelOutputError("model response must be a JSON object")
    reason = response.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ModelOutputError("model response requires a non-empty reason")
    discount = _parse_discount(response.get("proposed_discount"))
    option: AllowedOption | None = None
    if authority_level == "options_narrowed_in_code":
        option = _option_by_id(eligibility.allowed_options, response.get("option_id"))
        if discount < eligibility.discount_band.minimum or discount > eligibility.discount_band.maximum:
            raise ModelOutputError("model discount is outside the code-computed discount band")
    elif response.get("option_id") is not None:
        try:
            option = _option_by_id(eligibility.allowed_options, response["option_id"])
        except ModelOutputError:
            # In the prompt-only/no-constraint experiments an invalid model
            # choice remains a proposal so the policy checker can measure it.
            option = None

    offer_type = option.offer_type if option else response.get("offer_type")
    target_tier = option.target_tier if option else response.get("target_tier")
    target_addon = option.target_addon if option else response.get("target_addon")
    if offer_type is None:
        raise ModelOutputError("model response requires offer_type or a valid option_id")
    proposed_seats = response.get("proposed_seats")
    if option and option.minimum_seats is not None and proposed_seats is None:
        proposed_seats = option.minimum_seats
    if offer_type == "addon" and not target_addon:
        raise ModelOutputError("add-on response requires target_addon")
    if offer_type in {"upgrade", "seat_expansion"}:
        if not target_tier:
            raise ModelOutputError("tier response requires target_tier")
        if proposed_seats is not None and (isinstance(proposed_seats, bool) or not isinstance(proposed_seats, int)):
            raise ModelOutputError("proposed_seats must be an integer")
    if offer_type not in {"upgrade", "addon", "seat_expansion"}:
        raise ModelOutputError(f"unsupported offer type: {offer_type!r}")
    return Proposal(
        customer_id=str(account["customer_id"] if isinstance(account, Mapping) else account.customer_id),
        offer_type=str(offer_type),
        target_tier=target_tier,
        target_addon=target_addon,
        proposed_discount=discount,
        campaign_id=response.get("campaign_id"),
        reason=reason,
        proposed_seats=proposed_seats,
    )


class Proposer:
    """One cached model call per eligible triggered account."""

    def __init__(
        self,
        model: ModelClient | Any,
        *,
        config: AgentConfig = AgentConfig(),
        audit_writer: AuditWriter | None = None,
    ) -> None:
        self.model = model
        self.config = config
        self.cache = ResponseCache(config.cache_dir)
        self.audit_writer = audit_writer
        self.audit_records: list[Mapping[str, Any]] = []
        self.model_failures: list[str] = []

    def _record_audit(self, record: Mapping[str, Any]) -> None:
        """Keep an in-memory append-only record and fan it out to the caller."""

        self.audit_records.append(dict(record))
        if self.audit_writer:
            self.audit_writer(record)

    def _emit_event(self, record: Mapping[str, Any]) -> None:
        if self.audit_writer:
            self.audit_writer(record)

    def _payload(
        self,
        account: Any,
        seat_trajectory: Any,
        support_tickets: Any,
        eligibility: EligibilityResult,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "account": _jsonable(account),
            "seat_trajectory": _jsonable(seat_trajectory if seat_trajectory is not None else _value(account, "invoices", [])),
            "support_tickets": _jsonable(support_tickets if support_tickets is not None else _value(account, "support_tickets", [])),
        }
        if self.config.authority_level != "no_constraints":
            payload["allowed_options"] = _jsonable(eligibility.allowed_options)
            payload["discount_band"] = _jsonable(eligibility.discount_band)
        if extra_payload:
            payload.update({str(key): _jsonable(value) for key, value in extra_payload.items()})
        payload["_response_kind"] = "proposal"
        return payload

    def propose(
        self,
        account: Any,
        *,
        seat_trajectory: Any = None,
        support_tickets: Any = None,
        now: Any = None,
        campaign_state: CampaignState | None = None,
        prompt_text: str | None = None,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> Proposal | None:
        eligibility = filter_account(
            account,
            config=_policy_config(campaign_state),
            now=now,
        )
        if not eligibility.eligible:
            return None
        payload = self._payload(account, seat_trajectory, support_tickets, eligibility, extra_payload)
        # Additive prompt override for new entry points; existing upsell calls
        # leave both arguments unset and retain the existing prompt path.
        prompt = prompt_text if prompt_text is not None else get_prompt(self.config.authority_level)
        try:
            response, _, _ = self.cache.call(self.model, prompt, payload)
        except ModelResponseError as exc:
            self.model_failures.append(str(exc))
            return None
        proposal = _proposal_from_response(response, account, eligibility, self.config.authority_level)
        proposal = _clamp_proposed_seats(
            proposal,
            account,
            on_discard=self._emit_event,
        )
        if proposal.campaign_id:
            LOGGER.warning(
                "Discarded non-empty campaign_id %r from upsell proposal for %s",
                proposal.campaign_id,
                proposal.customer_id,
            )
            self._emit_event(
                _event_record(
                    "FIELD_DISCARDED",
                    "proposer",
                    proposal.customer_id,
                    {
                        "field": "campaign_id",
                        "discarded_value": proposal.campaign_id,
                        "reason": "upsell proposals cannot carry a campaign_id",
                    },
                )
            )
        proposal = replace(proposal, campaign_id=None)
        self._emit_event(
            _event_record(
                "PROPOSAL_GENERATED",
                "proposer",
                proposal.customer_id,
                {
                    "model_input_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "raw_model_output": _jsonable(response),
                    "parsed_proposal_fields": _jsonable(asdict(proposal)),
                },
            )
        )
        self._record_audit(_audit_record(proposal, phase="proposal_before_adjudication"))
        return proposal

    def propose_and_check(
        self,
        account: Any,
        *,
        seat_trajectory: Any = None,
        support_tickets: Any = None,
        now: Any = None,
        campaign_state: Any = None,
    ) -> tuple[Proposal | None, CheckResult | None]:
        proposal = self.propose(
            account,
            seat_trajectory=seat_trajectory,
            support_tickets=support_tickets,
            now=now,
            campaign_state=campaign_state,
        )
        if proposal is None:
            return None, None
        result = check_proposal(proposal, account, campaign_state=campaign_state)
        self._emit_event(
            _event_record(
                "POLICY_VERDICT",
                "policy_engine",
                proposal.customer_id,
                _policy_event_payload(result),
            )
        )
        self._record_audit(_audit_record(proposal, phase="adjudicated", verdict=result.verdict, result=result))
        return proposal, result


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _policy_config(state: Any) -> PolicyConfig:
    configured = state.get("policy_config") if isinstance(state, Mapping) else getattr(state, "policy_config", None)
    return configured if isinstance(configured, PolicyConfig) else DEFAULT_POLICY_CONFIG


__all__ = [
    "AgentConfig",
    "DEFAULT_CACHE_DIR",
    "ModelOutputError",
    "max_proposed_seats",
    "Proposer",
    "ResponseCache",
]
