"""Cached cohort strategy generation and deterministic proposal expansion."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Mapping

from policy.checker import CheckResult, check_proposal
from policy.rules import CampaignState, Proposal

from .eligibility import AllowedOption, DiscountBand, EligibilityResult, filter_account
from .proposer import (
    AgentConfig,
    AuditWriter,
    ModelClient,
    ModelOutputError,
    ResponseCache,
    _audit_record,
    _clamp_proposed_seats,
    _event_record,
    _jsonable,
    _option_by_id,
    _parse_discount,
    _policy_event_payload,
    _policy_config,
)
from .model_client import ModelResponseError
from .prompts import get_prompt
from .segmenter import Cohort


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CohortStrategy:
    cohort_id: str
    approach: str
    discount_band: DiscountBand
    option_id: str | None = None
    campaign_id: str | None = None
    offer_type: str | None = None
    target_tier: str | None = None
    target_addon: str | None = None
    proposed_seats: int | None = None
    model_input_hash: str | None = None
    raw_model_output: Any = None


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _parse_response(response: Any) -> Mapping[str, Any]:
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ModelOutputError("strategy response was not valid JSON") from exc
    if not isinstance(response, Mapping):
        raise ModelOutputError("strategy response must be a JSON object")
    approach = response.get("approach")
    if not isinstance(approach, str) or not approach.strip():
        raise ModelOutputError("strategy response requires a non-empty approach")
    return response


class Strategist:
    """One model call per cohort, followed by per-account expansion."""

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
        self.audit_records.append(dict(record))
        if self.audit_writer:
            self.audit_writer(record)

    def _emit_event(self, record: Mapping[str, Any]) -> None:
        if self.audit_writer:
            self.audit_writer(record)

    def strategize(
        self,
        cohort: Cohort,
        *,
        campaign_id: str | None = None,
        target_addon: str | None = None,
        now: Any = None,
        campaign_state: CampaignState | None = None,
    ) -> CohortStrategy:
        brief_target_addon = target_addon
        eligibilities = {
            str(_value(account, "customer_id")): filter_account(account, config=_policy_config(campaign_state), now=now)
            for account in cohort.accounts
        }
        payload = {
            "cohort": _jsonable(cohort),
            "campaign_id": campaign_id,
            "_response_kind": "strategy",
        }
        if self.config.authority_level != "no_constraints":
            if target_addon is None:
                allowed_options_by_account = {
                    customer_id: _jsonable(result.allowed_options)
                    for customer_id, result in eligibilities.items()
                }
            else:
                target_option = AllowedOption(
                    option_id=f"addon:{target_addon}",
                    offer_type="addon",
                    target_addon=target_addon,
                )
                allowed_options_by_account = {
                    customer_id: _jsonable(
                        (target_option,)
                        if result.eligible
                        and any(
                            option.option_id == target_option.option_id
                            for option in result.allowed_options
                        )
                        else ()
                    )
                    for customer_id, result in eligibilities.items()
                }
            payload["eligible_options_by_account"] = {
                customer_id: options
                for customer_id, options in allowed_options_by_account.items()
            }
            payload["group_discount_band"] = _jsonable(_group_band(tuple(eligibilities.values())))
        prompt = get_prompt(self.config.authority_level) + (
            "\nFor this campaign cohort, return JSON with `approach`, either "
            "`option_id` or direct offer fields (`offer_type`, `target_tier`, "
            "and `target_addon`), plus `discount_min` and `discount_max`."
        )
        try:
            response, _, _ = self.cache.call(self.model, prompt, payload)
        except ModelResponseError as exc:
            self.model_failures.append(str(exc))
            return None
        raw_response = _jsonable(response)
        response = _parse_response(response)
        if brief_target_addon is not None:
            returned_product = {
                field: response.get(field)
                for field in ("option_id", "offer_type", "target_tier", "target_addon")
                if field in response and response.get(field) not in (None, "")
            }
            expected_option_id = f"addon:{brief_target_addon}"
            if (
                returned_product.get("option_id") not in (None, expected_option_id)
                or returned_product.get("offer_type") not in (None, "addon")
                or returned_product.get("target_tier") is not None
                or (
                    returned_product.get("target_addon") is not None
                    and returned_product.get("target_addon") != brief_target_addon
                )
            ):
                LOGGER.warning(
                    "Discarded campaign product selection from cohort %s: "
                    "returned=%s, brief_target_addon=%r",
                    cohort.cohort_id,
                    returned_product,
                    brief_target_addon,
                )
                for field, value in returned_product.items():
                    expected = {
                        "option_id": expected_option_id,
                        "offer_type": "addon",
                        "target_tier": None,
                        "target_addon": brief_target_addon,
                    }[field]
                    if value != expected:
                        self._emit_event(
                            _event_record(
                                "FIELD_DISCARDED",
                                "strategist",
                                campaign_id or cohort.cohort_id,
                                {
                                    "field": field,
                                    "discarded_value": value,
                                    "reason": "campaign brief owns target product",
                                },
                            )
                        )
            if response.get("campaign_id") not in (None, ""):
                LOGGER.warning(
                    "Discarded model campaign_id %r from cohort %s; "
                    "campaign brief supplies %r",
                    response["campaign_id"],
                    cohort.cohort_id,
                    campaign_id,
                )
                self._emit_event(
                    _event_record(
                        "FIELD_DISCARDED",
                        "strategist",
                        campaign_id or cohort.cohort_id,
                        {
                            "field": "campaign_id",
                            "discarded_value": response["campaign_id"],
                            "reason": "campaign brief supplies campaign_id",
                        },
                    )
                )
        group_band = _group_band(tuple(eligibilities.values()))
        minimum = _parse_discount(response.get("discount_min", response.get("proposed_discount", group_band.minimum)))
        maximum = _parse_discount(response.get("discount_max", response.get("proposed_discount", group_band.maximum)))
        if self.config.authority_level == "options_narrowed_in_code":
            if minimum < group_band.minimum or maximum > group_band.maximum or maximum < minimum:
                raise ModelOutputError("strategy discount band is outside the code-computed group band")
        else:
            if maximum < minimum:
                raise ModelOutputError("strategy discount band is inverted")
        option_id = response.get("option_id")
        if option_id is not None:
            all_options = tuple(
                option
                for result in eligibilities.values()
                for option in result.allowed_options
            )
            if (
                self.config.authority_level == "options_narrowed_in_code"
                and brief_target_addon is None
            ):
                _option_by_id(all_options, option_id)
        offer_type = response.get("offer_type")
        target_tier = response.get("target_tier")
        returned_target_addon = response.get("target_addon")
        proposed_seats = response.get("proposed_seats")
        if brief_target_addon is None and option_id is None and offer_type is None:
            raise ModelOutputError("strategy requires option_id or offer_type")
        if brief_target_addon is None and offer_type == "addon" and not returned_target_addon:
            raise ModelOutputError("add-on strategy requires target_addon")
        if brief_target_addon is None and offer_type in {"upgrade", "seat_expansion"} and not target_tier:
            raise ModelOutputError("tier strategy requires target_tier")
        if (
            brief_target_addon is None
            and offer_type is not None
            and offer_type not in {"upgrade", "addon", "seat_expansion"}
        ):
            raise ModelOutputError(f"unsupported strategy offer type: {offer_type!r}")
        if (
            brief_target_addon is None
            and option_id is None
            and self.config.authority_level == "options_narrowed_in_code"
        ):
            direct_match = any(
                _option_matches(
                    option,
                    offer_type=offer_type,
                    target_tier=target_tier,
                    target_addon=returned_target_addon,
                )
                for result in eligibilities.values()
                for option in result.allowed_options
            )
            if not direct_match:
                raise ModelOutputError(
                    "strategy target is outside every account's allowed options"
                )
        if brief_target_addon is not None:
            target_option_id = f"addon:{brief_target_addon}"
            if self.config.authority_level == "options_narrowed_in_code":
                eligible_target_members = any(
                    any(
                        option.option_id == target_option_id
                        for option in result.allowed_options
                    )
                    for result in eligibilities.values()
                    if result.eligible
                )
                if not eligible_target_members:
                    raise ModelOutputError(
                        "campaign target add-on is not available to any eligible account"
                    )
            # Product selection belongs to the campaign brief. Preserve the
            # model's discount band and approach, but replace every product
            # field before expansion.
            option_id = target_option_id
            offer_type = "addon"
            target_tier = None
            target_addon = brief_target_addon
            proposed_seats = None
        strategy = CohortStrategy(
            cohort_id=cohort.cohort_id,
            approach=str(response["approach"]),
            discount_band=DiscountBand(minimum, maximum),
            option_id=str(option_id) if option_id is not None else None,
            campaign_id=campaign_id,
            offer_type=str(offer_type) if offer_type is not None else None,
            target_tier=str(target_tier) if target_tier is not None else None,
            target_addon=str(target_addon) if target_addon is not None else None,
            proposed_seats=proposed_seats,
            model_input_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            raw_model_output=raw_response,
        )
        self._emit_event(
            _event_record(
                "STRATEGY_GENERATED",
                "strategist",
                campaign_id or cohort.cohort_id,
                {
                    "model_input_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "raw_model_output": raw_response,
                    "parsed_strategy": _jsonable(asdict(strategy)),
                },
            )
        )
        return strategy

    def expand(
        self,
        strategy: CohortStrategy,
        cohort: Cohort,
        *,
        target_addon: str | None = None,
        now: Any = None,
        campaign_state: CampaignState | None = None,
    ) -> tuple[Proposal, ...]:
        proposals: list[Proposal] = []
        for account in cohort.accounts:
            eligibility = filter_account(
                account,
                config=_policy_config(campaign_state),
                now=now,
            )
            if not eligibility.eligible and eligibility.reason == "customer already holds an active offer":
                continue
            if not eligibility.eligible:
                continue
            if strategy.option_id is None and strategy.offer_type is not None:
                option = next(
                    (
                        candidate
                        for candidate in eligibility.allowed_options
                        if _option_matches(
                            candidate,
                            offer_type=strategy.offer_type,
                            target_tier=strategy.target_tier,
                            target_addon=strategy.target_addon,
                        )
                    ),
                    None,
                )
                if self.config.authority_level != "options_narrowed_in_code":
                    option = None
            elif strategy.option_id is None:
                continue
            else:
                try:
                    option = _option_by_id(eligibility.allowed_options, strategy.option_id)
                except ModelOutputError:
                    if self.config.authority_level == "options_narrowed_in_code":
                        continue
                    option = None
            if option is None and strategy.offer_type is None:
                continue
            if (
                option is None
                and self.config.authority_level == "options_narrowed_in_code"
            ):
                continue
            if target_addon is not None:
                target_option_id = f"addon:{target_addon}"
                target_option = next(
                    (
                        candidate
                        for candidate in eligibility.allowed_options
                        if candidate.option_id == target_option_id
                    ),
                    None,
                )
                if target_option is None:
                    continue
                option = target_option
            discount = (
                min(strategy.discount_band.maximum, eligibility.discount_band.maximum)
                if self.config.authority_level == "options_narrowed_in_code"
                else strategy.discount_band.maximum
            )
            if option is None:
                # Loose experiments deliberately preserve the model's direct
                # fields, including illegal combinations for checker metrics.
                offer_type = strategy.offer_type
                target_tier = strategy.target_tier
                target_addon = strategy.target_addon
                proposed_seats = strategy.proposed_seats
            else:
                offer_type = option.offer_type
                target_tier = option.target_tier
                target_addon = option.target_addon
                proposed_seats = option.minimum_seats
            proposal = Proposal(
                customer_id=str(_value(account, "customer_id")),
                offer_type=str(offer_type),
                target_tier=target_tier,
                target_addon=target_addon,
                proposed_discount=discount,
                campaign_id=strategy.campaign_id,
                reason=strategy.approach,
                proposed_seats=proposed_seats,
            )
            proposal = _clamp_proposed_seats(
                proposal,
                account,
                on_discard=self._emit_event,
            )
            proposals.append(proposal)
            self._record_audit(_audit_record(proposal, phase="proposal_before_adjudication"))
            self._emit_event(
                _event_record(
                    "PROPOSAL_GENERATED",
                    "strategist",
                    proposal.customer_id,
                    {
                        "model_input_hash": strategy.model_input_hash,
                        "raw_model_output": strategy.raw_model_output,
                        "parsed_proposal_fields": _jsonable(asdict(proposal)),
                        "source": "cohort_strategy_expansion",
                    },
                )
            )
        return tuple(proposals)

    def strategize_and_check(
        self,
        cohort: Cohort,
        *,
        campaign_id: str | None = None,
        target_addon: str | None = None,
        now: Any = None,
        campaign_state: Any = None,
    ) -> tuple[CohortStrategy | None, tuple[tuple[Proposal, CheckResult], ...]]:
        strategy = self.strategize(
            cohort,
            campaign_id=campaign_id,
            target_addon=target_addon,
            now=now,
            campaign_state=campaign_state,
        )
        checked: list[tuple[Proposal, CheckResult]] = []
        if strategy is None:
            return None, ()
        for proposal in self.expand(
            strategy,
            cohort,
            target_addon=target_addon,
            now=now,
            campaign_state=campaign_state,
        ):
            result = check_proposal(proposal, _account_for(cohort, proposal.customer_id), campaign_state=campaign_state)
            self._emit_event(
                _event_record(
                    "POLICY_VERDICT",
                    "policy_engine",
                    proposal.customer_id,
                    _policy_event_payload(result),
                )
            )
            self._record_audit(_audit_record(proposal, phase="adjudicated", verdict=result.verdict, result=result))
            checked.append((proposal, result))
        return strategy, tuple(checked)


def _group_band(eligibilities: tuple[EligibilityResult, ...]) -> DiscountBand:
    eligible = [result.discount_band for result in eligibilities if result.eligible]
    if not eligible:
        return DiscountBand()
    return DiscountBand(
        minimum=max(band.minimum for band in eligible),
        maximum=min(band.maximum for band in eligible),
    )


def _option_matches(
    option: AllowedOption,
    *,
    offer_type: str | None,
    target_tier: str | None,
    target_addon: str | None,
) -> bool:
    return (
        option.offer_type == offer_type
        and option.target_tier == target_tier
        and option.target_addon == target_addon
    )


def _account_for(cohort: Cohort, customer_id: str) -> Any:
    for account in cohort.accounts:
        if str(_value(account, "customer_id")) == customer_id:
            return account
    raise KeyError(f"customer {customer_id!r} is not in cohort {cohort.cohort_id!r}")


__all__ = ["CohortStrategy", "Strategist"]
