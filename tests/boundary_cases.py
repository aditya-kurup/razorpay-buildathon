"""Independent hand-written boundary cases for R1--R7.

Thresholds and expected verdicts are literal, spec-derived test data. They do
not read thresholds from ``catalog.py`` or ``DEFAULT_POLICY_CONFIG``.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from data import catalog
from policy.rules import (
    CampaignState,
    PolicyConfig,
    Proposal,
    r1_margin_floor,
    r2_tier_discount_ceiling,
    r3_addon_tier_gating,
    r4_no_stacking,
    r5_one_active_offer,
    r6_campaign_budget,
    r7_upgrade_direction,
)


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def make_account(
    *, tier="pro", billing_cycle="monthly", standing_discount=Decimal("0"),
    existing_discount=Decimal("0"), seats_in_use=10, seats_contracted=10,
    offer_history=None,
):
    return {
        "customer_id": "hand-account", "tier": tier,
        "billing_cycle": billing_cycle, "standing_discount": standing_discount,
        "existing_discount": existing_discount, "seats_in_use": seats_in_use,
        "seats_contracted": seats_contracted, "offer_history": offer_history or [],
    }


def make_proposal(
    *, offer_type="upgrade", target_tier="enterprise", target_addon=None,
    discount=Decimal("0"), proposed_seats=11, campaign_id=None,
):
    return Proposal(
        customer_id="hand-account", offer_type=offer_type,
        target_tier=target_tier, target_addon=target_addon,
        proposed_discount=discount, proposed_seats=proposed_seats,
        campaign_id=campaign_id, reason="Independent hand-written fixture.",
    )


def run(rule, proposal, account, state=None):
    return rule(proposal, account, catalog, state or CampaignState(now=NOW))


def active_offer(expires_at):
    return {
        "offer_id": "hand-offer", "created_at": NOW.isoformat(),
        "expires_at": expires_at.isoformat(), "redeemed": False, "status": "active",
    }


class HandWrittenBoundaryCases(unittest.TestCase):
    # R1: Pro is ₹600 list price, ₹180 serving cost; ₹400 gives exactly the
    # specification's 55% margin floor: (400 - 180) / 400. The prices and
    # 55% floor are literals here, independent of catalog.py.
    def test_r1_one_paise_above_floor(self):
        price_paise = Decimal("40000") + Decimal("1")  # ₹400 + one paise
        discount = Decimal("1") - price_paise / Decimal("60000")  # Pro list ₹600
        self.assertTrue(run(r1_margin_floor, make_proposal(target_tier="pro", discount=discount), make_account()).passed)

    def test_r1_exactly_55_percent_floor(self):
        price_paise = Decimal("40000")  # ₹400; 55% = (400 - 180) / 400
        discount = Decimal("1") - price_paise / Decimal("60000")
        self.assertTrue(run(r1_margin_floor, make_proposal(target_tier="pro", discount=discount), make_account()).passed)

    def test_r1_one_paise_below_floor(self):
        price_paise = Decimal("40000") - Decimal("1")  # ₹400 - one paise
        discount = Decimal("1") - price_paise / Decimal("60000")
        self.assertFalse(run(r1_margin_floor, make_proposal(target_tier="pro", discount=discount), make_account()).passed)

    # R1: Starter is ₹250 list price and ₹85 serving cost (section 2). The
    # first whole paise at or above the 55% floor is ₹188.89; ₹188.88 is one
    # paise below that boundary.
    def test_r1_starter_monthly_at_floor_boundary(self):
        price_paise = Decimal("18889")  # ₹188.89; (188.89 - 85) / 188.89 >= 55%
        discount = Decimal("1") - price_paise / Decimal("25000")  # Starter list ₹250
        self.assertTrue(run(r1_margin_floor, make_proposal(target_tier="starter", discount=discount), make_account(tier="starter")).passed)

    def test_r1_starter_monthly_one_paise_below_floor(self):
        price_paise = Decimal("18888")  # ₹188.88; one paise below Starter's 55% floor boundary
        discount = Decimal("1") - price_paise / Decimal("25000")
        self.assertFalse(run(r1_margin_floor, make_proposal(target_tier="starter", discount=discount), make_account(tier="starter")).passed)

    # R1: Enterprise is ₹1,200 list price and ₹320 serving cost (section 2).
    # The first whole paise at or above its 55% floor is ₹711.12.
    def test_r1_enterprise_monthly_at_floor_boundary(self):
        price_paise = Decimal("71112")  # ₹711.12; first whole paise at the 55% floor
        discount = Decimal("1") - price_paise / Decimal("120000")  # Enterprise list ₹1,200
        self.assertTrue(run(r1_margin_floor, make_proposal(target_tier="enterprise", discount=discount), make_account(tier="enterprise")).passed)

    def test_r1_enterprise_monthly_one_paise_below_floor(self):
        price_paise = Decimal("71111")  # ₹711.11; one paise below Enterprise's floor boundary
        discount = Decimal("1") - price_paise / Decimal("120000")
        self.assertFalse(run(r1_margin_floor, make_proposal(target_tier="enterprise", discount=discount), make_account(tier="enterprise")).passed)

    # R1: Pro annual starts at ₹480 per seat after the spec's 20% annual
    # discount; ₹400 is exactly the 55% floor price for its ₹180 cost.
    def test_r1_pro_annual_at_floor_boundary(self):
        price_paise = Decimal("40000")  # ₹400; annual Pro floor price at 55%
        discount = Decimal("1") - price_paise / Decimal("48000")  # ₹600 × (1 - 20%)
        self.assertTrue(run(r1_margin_floor, make_proposal(target_tier="pro", discount=discount), make_account(tier="pro", billing_cycle="annual", standing_discount=Decimal("0.20"))).passed)

    def test_r1_pro_annual_one_paise_below_floor(self):
        price_paise = Decimal("39999")  # ₹399.99; one paise below Pro's annual floor price
        discount = Decimal("1") - price_paise / Decimal("48000")
        self.assertFalse(run(r1_margin_floor, make_proposal(target_tier="pro", discount=discount), make_account(tier="pro", billing_cycle="annual", standing_discount=Decimal("0.20"))).passed)

    # R2: Pro's per-offer discount ceiling is 15% (section 2).
    def test_r2_one_point_under_15_percent(self):
        self.assertTrue(run(r2_tier_discount_ceiling, make_proposal(target_tier="pro", discount=Decimal("0.14")), make_account()).passed)  # 14% < spec's 15%

    def test_r2_exactly_15_percent(self):
        self.assertTrue(run(r2_tier_discount_ceiling, make_proposal(target_tier="pro", discount=Decimal("0.15")), make_account()).passed)  # spec's 15%

    def test_r2_one_point_over_15_percent(self):
        self.assertFalse(run(r2_tier_discount_ceiling, make_proposal(target_tier="pro", discount=Decimal("0.16")), make_account()).passed)  # 16% > spec's 15%

    # R2: Starter's per-offer ceiling is 10% (section 2).
    def test_r2_starter_one_point_under_10_percent(self):
        self.assertTrue(run(r2_tier_discount_ceiling, make_proposal(target_tier="starter", discount=Decimal("0.09")), make_account(tier="starter")).passed)

    def test_r2_starter_exactly_10_percent(self):
        self.assertTrue(run(r2_tier_discount_ceiling, make_proposal(target_tier="starter", discount=Decimal("0.10")), make_account(tier="starter")).passed)

    def test_r2_starter_one_point_over_10_percent(self):
        self.assertFalse(run(r2_tier_discount_ceiling, make_proposal(target_tier="starter", discount=Decimal("0.11")), make_account(tier="starter")).passed)

    # R2: Enterprise's per-offer discount ceiling is 20% (section 2).
    def test_r2_enterprise_one_point_under_20_percent(self):
        self.assertTrue(run(r2_tier_discount_ceiling, make_proposal(target_tier="enterprise", discount=Decimal("0.19")), make_account(tier="enterprise")).passed)  # 19% < spec's 20%

    def test_r2_enterprise_exactly_20_percent(self):
        self.assertTrue(run(r2_tier_discount_ceiling, make_proposal(target_tier="enterprise", discount=Decimal("0.20")), make_account(tier="enterprise")).passed)  # spec's 20%

    def test_r2_enterprise_one_point_over_20_percent(self):
        self.assertFalse(run(r2_tier_discount_ceiling, make_proposal(target_tier="enterprise", discount=Decimal("0.21")), make_account(tier="enterprise")).passed)  # 21% > spec's 20%

    # R3: compliance export is Enterprise-only; history is Pro/Enterprise.
    def test_r3_compliance_on_enterprise(self):
        self.assertTrue(run(r3_addon_tier_gating, make_proposal(offer_type="addon", target_tier=None, target_addon="compliance_export_audit_log", proposed_seats=None), make_account(tier="enterprise")).passed)

    def test_r3_compliance_on_pro(self):
        self.assertFalse(run(r3_addon_tier_gating, make_proposal(offer_type="addon", target_tier=None, target_addon="compliance_export_audit_log", proposed_seats=None), make_account(tier="pro")).passed)

    def test_r3_compliance_on_starter(self):
        self.assertFalse(run(r3_addon_tier_gating, make_proposal(offer_type="addon", target_tier=None, target_addon="compliance_export_audit_log", proposed_seats=None), make_account(tier="starter")).passed)

    def test_r3_history_on_enterprise(self):
        self.assertTrue(run(r3_addon_tier_gating, make_proposal(offer_type="addon", target_tier=None, target_addon="unlimited_message_history", proposed_seats=None), make_account(tier="enterprise")).passed)

    def test_r3_history_on_pro(self):
        self.assertTrue(run(r3_addon_tier_gating, make_proposal(offer_type="addon", target_tier=None, target_addon="unlimited_message_history", proposed_seats=None), make_account(tier="pro")).passed)

    def test_r3_history_on_starter(self):
        self.assertFalse(run(r3_addon_tier_gating, make_proposal(offer_type="addon", target_tier=None, target_addon="unlimited_message_history", proposed_seats=None), make_account(tier="starter")).passed)

    # R4: annual billing has the spec's 20% standing discount; the current
    # provisional Pro combined-authority ceiling is the literal 30%. Inclusive.
    def test_r4_29_percent_combined(self):
        self.assertTrue(run(r4_no_stacking, make_proposal(target_tier="pro", discount=Decimal("0.09")), make_account(tier="pro", billing_cycle="annual", standing_discount=Decimal("0.20")), CampaignState(now=NOW)).passed)  # 20% + 9% = 29% < 30%

    def test_r4_exactly_30_percent_combined(self):
        self.assertTrue(run(r4_no_stacking, make_proposal(target_tier="pro", discount=Decimal("0.10")), make_account(tier="pro", billing_cycle="annual", standing_discount=Decimal("0.20")), CampaignState(now=NOW)).passed)  # 20% + 10% = 30%

    def test_r4_31_percent_combined(self):
        self.assertFalse(run(r4_no_stacking, make_proposal(target_tier="pro", discount=Decimal("0.11")), make_account(tier="pro", billing_cycle="annual", standing_discount=Decimal("0.20")), CampaignState(now=NOW)).passed)  # 20% + 11% = 31% > 30%

    # R4: annual Starter's current provisional combined-authority ceiling is
    # the literal 25%; standing annual discount remains the spec's 20%.
    def test_r4_starter_24_percent_combined(self):
        self.assertTrue(run(r4_no_stacking, make_proposal(target_tier="starter", discount=Decimal("0.04")), make_account(tier="starter", billing_cycle="annual", standing_discount=Decimal("0.20")), CampaignState(now=NOW)).passed)

    def test_r4_starter_exactly_25_percent_combined(self):
        self.assertTrue(run(r4_no_stacking, make_proposal(target_tier="starter", discount=Decimal("0.05")), make_account(tier="starter", billing_cycle="annual", standing_discount=Decimal("0.20")), CampaignState(now=NOW)).passed)

    def test_r4_starter_26_percent_combined(self):
        self.assertFalse(run(r4_no_stacking, make_proposal(target_tier="starter", discount=Decimal("0.06")), make_account(tier="starter", billing_cycle="annual", standing_discount=Decimal("0.20")), CampaignState(now=NOW)).passed)

    # R4: annual Enterprise's combined-authority ceiling is 40% (section 3's
    # provisional config); annual standing discount is the spec's 20%.
    def test_r4_enterprise_39_percent_combined(self):
        self.assertTrue(run(r4_no_stacking, make_proposal(target_tier="enterprise", discount=Decimal("0.19")), make_account(tier="enterprise", billing_cycle="annual", standing_discount=Decimal("0.20")), CampaignState(now=NOW)).passed)  # 20% + 19% = 39% < 40%

    def test_r4_enterprise_exactly_40_percent_combined(self):
        self.assertTrue(run(r4_no_stacking, make_proposal(target_tier="enterprise", discount=Decimal("0.20")), make_account(tier="enterprise", billing_cycle="annual", standing_discount=Decimal("0.20")), CampaignState(now=NOW)).passed)  # 20% + 20% = spec's 40%

    def test_r4_enterprise_41_percent_combined(self):
        self.assertFalse(run(r4_no_stacking, make_proposal(target_tier="enterprise", discount=Decimal("0.21")), make_account(tier="enterprise", billing_cycle="annual", standing_discount=Decimal("0.20")), CampaignState(now=NOW)).passed)  # 20% + 21% = 41% > 40%

    # R5: configured TTL is 600 seconds (10 minutes), and expiry is exclusive.
    def test_r5_one_second_before_expiry(self):
        self.assertEqual("exclusive", PolicyConfig().offer_ttl_comparison)  # R5 expiry comparison is deliberately exclusive.
        self.assertEqual(600, PolicyConfig().offer_ttl_seconds)  # R5 TTL is 10 minutes = 600 seconds in the spec/demo config.
        self.assertFalse(run(r5_one_active_offer, make_proposal(), make_account(offer_history=[active_offer(NOW + timedelta(seconds=1))])).passed)

    def test_r5_exactly_at_expiry(self):
        self.assertTrue(run(r5_one_active_offer, make_proposal(), make_account(offer_history=[active_offer(NOW)])).passed)

    def test_r5_one_second_after_expiry(self):
        self.assertTrue(run(r5_one_active_offer, make_proposal(), make_account(offer_history=[active_offer(NOW - timedelta(seconds=1))])).passed)

    # R5: per-customer offer cap is one active offer.
    def test_r5_zero_active_offers(self):
        self.assertTrue(run(r5_one_active_offer, make_proposal(), make_account()).passed)

    def test_r5_exactly_one_active_offer(self):
        self.assertFalse(run(r5_one_active_offer, make_proposal(), make_account(offer_history=[active_offer(NOW + timedelta(seconds=60))])).passed)

    def test_r5_two_active_offers(self):
        second = active_offer(NOW + timedelta(seconds=60))
        second["offer_id"] = "hand-offer-2"
        self.assertFalse(run(r5_one_active_offer, make_proposal(), make_account(offer_history=[active_offer(NOW + timedelta(seconds=60)), second])).passed)

    # R6: 10% of 10 Pro seats at ₹600 is ₹600 = 60,000 paise; exact budget
    # fit is inclusive.
    def test_r6_one_paise_remaining(self):
        state = CampaignState(campaign_id="hand-campaign", allocated_budget_paise=100000, reserved_discount_paise=39999, now=NOW)
        self.assertTrue(run(r6_campaign_budget, make_proposal(target_tier="pro", discount=Decimal("0.10"), campaign_id="hand-campaign", proposed_seats=10), make_account(), state).passed)

    def test_r6_exact_budget_fit(self):
        state = CampaignState(campaign_id="hand-campaign", allocated_budget_paise=100000, reserved_discount_paise=40000, now=NOW)
        self.assertTrue(run(r6_campaign_budget, make_proposal(target_tier="pro", discount=Decimal("0.10"), campaign_id="hand-campaign", proposed_seats=10), make_account(), state).passed)

    def test_r6_one_paise_over_budget(self):
        state = CampaignState(campaign_id="hand-campaign", allocated_budget_paise=100000, reserved_discount_paise=40001, now=NOW)
        self.assertFalse(run(r6_campaign_budget, make_proposal(target_tier="pro", discount=Decimal("0.10"), campaign_id="hand-campaign", proposed_seats=10), make_account(), state).passed)

    # R6 add-on boundary: Guest pack is ₹4,000, so 10% is 40,000 paise.
    def test_r6_addon_one_paise_remaining(self):
        state = CampaignState(campaign_id="hand-campaign", allocated_budget_paise=100000, reserved_discount_paise=59999, now=NOW)
        self.assertTrue(run(r6_campaign_budget, make_proposal(offer_type="addon", target_tier="pro", target_addon="guest_access_pack", discount=Decimal("0.10"), campaign_id="hand-campaign", proposed_seats=None), make_account(), state).passed)

    def test_r6_addon_exact_budget_fit(self):
        state = CampaignState(campaign_id="hand-campaign", allocated_budget_paise=100000, reserved_discount_paise=60000, now=NOW)
        self.assertTrue(run(r6_campaign_budget, make_proposal(offer_type="addon", target_tier="pro", target_addon="guest_access_pack", discount=Decimal("0.10"), campaign_id="hand-campaign", proposed_seats=None), make_account(), state).passed)

    def test_r6_addon_one_paise_over_budget(self):
        state = CampaignState(campaign_id="hand-campaign", allocated_budget_paise=100000, reserved_discount_paise=60001, now=NOW)
        self.assertFalse(run(r6_campaign_budget, make_proposal(offer_type="addon", target_tier="pro", target_addon="guest_access_pack", discount=Decimal("0.10"), campaign_id="hand-campaign", proposed_seats=None), make_account(), state).passed)

    # R7: higher, same, lower tiers; then seat increase and seats below usage.
    def test_r7_higher_tier(self):
        self.assertTrue(run(r7_upgrade_direction, make_proposal(target_tier="enterprise", proposed_seats=10), make_account(tier="pro")).passed)

    def test_r7_same_tier(self):
        self.assertFalse(run(r7_upgrade_direction, make_proposal(target_tier="pro", proposed_seats=10), make_account(tier="pro")).passed)

    def test_r7_lower_tier(self):
        self.assertFalse(run(r7_upgrade_direction, make_proposal(target_tier="starter", proposed_seats=10), make_account(tier="pro")).passed)

    def test_r7_seat_increase(self):
        self.assertTrue(run(r7_upgrade_direction, make_proposal(target_tier="pro", proposed_seats=11), make_account(tier="pro")).passed)

    def test_r7_below_current_usage(self):
        self.assertFalse(run(r7_upgrade_direction, make_proposal(target_tier="pro", proposed_seats=9), make_account(tier="pro", seats_in_use=10, seats_contracted=10)).passed)


if __name__ == "__main__":
    unittest.main()
