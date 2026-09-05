#!/usr/bin/env python3
"""Stream one customer-initiated merchant-service conversation."""

from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _print_event(event: str, data: dict) -> None:
    if event == "customer_message_received":
        print("\nCUSTOMER REQUEST")
        print(f"  {data.get('message', '')}")
    elif event == "eligibility_result":
        print("\nELIGIBILITY")
        print(f"  passed: {data.get('passed')}")
        print(f"  reason: {data.get('reason') or 'none'}")
        band = data.get("discount_band") or {}
        print(f"  discount band: {band.get('minimum', '?')} - {band.get('maximum', '?')}")
        options = data.get("allowed_options") or []
        print(f"  allowed options: {', '.join(str(item.get('option_id')) for item in options) or 'none'}")
    elif event == "prompt_built":
        print("\nPROMPT")
        print("  customer message included as untrusted input; policy inputs remain code-computed")
    elif event == "model_call_started":
        print("\nMODEL")
        print("  call started")
    elif event == "model_response":
        print("  response received")
        raw = data.get("raw_json")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                pass
        if isinstance(raw, dict):
            print(f"  proposal: option={raw.get('option_id') or raw.get('offer_type')}, discount={raw.get('proposed_discount')}, seats={raw.get('proposed_seats')}")
    elif event == "field_discard":
        print(f"  field discarded: {data.get('field')} ({data.get('reason')})")
    elif event == "policy_verdict":
        print("\nPOLICY")
        print(f"  verdict: {'approved' if data.get('approved') else 'rejected'}")
        print(f"  rules fired: {', '.join(data.get('rules_fired') or []) or 'none'}")
    elif event == "offer_stored":
        print(f"\nOFFER STORED\n  id: {data.get('offer_id')}\n  price: {data.get('price_paise')} paise")
    elif event == "done":
        print("\nRESULT")
        print(f"  status: {data.get('status')}")
        if data.get("proposal"):
            proposal = data["proposal"]
            print(f"  proposal: {proposal.get('offer_type')} / discount {proposal.get('proposed_discount')} / seats {proposal.get('proposed_seats')}")
            print(f"  reason: {proposal.get('reason')}")
        elif data.get("reason"):
            print(f"  reason: {data['reason']}")
    elif event == "error":
        print(f"\nERROR ({data.get('stage', 'request')}): {data.get('message')}")


def _stream(base_url: str, customer_id: str, message: str) -> None:
    request = Request(
        f"{base_url.rstrip('/')}/api/converse/{customer_id}",
        data=json.dumps({"message": message}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    event = None
    data_lines: list[str] = []
    with urlopen(request, timeout=180) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data_lines.append(line[6:])
            elif not line and event:
                try:
                    payload = json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    payload = {"message": "invalid SSE JSON"}
                _print_event(event, payload)
                event = None
                data_lines = []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("customer_id", nargs="?")
    parser.add_argument("message", nargs="?")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    customer_id = args.customer_id or input("Customer ID: ").strip()
    message = args.message or input("Customer message: ")
    try:
        _stream(args.base_url, customer_id, message)
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
