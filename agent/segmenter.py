"""Deterministic three-cohort campaign segmentation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping

from data import catalog


@dataclass(frozen=True)
class Cohort:
    cohort_id: str
    seat_band: str
    account_ids: tuple[str, ...]
    accounts: tuple[Any, ...]
    tiers: tuple[str, ...]
    average_seat_growth_rate: Decimal


def _value(account: Any, name: str, default: Any = None) -> Any:
    if isinstance(account, Mapping):
        return account.get(name, default)
    return getattr(account, name, default)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _seat_band(seats: int, small_max: int, medium_max: int) -> str:
    if seats <= small_max:
        return "small"
    if seats <= medium_max:
        return "medium"
    return "large"


def segment_accounts(
    accounts: Iterable[Any],
    *,
    small_max_seats: int = 20,
    medium_max_seats: int = 60,
    target_addon: str | None = None,
    audit_writer: Callable[[Mapping[str, Any]], None] | None = None,
) -> tuple[Cohort, Cohort, Cohort]:
    """Partition accounts into small, medium, and large seat cohorts.

    Tier and precomputed growth rate are part of the deterministic sort and
    cohort metadata, while seat size is the primary cut. No model is called.
    """

    if small_max_seats < 1 or medium_max_seats <= small_max_seats:
        raise ValueError("cohort seat boundaries must be increasing")
    if target_addon is not None and target_addon not in catalog.ADD_ONS:
        raise ValueError(f"unknown target add-on: {target_addon!r}")
    grouped: dict[str, list[Any]] = {"small": [], "medium": [], "large": []}
    for account in accounts:
        if target_addon is not None:
            tier = _value(account, "tier")
            active_addons = set(_value(account, "active_addons", ()) or ())
            if (
                tier not in catalog.TIERS
                or not catalog.addon_is_available(target_addon, tier)
                or target_addon in active_addons
            ):
                continue
        seats = int(_value(account, "seats_contracted", 0))
        grouped[_seat_band(seats, small_max_seats, medium_max_seats)].append(account)

    cohorts: list[Cohort] = []
    for band in ("small", "medium", "large"):
        members = sorted(
            grouped[band],
            key=lambda account: (
                _value(account, "tier", ""),
                _decimal(_value(account, "seat_growth_rate", 0)),
                str(_value(account, "customer_id", "")),
            ),
        )
        ids = tuple(str(_value(account, "customer_id", "")) for account in members)
        growth_total = sum((_decimal(_value(account, "seat_growth_rate", 0)) for account in members), Decimal("0"))
        average_growth = growth_total / len(members) if members else Decimal("0")
        cohorts.append(
            Cohort(
                cohort_id=f"seats_{band}",
                seat_band=band,
                account_ids=ids,
                accounts=tuple(members),
                tiers=tuple(sorted({str(_value(account, "tier", "")) for account in members})),
                average_seat_growth_rate=average_growth,
            )
        )
    result = tuple(cohorts)  # type: ignore[assignment]
    if audit_writer is not None:
        for cohort in result:
            audit_writer(
                {
                    "event_type": "COHORT_FORMED",
                    "actor": "system",
                    "subject_id": cohort.cohort_id,
                    "payload": {
                        "cohort_id": cohort.cohort_id,
                        "member_ids": list(cohort.account_ids),
                        "filter_criteria": {
                            "seat_band": cohort.seat_band,
                            "small_max_seats": small_max_seats,
                            "medium_max_seats": medium_max_seats,
                            "target_addon": target_addon,
                        },
                        "tiers": list(cohort.tiers),
                        "average_seat_growth_rate": str(cohort.average_seat_growth_rate),
                    },
                }
            )
    return result  # type: ignore[return-value]


segment = segment_accounts


__all__ = ["Cohort", "segment", "segment_accounts"]
