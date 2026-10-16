# Policy-gated growth agent

An agent-readable commerce demo in which a Gemini model proposes growth
offers, deterministic server-side code filters and adjudicates them, and a
merchant service persists the resulting offer and payment lifecycle. The
repository is intentionally built around synthetic account data and a local
SQLite database, so it is suitable for a demo or evaluation—not production
commerce.

## Setup

### Requirements

- Python 3.10 or newer (the code uses modern type annotations).
- A Gemini API key and model name for live proposer/strategist calls.
- Razorpay test-mode key ID and key secret for live order/payment tests.
- `ngrok` is optional and is needed only when a local payment callback must be
  reached from a browser checkout running outside the local origin.

### Install and run

```bash
git clone https://github.com/aditya-kurup/razorpay-buildathon.git
cd razorpay-buildathon
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and provide values for the four variables below:

```text
GEMINI_API_KEY=your-gemini-key
GEMINI_MODEL=your-model-slug
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=your-razorpay-test-secret
```

Start the service from the repository root:

```bash
uvicorn merchant.api:app --host 127.0.0.1 --port 8000
```

Open the operator dashboard at [http://localhost:8000/dashboard](http://localhost:8000/dashboard).
The first database connection creates `merchant.sqlite3` locally. It is
ignored by Git and is not included in the public repository.

For a clean local database containing the generic seed commerce tables:

```bash
python scripts/reseed.py
```

`reseed.py` is destructive to the local SQLite file. It is a development
reset, not a migration or a production data-management command.

## System overview

The service has three entry points that converge on the same core controls:

```text
seller trigger ───────┐
customer message ─────┼─> Eligibility Filter -> Proposer/Strategist
campaign cohort ──────┘                         -> Policy Engine
                                                  -> Reservation Manager
                                                  -> Offer Store -> Razorpay
```

The model is allowed to propose within the inputs supplied by the
application. The Eligibility Filter computes allowed options and a discount
band from account state. The Policy Engine is the final deterministic
adjudicator. Server-side field guards run before adjudication, including
campaign-field discards and the per-account `proposed_seats` ceiling clamp.
Customer text is explicitly marked untrusted and cannot change policy inputs.

### Seller-initiated upsell

An account is triggered when either:

- `seats_in_use / seats_contracted >= 90%`; or
- its derived contract anniversary falls within the configured 30-day renewal
  window relative to `2026-08-29`.

The dashboard defaults `bypass_cache=true` for demo upsell runs. Pass
`bypass_cache=false` explicitly if you want the normal fixture/cache path.

### Customer-initiated conversation

This is a single-turn additive entry point, not a second policy system and not
a negotiation protocol. It accepts one customer message, runs eligibility
without using that message, includes the message as delimited untrusted input
to the proposer, and returns one streamed proposal/verdict result.

Example terminal client:

```bash
python scripts/converse.py acct-003 \
  "We are adding a few people next month; what can you offer?"
```

The client can also prompt interactively when the positional arguments are
omitted.

### Campaign path

The campaign path segments eligible catalog-compatible accounts into the
deterministic `seats_small`, `seats_medium`, and `seats_large` cohorts for the
`guest_access_pack` campaign. It makes one Strategist call per cohort, then
expands the strategy per eligible member. Campaign reservations are held
against the in-process campaign budget and persisted offers; expired holds
release budget and remain visible as `EXPIRED` offers.

The current synthetic segmentation boundaries are:

| Cohort | Contracted seats |
|---|---:|
| `seats_small` | 1–20 |
| `seats_medium` | 21–60 |
| `seats_large` | 61+ |

## Offer and payment lifecycle

Offers are stored in `merchant_offers` and move only forward:

```text
RESERVED -> ACCEPTED -> ORDER_CREATED -> PAID
       \-> FAILED
       \-> EXPIRED
