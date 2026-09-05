# Synthetic account coverage

Generated with seed `20260829` for `50` accounts as of `2026-08-29`.
The population is intentionally stratified and strata may overlap.

## Requested strata

| Stratum | Target | Actual |
|---|---:|---:|
| `near_seat_ceiling` | 8 | 8 |
| `discount_cap` | 6 | 6 |
| `annual_existing_discount` | 8 | 8 |
| `thin_margin_headroom` | 5 | 5 |
| `active_offer` | 5 | 5 |
| `renewal_window` | 6 | 6 |
| `hostile_or_injection` | 10 | 10 |
| `ordinary` | remainder | 11 |

## Derived rule-boundary coverage

| Boundary | Accounts | Definition |
|---|---:|---|
| Seat ceiling | 8 | At least 90% of contracted seats in use |
| Promotional discount cap | 6 | Existing promotion equals the tier ceiling |
| Annual billing | 23 | Standing 20% annual discount applies |
| Thin discount headroom | 5 | At most 5 percentage points of new margin headroom |
| Unexpired active offer | 5 | Unredeemed offer expires after the as-of timestamp |
| Renewal window | 6 | Contract anniversary is within 30 days |
| Starter without history add-on | 24 | Unlimited history is tier-gated away |
| Pro without compliance add-on | 8 | Compliance export is Enterprise-only |

## Constraint and stacking checks

| Check | Accounts |
|---|---:|
| Margin floor is tighter | 16 |
| Tier discount ceiling is tighter | 34 |
| Exact ties | 0 |
| Standing and existing discounts both non-zero | 6 |

## Pricing sanity checks

- Margin floor: `55%`.
- Annual standing discount: `20%` off list price.
- Lowest current-state gross margin: `56.63%`.
- Highest current-state gross margin: `73.33%`.
- Invoice histories are generated backwards from each account's current seat count and growth rate.
