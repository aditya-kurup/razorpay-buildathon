"""Concurrency and deterministic-expiry evidence for the reservation layer."""

from __future__ import annotations

import json
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

try:
    from reservation.manager import ReservationManager
except ModuleNotFoundError:  # Direct execution: python tests/concurrency.py
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from reservation.manager import ReservationManager


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
BUDGET_PAISE = 5_000_000  # ₹50,000 from the specification.
DISCOUNT_VALUE_PAISE = 100_000  # ₹1,000 per offer: 50 offers fit exactly.
RUNS = 50
ATTEMPTS = 100
REPORT_PATH = Path(__file__).resolve().parent / "concurrency_report.md"


def run_budget_stress() -> dict[str, object]:
    successful_by_run: list[int] = []
    overdraw_count = 0
    double_redemption_count = 0
    for run_number in range(RUNS):
        manager = ReservationManager(clock=lambda: NOW)
        manager.register_campaign("campaign-50000", BUDGET_PAISE)
        barrier = Barrier(ATTEMPTS)

        def reserve_and_redeem(number: int):
            barrier.wait()
            approval = manager.reserve(
                "campaign-50000",
                f"customer-{run_number}-{number}",
                price_paise=600 * 100,
                discount_value_paise=DISCOUNT_VALUE_PAISE,
                offer_id=f"offer-{run_number}-{number}",
                now=NOW,
            )
            if not approval.approved or approval.offer is None:
                return None
            return manager.redeem(approval.offer.offer_id, NOW)

        with ThreadPoolExecutor(max_workers=ATTEMPTS) as pool:
            redemptions = list(pool.map(reserve_and_redeem, range(ATTEMPTS)))
        redemptions = [result for result in redemptions if result is not None]
        successful = sum(result.redeemed for result in redemptions)
        snapshot = manager.campaign_snapshot("campaign-50000", NOW)
        consumed = int(snapshot["consumed_paise"])
        redeemed_total = sum(
            result.offer.discount_value_paise
            for result in redemptions
            if result.redeemed and result.offer is not None
        )
        if int(snapshot["remaining_paise"]) < 0 or int(snapshot["held_paise"]) < 0:
            overdraw_count += 1
        ledger_total = int(snapshot["ledger_total_paise"])
        if redeemed_total != ledger_total or ledger_total != consumed or consumed > BUDGET_PAISE:
            overdraw_count += 1
        if sum(result.redeemed for result in redemptions) != len({result.offer.offer_id for result in redemptions if result.redeemed and result.offer}):
            double_redemption_count += 1
        successful_by_run.append(successful)
    return {
        "runs_completed": RUNS,
        "overdraw_count": overdraw_count,
        "double_redemption_count": double_redemption_count,
        "successful_offers_distribution": dict(sorted(Counter(successful_by_run).items())),
    }


class ReservationConcurrencyTests(unittest.TestCase):
    def test_100_simultaneous_redemptions_repeated_50_times(self) -> None:
        report = run_budget_stress()
        self.assertEqual(RUNS, report["runs_completed"])
        self.assertEqual(0, report["overdraw_count"])
        self.assertEqual(0, report["double_redemption_count"])
        self.assertEqual({50: RUNS}, report["successful_offers_distribution"])

    def test_same_offer_id_is_idempotent_under_concurrency(self) -> None:
        manager = ReservationManager(clock=lambda: NOW)
        manager.register_campaign("campaign", 100_000)
        approval = manager.reserve("campaign", "customer", 60_000, 100_000, offer_id="same", now=NOW)
        self.assertTrue(approval.approved)
        barrier = Barrier(ATTEMPTS)

        def redeem():
            barrier.wait()
            return manager.redeem("same", NOW)

        with ThreadPoolExecutor(max_workers=ATTEMPTS) as pool:
            results = list(pool.map(lambda _: redeem(), range(ATTEMPTS)))
        self.assertEqual(1, sum(result.redeemed for result in results))
        self.assertEqual(99, sum(result.reason == "already_redeemed" for result in results))
        snapshot = manager.campaign_snapshot("campaign", NOW)
        self.assertEqual(100_000, snapshot["consumed_paise"])
        self.assertEqual(0, snapshot["held_paise"])

    def test_redemption_at_and_after_expiry_returns_hold(self) -> None:
        manager = ReservationManager(clock=lambda: NOW)
        manager.register_campaign("campaign", 100_000)
        approval = manager.reserve("campaign", "customer", 60_000, 100_000, offer_id="expiring", now=NOW, ttl_seconds=600)
        self.assertTrue(approval.approved)
        self.assertEqual(0, manager.remaining_budget("campaign", NOW))
        result = manager.redeem("expiring", NOW + timedelta(seconds=600))
        self.assertFalse(result.redeemed)
        self.assertEqual("expired", result.reason)
        self.assertEqual(100_000, manager.remaining_budget("campaign", NOW + timedelta(seconds=600)))
        self.assertEqual(0, manager.campaign_snapshot("campaign", NOW + timedelta(seconds=600))["consumed_paise"])

    def test_redemption_racing_with_expiry_has_one_deterministic_outcome(self) -> None:
        manager = ReservationManager()
        manager.register_campaign("campaign", 100_000)
        approval = manager.reserve("campaign", "customer", 60_000, 100_000, offer_id="race", now=NOW, ttl_seconds=600)
        self.assertTrue(approval.approved)
        barrier = Barrier(2)

        def attempt(at: datetime):
            barrier.wait()
            return manager.redeem("race", at)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda at: attempt(at), (NOW, NOW + timedelta(seconds=600))))
        redeemed_count = sum(result.redeemed for result in results)
        expired_count = sum(result.reason == "expired" for result in results)
        already_redeemed_count = sum(result.reason == "already_redeemed" for result in results)
        self.assertIn(redeemed_count, {0, 1})
        self.assertGreaterEqual(expired_count, 1)
        self.assertEqual(2, redeemed_count + expired_count + already_redeemed_count)
        self.assertEqual(1 if redeemed_count else 0, already_redeemed_count)
        snapshot = manager.campaign_snapshot("campaign", NOW + timedelta(seconds=600))
        self.assertEqual(0, snapshot["held_paise"])
        self.assertIn(snapshot["consumed_paise"], {0, 100_000})


def write_report(report: dict[str, object]) -> None:
    distribution = report["successful_offers_distribution"]
    REPORT_PATH.write_text(
        "\n".join(
            [
                "# Reservation concurrency report",
                "",
                f"- Runs completed: **{report['runs_completed']}**",
                f"- Overdraw count: **{report['overdraw_count']}** (target zero)",
                f"- Double-redemption count: **{report['double_redemption_count']}** (target zero)",
                f"- Successful offers per run: `{json.dumps(distribution, sort_keys=True)}`",
                "",
                "The budget check and hold are protected by one manager-instance `RLock`, making the check-and-update indivisible for all shared in-process callers. Expiry is resolved synchronously under the same lock before reads and redemption, with no timer or sweeper thread.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    result = run_budget_stress()
    write_report(result)
    print(json.dumps(result, indent=2, sort_keys=True))