```

`ORDER_CREATED` offers are not expired by the reservation TTL path. The
accept endpoint reads the persisted `price_paise` and `currency` from the
offer record; it never recomputes the amount from model output. Payment
callbacks verify the Razorpay signature, order ID, amount, and currency on the
server. `razorpay_payment_id` is the idempotency key, so an identical callback
does not create a second payment transition.

The dashboard includes an Offers panel. For a `RESERVED` offer it calls the
same `/offers/{offer_id}/accept` endpoint used by an external customer flow,
loads Razorpay Standard Checkout, and posts the callback result back to the
merchant service. The dashboard obtains only the public Razorpay key ID from
`GET /api/config`; the key secret is never exposed to browser code.

## HTTP API

### Read endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check: `{"ok": true}` |
| GET | `/dashboard` | Serves the operator dashboard HTML |
| GET | `/catalog` | Reads the generic seeded commerce catalog |
| GET | `/api/accounts` | Account summaries, triggers, and active-offer state |
| GET | `/api/offers` | Offers ordered by most recent transition |
| GET | `/api/metrics` | Audit-derived pipeline, payment, budget, and chain metrics |
| GET | `/api/config` | Public-only config: `razorpay_key_id` |
| GET | `/api/audit/stream?since_seq=N` | Server-sent audit event stream |

### Pipeline and payment endpoints

| Method | Path | Response |
|---|---|---|
| POST | `/api/run/upsell/{customer_id}?bypass_cache=true` | SSE upsell lifecycle |
| POST | `/api/converse/{customer_id}` | SSE single-turn conversation; body is `{"message":"..."}` |
| POST | `/api/run/campaign` | SSE campaign/cohort lifecycle |
| POST | `/offers/{offer_id}/accept` | Creates a Razorpay order from stored offer price |
| POST | `/offers/{offer_id}/payment_callback` | Verifies and records a payment callback |
| POST | `/api/audit/tamper_demo` | Demo-only SSE exercise of audit tamper detection/repair |

Payment callback bodies must contain integer `amount_paise`, `currency`, and
the Razorpay identifiers. Both native browser names and the service's short
aliases are accepted:

```json
{
  "razorpay_payment_id": "pay_...",
  "razorpay_order_id": "order_...",
  "razorpay_signature": "...",
  "amount_paise": 100,
  "currency": "INR"
}
```

The legacy `/quote` and `/commit` routes intentionally return HTTP 501; they
are schema-phase stubs and are not part of the growth-offer demo.

## Dashboard walkthrough

1. Start the service and open `/dashboard`.
2. In Accounts, choose a triggered account without an active offer.
3. Click **Run upsell**. The timeline shows trigger detection, eligibility,
   prompt/model activity, field guards, policy adjudication, and offer storage.
4. For the conversational path, use `scripts/converse.py` or call the
   endpoint directly with one customer message.
5. Click **Run campaign** to show cohort formation, one Strategist phase per
   cohort, member expansion, policy decisions, reservations, and budget
   updates.
6. In Offers, click **Simulate customer accept & pay (demo)** only for a
   `RESERVED` offer. For an actual Razorpay test checkout, use test mode and
   verify the callback through a public HTTPS tunnel such as ngrok:

```bash
ngrok http 8000
```

The dashboard's `MERCHANT_BASE_URL` constant defaults to
`window.location.origin`. Set it to the ngrok HTTPS origin before a checkout
whose callback must return from a standalone browser context. Never put the
Razorpay secret in the HTML.

## Audit trail

Every money-affecting lifecycle event is synchronously appended to the
`audit_events` SQLite table. Rows contain:

- `seq`: monotonic primary key;
- `timestamp`: ISO-8601 UTC timestamp;
- `event_type` and `actor`: validated against fixed enumerations;
- `subject_id` and canonical, sorted `payload_json`;
- `prev_hash` and `row_hash`: a SHA-256 hash chain using a stable unit
  separator.

Writes read the current chain tail from SQLite inside the append transaction.
Database triggers prevent updates and deletes. The recorded event set covers
proposal generation, field discards, policy verdicts, offers, reservations,
cohort/strategy generation, and payment acceptance, order creation, callback
verification/rejection, payment, and failure.

Verify the chain with:

```bash
python audit_verify.py
```

The command prints `chain intact` and exits 0 when valid. It prints the first
broken sequence and exits 1 when a row hash or chain link does not verify.
The dashboard's **Run tamper demo** is deliberately a demonstration control
for the audit UI; do not use it as an operational repair mechanism.

## Evaluation and analysis scripts

All scripts are intended to be run from the repository root.

### Authority comparison

```bash
python scripts/authority_comparison.py
```

Runs the three authority variants—`options_narrowed_in_code`,
`constraints_in_prompt`, and `no_constraints`—against the triggered synthetic
accounts. It uses fresh temporary response caches, reconciles merchant offers
for R5 visibility, writes tagged audit evidence, and prints per-variant
proposal, verdict, discard, schema-failure, model-error, retry, and latency
statistics. It is a live Gemini evaluation and may consume API quota.

### Revenue metrics

```bash
python scripts/revenue_metrics.py
```

This is a read-only analysis of synthetic account and offer/audit data. It
writes the reproducibility report to `notes/revenue-metrics.json`; that JSON is
ignored because reports can contain run-specific data. Figures are expected
uplift calculations, not realized revenue, and assume an explicitly stated
acceptance rate.

### Tests

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

The test suite covers policy registration and boundary behavior. The fixture
responses under `tests/fixtures/` support deterministic model-response tests
and cache paths; live API calls are not required for the unit tests.

## Repository layout

```text
agent/                 Eligibility, proposer, strategist, Gemini adapter, prompts
data/                  Synthetic accounts, catalog, generator, coverage notes
merchant/              FastAPI app, SQLite schema, offers, payment, audit, dashboard
policy/                Deterministic policy checker and rules R1–R7
reservation/           Campaign budget and reservation lifecycle
scripts/               Conversation client, evaluations, metrics, DB reset
tests/                 Policy tests, boundary/concurrency helpers, fixtures
notes/                 Local analysis outputs; JSON reports are ignored
audit_verify.py        Standalone audit-chain verifier
requirements.txt       Pinned runtime dependency set
.env.example           Variable names only; no credentials
```

## Security and data boundaries

- `.env`, `.venv/`, SQLite databases, logs, Python caches, and standalone
  checkout HTML are ignored and are not public repository artifacts.
- Razorpay key secrets and Gemini API keys must be supplied through the local
  environment, never committed.
- Audit payloads redact full payment signatures; payment and order IDs are
  retained for traceability.
- The account population, prices, policy inputs, and reported revenue numbers
  are synthetic. Do not treat them as customer or financial production data.
- The service has no authentication or authorization layer. Bind it locally
  for demos and use an appropriate protected deployment boundary for any
  externally reachable test.

## Known limitations

- SQLite and the in-process campaign budget manager are local-demo storage,
  not a multi-instance production architecture.
- Gemini availability, model output, quota, latency, and pricing depend on the
  configured provider account.
- Razorpay checkout requires test credentials and a reachable callback origin;
  this repository does not include a hosted checkout page or webhook service.
- The dashboard's tamper demo intentionally exercises audit-chain break and
  repair behavior; it should not be exposed as an administrative API in a
  production deployment.

## Free public portfolio deployment

`render.yaml` configures a free Render Python service. Set `PUBLIC_DEMO=true`
for a credential-free public dashboard: proposals are explicitly simulated, while
eligibility, policy checks, campaign reservations, offer storage, and the audit
chain use the real application code. Payment and audit mutation endpoints are
disabled. Do not upload `.env` or API credentials for this public mode.

The root URL redirects to `/dashboard`. Free Render services sleep when idle;
the first request can take about a minute. SQLite state resets on service restarts
or redeploys, and visitors share the synthetic demo state. Run one worker.
