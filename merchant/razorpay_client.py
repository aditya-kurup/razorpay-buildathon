"""Razorpay Orders API adapter for the standalone merchant service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class RazorpayError(RuntimeError):
    pass


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = value.strip().strip("'\"")


class RazorpayClient:
    def __init__(self, *, dotenv_path: Path | None = None, opener: Any = None) -> None:
        path = dotenv_path or Path(__file__).resolve().parents[1] / ".env"
        _load_dotenv(path)
        self.key_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
        self.key_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
        if not self.key_id or not self.key_secret:
            raise RuntimeError(
                f"RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be configured in {path}"
            )
        self._opener = opener or urllib.request.urlopen

    def create_order(self, amount_paise: int, currency: str, receipt: str) -> dict[str, Any]:
        payload = {"amount": amount_paise, "currency": currency, "receipt": receipt, "payment_capture": 1}
        request = urllib.request.Request(
            "https://api.razorpay.com/v1/orders",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Basic " + base64.b64encode(
                    f"{self.key_id}:{self.key_secret}".encode("utf-8")
                ).decode("ascii"),
                "Content-Type": "application/json",
            }, method="POST",
        )
        try:
            with self._opener(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            raise RazorpayError(f"order creation failed: {exc}") from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise RazorpayError("order creation returned invalid JSON") from exc
        if not isinstance(body, dict) or not body.get("id"):
            raise RazorpayError("order creation response did not contain an order id")
        return body

    def verify_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        expected = hmac.new(
            self.key_secret.encode("utf-8"),
            f"{order_id}|{payment_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature or "")
