# Policy-gated growth agent

## Setup

Clone the repository, create a virtual environment, install the runtime
dependencies, copy the environment template, start the service, and open the
dashboard:

```bash
git clone https://github.com/aditya-kurup/razorpay-buildathon.git
cd razorpay-buildathon
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn merchant.api:app --host 127.0.0.1 --port 8000
open http://localhost:8000/dashboard
```

Set `GEMINI_API_KEY`, `GEMINI_MODEL`, `RAZORPAY_KEY_ID`, and
`RAZORPAY_KEY_SECRET` in `.env` before running live model or payment flows.
The service creates `merchant.sqlite3` locally on first start. It is ignored
by Git.

## What this contains

This repository combines the merchant service, the policy-gated growth-agent
pipeline, synthetic account data, evaluation scripts, and the dashboard demo.
The seller-initiated upsell path, customer-initiated single-turn path,
campaign path, Razorpay payment leg, and append-only audit chain share the
same local SQLite store.

Useful commands:

```bash
python audit_verify.py
python scripts/reseed.py
python scripts/converse.py acct-003 "What can you do for our growing team?"
python -m unittest discover -s tests -p 'test_*.py'
```

`scripts/revenue_metrics.py` is a read-only analysis tool. The standalone
Razorpay checkout HTML used during local testing is intentionally excluded
from the public clone.
