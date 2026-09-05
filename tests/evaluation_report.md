# Policy checker evaluation

The valid generated matrix is 20 account shapes × 28 proposal shapes = 560 cases.
Malformed inputs add 24 cases. Expected verdicts come from the literal oracle in `generated_cases.py`.
The oracle does not call `rules.py`, `checker.py`, or `catalog.py`; those are used only to produce the actual checker result. This is therefore a correctness comparison, not merely a consistency sweep.

## Case totals

| Set | Cases | Expected rejected | Expected raise |
|---|---:|---:|---:|
| Hand-written boundary assertions | 51 individual assertions | 21 | 0 |
| Generated valid matrix | 560 | 416 | 0 |
| Malformed | 24 | 7 | 17 |
| Evaluated total | 584 | 423 | 17 |

## Hand-written assertion count

| Rule | Individual assertions |
|---|---:|
| R1 | 9 |
| R2 | 9 |
| R3 | 6 |
| R4 | 9 |
| R5 | 7 |
| R6 | 6 |
| R7 | 5 |
| Total | 51 |

## Outcomes

- Actual rejected: **423**
- Actual approved: **144**
- False allows: **0** — target zero.
- False blocks: **0** — reported honestly.
- Checker crashes: **17** — reported separately below.

## Per-rule firing counts

| Rule | Expected fires | Actual fires | Status |
|---|---:|---:|---|
| R1 | 141 | 141 | covered |
| R2 | 157 | 157 | covered |
| R3 | 85 | 85 | covered |
| R4 | 128 | 128 | covered |
| R5 | 140 | 140 | covered |
| R6 | 102 | 102 | covered |
| R7 | 89 | 89 | covered |

## Crash cases

- `malformed__null_discount`: `ValueError: Malformed proposal: proposed discount is required`
- `malformed__float_discount`: `ValueError: Malformed proposal: proposed discount must not be a float`
- `malformed__excessive_decimal_discount`: `ValueError: Malformed proposal: proposed discount has more than two decimal places`
- `malformed__nonnumeric_discount`: `ValueError: Malformed proposal: proposed discount is not numeric`
- `malformed__null_customer_id`: `ValueError: Malformed proposal: customer id is required`
- `malformed__empty_customer_id`: `ValueError: Malformed proposal: customer id is required`
- `malformed__null_offer_type`: `ValueError: Malformed proposal: offer type is required`
- `malformed__unsupported_offer_type`: `ValueError: Malformed proposal: unsupported offer type 'coupon'`
- `malformed__null_target_tier`: `ValueError: Malformed proposal: upgrade proposal requires target_tier`
- `malformed__null_reason`: `ValueError: Malformed proposal: model reason is required`
- `malformed__null_seats`: `ValueError: Malformed proposal: upgrade proposal requires proposed_seats`
- `malformed__addon_with_target_tier`: `ValueError: Malformed proposal: add-on proposal must not name target_tier`
- `malformed__addon_without_target_addon`: `ValueError: Malformed proposal: add-on proposal requires target_addon`
- `malformed__boolean_discount`: `ValueError: Malformed proposal: proposed discount is not numeric`
- `malformed__boolean_seats`: `ValueError: Malformed proposal: proposed_seats must be an integer`
- `malformed__missing_customer_id_key`: `ValueError: Malformed proposal: customer id is required`
- `malformed__null_target_addon_for_addon`: `ValueError: Malformed proposal: add-on proposal requires target_addon`

## False allows

None.

## False blocks

None.

## Malformed expectations

Expected rejection: `malformed__negative_discount`, `malformed__unknown_target_tier`, `malformed__seat_below_usage`, `malformed__negative_seats`, `malformed__unknown_addon`, `malformed__nonexistent_campaign`, `malformed__campaign_without_budget`.
Expected raise: `malformed__null_discount`, `malformed__float_discount`, `malformed__excessive_decimal_discount`, `malformed__nonnumeric_discount`, `malformed__null_customer_id`, `malformed__empty_customer_id`, `malformed__null_offer_type`, `malformed__unsupported_offer_type`, `malformed__null_target_tier`, `malformed__null_reason`, `malformed__null_seats`, `malformed__addon_with_target_tier`, `malformed__addon_without_target_addon`, `malformed__boolean_discount`, `malformed__boolean_seats`, `malformed__missing_customer_id_key`, `malformed__null_target_addon_for_addon`.

## Boundary semantics

- R1 margin floor, R2 per-offer ceiling, R4 combined authority, and R6 budget are inclusive at exact equality.
- R5 offer expiry is deliberately exclusive: exact expiry is expired. R5 offer-cap comparison is inclusive.
- Hand-written expected verdicts are literal booleans reasoned from the spec; generated expected verdicts use the independent literal oracle in this file.
