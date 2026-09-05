from datetime import date, datetime, time, timedelta, timezone
from dataclasses import asdict, replace
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
import os
import queue
import re
import sys
import tempfile
import threading
import time as time_module
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

try:
    from payments.razorpay_client import RazorpayClient
except ModuleNotFoundError:
    from .razorpay_client import RazorpayClient

from .db import connect
from .offer_store import MerchantOfferStore, MerchantOffer, ORDER_CREATED, RESERVED


# The consolidated repository root contains both the merchant service and
# growth-agent packages. An environment override remains useful for local
# development, while the default is portable for a fresh clone.
GROWTH_ROOT = Path(
    os.getenv(
        "GROWTH_AGENT_ROOT",
        str(Path(__file__).resolve().parents[1]),
    )
)
if str(GROWTH_ROOT) not in sys.path:
    sys.path.insert(0, str(GROWTH_ROOT))

from agent.eligibility import filter_account  # noqa: E402
from agent.model_client import GeminiModelClient, ModelClientConfig, ModelResponseError  # noqa: E402
from agent.proposer import AgentConfig, Proposer, _jsonable  # noqa: E402
from agent.prompts import get_customer_initiated_prompt, get_prompt  # noqa: E402
from agent.segmenter import segment_accounts  # noqa: E402
from agent.strategist import Strategist  # noqa: E402
from data import catalog as growth_catalog  # noqa: E402
from policy.checker import check_proposal  # noqa: E402
from policy.rules import CampaignState  # noqa: E402
from reservation.manager import ReservationManager  # noqa: E402
from reservation.store import OfferStore as GrowthOfferStore  # noqa: E402
from .audit_events import AuditEventLogger, verify_audit_chain  # noqa: E402


LOGGER = logging.getLogger(__name__)

app = FastAPI(title="Agent-readable commerce merchant service")
_offer_store = MerchantOfferStore()
_payment_gateway: Any = None
_reservation_release: Callable[[MerchantOffer, str], None] | None = None
_audit_event_logger = AuditEventLogger()
_audit_condition = threading.Condition()
AS_OF_DATETIME = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
AS_OF_DATE = AS_OF_DATETIME.date()
INJECTION_IDS = {f"acct-{number:03d}" for number in range(32, 42)}
CAMPAIGN_ID = "guest-access-pack-launch"
CAMPAIGN_BUDGET_PAISE = 5_000_000
CAMPAIGN_TARGET_ADDON = "guest_access_pack"


def configure_payment_dependencies(*, offer_store: MerchantOfferStore | None = None, gateway: Any = None,
                                   reservation_release: Callable[[MerchantOffer, str], None] | None = None) -> None:
    """Inject stores/gateways for tests and the demo without changing endpoints."""
    global _offer_store, _payment_gateway, _reservation_release
    if offer_store is not None:
        _offer_store = offer_store
    _payment_gateway = gateway
    _reservation_release = reservation_release


def _release_reservation(offer: MerchantOffer, reason: str, now: datetime) -> None:
    if offer.campaign_id:
        released = _offer_store.release_campaign_reservation(offer, now, reason)
        if released:
            _offer_store.append_audit(
                offer_id=offer.offer_id,
                event_type="released",
                timestamp=now,
                details={
                    "campaign_id": offer.campaign_id,
                    "amount_paise": offer.discount_value_paise,
                    "reason": reason,
                },
            )
        if _reservation_release is not None:
            _reservation_release(offer, reason)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gateway() -> Any:
    global _payment_gateway
    if _payment_gateway is None:
        _payment_gateway = RazorpayClient()
    return _payment_gateway


_CALLBACK_PATH = re.compile(r"^/offers/[^/]+/payment_callback$")


def _callback_cors_headers(request: Request) -> dict[str, str]:
    if request.headers.get("origin") != "null":
        return {}
    return {
        "Access-Control-Allow-Origin": "null",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "content-type",
        "Vary": "Origin",
    }


@app.middleware("http")
async def callback_cors(request: Request, call_next):
    """Permit local-file requests only for the payment callback route."""
    if not _CALLBACK_PATH.fullmatch(request.url.path):
        return await call_next(request)
    headers = _callback_cors_headers(request)
    if request.method == "OPTIONS":
        if not headers:
            return Response(status_code=403)
        return Response(status_code=204, headers=headers)
    response = await call_next(request)
    response.headers.update(headers)
    return response


@app.get("/health")
def health():
    return {"ok": True}


def _env_value(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    env_path = GROWTH_ROOT / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, raw_value = stripped.split("=", 1)
            if key.strip() == name:
                return raw_value.strip().strip("'\"")
    except OSError:
        pass
    return ""


@app.get("/api/config")
def dashboard_config() -> dict[str, str]:
    return {"razorpay_key_id": _env_value("RAZORPAY_KEY_ID")}


@app.get("/catalog")
def catalog():
    conn = connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM products ORDER BY id")]
    conn.close()
    return {"products": rows}


@app.post("/quote")
def quote_stub():
    raise HTTPException(501, "quote not implemented in schema phase")


@app.post("/commit")
def commit_stub():
    raise HTTPException(501, "commit not implemented in schema phase")


@app.post("/offers/{offer_id}/accept")
def accept_offer(offer_id: str) -> dict[str, Any]:
    now = _now()
    try:
        offer, expired = _offer_store.accept(offer_id, now)
    except KeyError:
        _offer_store.record_event("UNKNOWN_OFFER", "accept requested for unknown offer", now, offer_id)
        raise HTTPException(404, "offer not found")
    if expired:
        _release_reservation(offer, "offer_expired_before_accept", now)
        raise HTTPException(409, "offer has expired")
    if offer.status != "ACCEPTED":
        raise HTTPException(409, f"offer is not RESERVED: {offer.status}")

    # The amount is deliberately read once from the persisted offer record.
    stored_amount_paise = offer.price_paise
    try:
        order = _gateway().create_order(
            amount_paise=stored_amount_paise,
            currency=offer.currency,
            receipt=offer.offer_id,
        )
        order_id = str(order["id"])
        updated = _offer_store.order_created(offer_id, order_id, _now())
    except Exception as exc:
        reason = f"Razorpay order creation failed: {exc}"
        LOGGER.exception("%s for offer %s", reason, offer_id)
        try:
            failed = _offer_store.failed(offer_id, _now(), reason)
            _release_reservation(failed, "order_creation_failed", _now())
        except Exception:
            LOGGER.exception("Could not mark failed offer %s or release its reservation", offer_id)
        raise HTTPException(502, "payment order creation failed")
    return {
        "offer_id": updated.offer_id,
        "order_id": order_id,
        "amount_paise": stored_amount_paise,
        "currency": updated.currency,
        "razorpay_order": order,
    }


@app.post("/offers/{offer_id}/payment_callback")
def payment_callback(offer_id: str, body: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    try:
        offer = _offer_store.get(offer_id)
    except KeyError:
        _offer_store.record_event("UNKNOWN_OFFER", "payment callback for unknown offer", now, offer_id)
        _offer_store.append_audit_event(
            event_type="PAYMENT_CALLBACK_RECEIVED",
            actor="payment_leg",
            subject_id=offer_id,
            timestamp=now,
            payload={
                "razorpay_payment_id": body.get("payment_id", body.get("razorpay_payment_id")),
                "reported_amount_paise": body.get("amount_paise"),
                "signature": _redacted_signature(body.get("signature", body.get("razorpay_signature"))),
            },
        )
        _offer_store.append_audit_event(
            event_type="PAYMENT_REJECTED",
            actor="payment_leg",
            subject_id=offer_id,
            timestamp=now,
            payload={"reason": "unknown_offer"},
        )
        _offer_store.append_audit(
            offer_id=offer_id,
            event_type="rejected_unknown_offer",
            timestamp=now,
            details={"reason": "payment callback for unknown offer"},
        )
        raise HTTPException(404, "offer not found")

    payment_id = body.get("payment_id", body.get("razorpay_payment_id"))
    order_id = body.get("order_id", body.get("razorpay_order_id"))
    signature = body.get("signature", body.get("razorpay_signature"))
    _offer_store.append_audit_event(
        event_type="PAYMENT_CALLBACK_RECEIVED",
        actor="payment_leg",
        subject_id=offer.customer_id,
        timestamp=now,
        payload={
            "razorpay_payment_id": payment_id,
            "reported_amount_paise": body.get("amount_paise"),
            "signature": _redacted_signature(signature),
        },
    )

    # Accept Razorpay's native browser field names as well as the internal names.
    if not all(isinstance(value, str) and value for value in (payment_id, order_id, signature)):
        _offer_store.record_event("MALFORMED_CALLBACK", "payment callback is missing required fields", now, offer_id, payment_id)
        _offer_store.append_audit_event(
            event_type="PAYMENT_REJECTED",
            actor="payment_leg",
            subject_id=offer.customer_id,
            timestamp=now,
            payload={"reason": "malformed_callback"},
        )
        raise HTTPException(400, "payment_id, order_id, and signature are required")
    if not isinstance(body.get("amount_paise"), int) or isinstance(body.get("amount_paise"), bool):
        _offer_store.record_event("MALFORMED_CALLBACK", "amount_paise must be an integer", now, offer_id, payment_id)
        _offer_store.append_audit_event(
            event_type="PAYMENT_REJECTED",
            actor="payment_leg",
            subject_id=offer.customer_id,
            timestamp=now,
            payload={"reason": "malformed_amount"},
        )
        raise HTTPException(400, "amount_paise must be an integer")
    amount_paise = body["amount_paise"]
    currency = body.get("currency")
    if not isinstance(currency, str) or not currency:
        _offer_store.record_event("MALFORMED_CALLBACK", "currency is required", now, offer_id, payment_id)
        _offer_store.append_audit_event(
            event_type="PAYMENT_REJECTED",
            actor="payment_leg",
            subject_id=offer.customer_id,
            timestamp=now,
            payload={"reason": "malformed_currency"},
        )
        raise HTTPException(400, "currency is required")

    try:
        valid_signature = _gateway().verify_signature(offer.razorpay_order_id or order_id, payment_id, signature)
    except Exception as exc:
        valid_signature = False
        LOGGER.exception("Signature verification failed for offer %s: %s", offer_id, exc)
    if not valid_signature or order_id != offer.razorpay_order_id:
        detail = "Razorpay signature mismatch or order id mismatch"
        LOGGER.error("%s: offer=%s payment=%s", detail, offer_id, payment_id)
        _offer_store.record_event("SIGNATURE_MISMATCH", detail, now, offer_id, payment_id)
        _offer_store.append_audit(
            offer_id=offer_id,
            event_type="rejected_bad_signature",
            timestamp=now,
            details={
                "payment_id": payment_id,
                "order_id": order_id,
                "expected_order_id": offer.razorpay_order_id,
                "reason": detail,
            },
        )
        _offer_store.append_audit_event(
            event_type="PAYMENT_REJECTED",
            actor="payment_leg",
            subject_id=offer.customer_id,
            timestamp=now,
            payload={"reason": "signature_mismatch"},
        )
        raise HTTPException(400, "payment signature verification failed")
    if amount_paise != offer.price_paise or currency != offer.currency:
        detail = (
            f"SERIOUS AMOUNT MISMATCH: callback={amount_paise} {currency}; "
            f"stored={offer.price_paise} {offer.currency}"
        )
        LOGGER.critical("%s offer=%s payment=%s", detail, offer_id, payment_id)
        _offer_store.record_event("AMOUNT_MISMATCH", detail, now, offer_id, payment_id)
        _offer_store.append_audit(
            offer_id=offer_id,
            event_type="rejected_amount_mismatch",
            timestamp=now,
            details={
                "payment_id": payment_id,
                "order_id": order_id,
                "stored_amount_paise": offer.price_paise,
                "reported_amount_paise": amount_paise,
                "stored_currency": offer.currency,
                "reported_currency": currency,
            },
        )
        _offer_store.append_audit_event(
            event_type="PAYMENT_REJECTED",
            actor="payment_leg",
            subject_id=offer.customer_id,
            timestamp=now,
            payload={"reason": "amount_mismatch"},
        )
        raise HTTPException(400, "payment amount or currency does not match offer")
    try:
        updated, recorded = _offer_store.record_paid(
            offer_id, payment_id, order_id, amount_paise, currency, now
        )
    except ValueError as exc:
        _offer_store.record_event("INVALID_PAYMENT_STATE", str(exc), now, offer_id, payment_id)
        _offer_store.append_audit_event(
            event_type="PAYMENT_REJECTED",
            actor="payment_leg",
            subject_id=offer.customer_id,
            timestamp=now,
            payload={"reason": "invalid_payment_state", "detail": str(exc)},
        )
        raise HTTPException(409, str(exc))
    if not recorded:
        return {"ok": True, "duplicate": True, "offer_id": offer_id, "status": updated.status}
    return {"ok": True, "duplicate": False, "offer_id": offer_id, "status": updated.status}


def _redacted_signature(value: Any) -> str | None:
    """Keep only the permitted eight-character signature prefix in audit data."""
    return value[:8] if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# Judge-facing dashboard integration
# ---------------------------------------------------------------------------


def _load_accounts() -> list[dict[str, Any]]:
    path = GROWTH_ROOT / "data" / "accounts.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _reconcile_account(account: Mapping[str, Any]) -> dict[str, Any]:
    """Return an account copy whose offer history reflects merchant SQLite.

    R5 remains a pure rule over ``account.offer_history``. The merchant layer
    supplies the current RESERVED/ACCEPTED/ORDER_CREATED offers in that
    shape before eligibility or adjudication, without mutating accounts.json
    or the loaded account object.
    """
    customer_id = str(account.get("customer_id", ""))
    reconciled = dict(account)
    history = [dict(item) if isinstance(item, Mapping) else item
               for item in (account.get("offer_history", []) or [])]
    now = _now()
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT o.offer_id, o.campaign_id, o.expires_at, o.status,
                   o.price_paise, o.discount_value_paise, o.currency,
                   t.occurred_at AS created_at
            FROM merchant_offers AS o
            LEFT JOIN offer_transitions AS t
              ON t.offer_id = o.offer_id AND t.from_status IS NULL
            WHERE o.customer_id = ?
              AND o.status IN ('RESERVED', 'ACCEPTED', 'ORDER_CREATED')
            ORDER BY o.offer_id
            """,
            (customer_id,),
        ).fetchall()
    finally:
        conn.close()

    existing_ids = {
        str(item.get("offer_id"))
        for item in history
        if isinstance(item, Mapping) and item.get("offer_id") is not None
    }
    for row in rows:
        expires_at = _parse_account_datetime(row["expires_at"])
        if expires_at is None or expires_at <= now:
            continue
        if row["offer_id"] in existing_ids:
            continue
        history.append({
            "offer_id": row["offer_id"],
            "campaign_id": row["campaign_id"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "redeemed": False,
            "redemption_status": "unredeemed",
            "status": "active",
            "price_paise": row["price_paise"],
            "discount_value_paise": row["discount_value_paise"],
            "currency": row["currency"],
        })
    reconciled["offer_history"] = history
    return reconciled


def _parse_account_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _expire_active_offers(customer_id: str) -> list[MerchantOffer]:
    """Resolve all current active offers at their TTL boundary."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT offer_id, expires_at FROM merchant_offers WHERE customer_id = ? AND status IN ('RESERVED', 'ACCEPTED', 'ORDER_CREATED')",
            (customer_id,),
        ).fetchall()
    finally:
        conn.close()
    expired: list[MerchantOffer] = []
    for row in rows:
        offer = _offer_store.get(row["offer_id"])
        expiry = _parse_account_datetime(row["expires_at"])
        if expiry is None:
            continue
        resolved, did_expire = _offer_store.accept(offer.offer_id, expiry)
        if did_expire:
            _release_reservation(resolved, "reservation_ttl_expired", expiry)
            expired.append(resolved)
    return expired


def _account_value(account: Any, name: str, default: Any = None) -> Any:
    if isinstance(account, Mapping):
        return account.get(name, default)
    return getattr(account, name, default)


def _renewal_triggered(account: Mapping[str, Any]) -> bool:
    try:
        signup = date.fromisoformat(str(account["signup_date"]))
        anniversary = signup.replace(year=AS_OF_DATE.year)
        if anniversary < AS_OF_DATE:
            anniversary = anniversary.replace(year=AS_OF_DATE.year + 1)
        return 0 <= (anniversary - AS_OF_DATE).days <= 30
    except (KeyError, TypeError, ValueError):
        return False


def _has_active_offer(account: Mapping[str, Any]) -> bool:
    for offer in account.get("offer_history", []) or []:
        if offer.get("redeemed") or offer.get("status") in {"expired", "consumed", "redeemed"}:
            continue
        expires = offer.get("expires_at")
        if not expires:
            created = offer.get("created_at")
            if not created:
                continue
            try:
                expires = (datetime.fromisoformat(str(created).replace("Z", "+00:00")) + timedelta(minutes=10)).isoformat()
            except ValueError:
                continue
        try:
            expires_at = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at > AS_OF_DATETIME:
                return True
        except ValueError:
            continue
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM merchant_offers WHERE customer_id = ? AND status IN ('RESERVED','ACCEPTED','ORDER_CREATED') LIMIT 1",
            (account.get("customer_id"),),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _trigger_info(account: Mapping[str, Any]) -> dict[str, bool]:
    contracted = int(account.get("seats_contracted") or 0)
    in_use = int(account.get("seats_in_use") or 0)
    seat_ceiling = contracted > 0 and in_use / contracted >= 0.90
    return {"seat_ceiling": seat_ceiling, "renewal_window": _renewal_triggered(account)}


def _account_summary(account: Mapping[str, Any]) -> dict[str, Any]:
    triggers = _trigger_info(account)
    return {
        "customer_id": account.get("customer_id"),
        "tier": account.get("tier"),
        "seats_contracted": account.get("seats_contracted"),
        "seats_in_use": account.get("seats_in_use"),
        "strata": account.get("strata", []),
        "has_active_offer": _has_active_offer(account),
        "triggered_upsell": triggers["seat_ceiling"] or triggers["renewal_window"],
        "trigger_types": [name for name, value in triggers.items() if value],
        "hostile": str(account.get("customer_id")) in INJECTION_IDS,
    }


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _emit_audit_record(record: Mapping[str, Any], *, pipeline: str | None = None) -> dict[str, Any]:
    payload = dict(record.get("payload", {}))
    if pipeline and "pipeline" not in payload:
        payload["pipeline"] = pipeline
    entry = _audit_event_logger.append(
        timestamp=_now(),
        event_type=str(record["event_type"]),
        actor=str(record["actor"]),
        subject_id=str(record["subject_id"]),
        payload=payload,
    )
    with _audit_condition:
        _audit_condition.notify_all()
    return entry


def _pipeline_audit_writer(record: Mapping[str, Any], *, pipeline: str) -> None:
    """Persist only the event-shaped records; phase snapshots stay in memory."""

    if "event_type" in record:
        _emit_audit_record(record, pipeline=pipeline)


class _TracingModel:
    """Emit UI lifecycle events while delegating unchanged to the real model."""

    def __init__(self, inner: Any, emit: Callable[[str, Any], None]) -> None:
        self.inner = inner
        self.emit = emit

    def complete(self, prompt: str, payload: Mapping[str, Any]) -> Any:
        actual_prompt = (
            self.inner._prompt_with_payload(prompt, payload)
            if hasattr(self.inner, "_prompt_with_payload")
            else prompt
        )
        self.emit("prompt_built", {
            "prompt_text": actual_prompt,
            "allowed_options": payload.get("allowed_options"),
            "discount_band": payload.get("discount_band"),
        })
        self.emit("model_call_started", {})
        started = time_module.perf_counter()
        try:
            response = self.inner.complete(prompt, payload)
        except Exception as exc:
            latency_ms = round((time_module.perf_counter() - started) * 1000, 1)
            self.emit("error", {"stage": "model_call", "message": str(exc), "latency_ms": latency_ms})
            raise
        latency_ms = round((time_module.perf_counter() - started) * 1000, 1)
        raw = response
        log_path = getattr(getattr(self.inner, "config", None), "response_log_path", None)
        if log_path:
            try:
                lines = Path(log_path).read_text(encoding="utf-8").splitlines()
                if lines:
                    raw = json.loads(lines[-1]).get("raw_response", response)
            except (OSError, ValueError, TypeError):
                pass
        self.emit("model_response", {"raw_json": raw, "latency_ms": latency_ms})
        return response


def _new_model() -> GeminiModelClient:
    return GeminiModelClient(ModelClientConfig())


def _offer_terms(proposal: Any, account: Mapping[str, Any]) -> tuple[int, int]:
    discount = Decimal(str(proposal.proposed_discount))
    if proposal.offer_type == "addon":
        addon = growth_catalog.ADD_ONS[proposal.target_addon]
        base_rupees = Decimal(str(addon["price"]))
        quantity = proposal.proposed_seats if addon.get("pricing") == "per_seat" else 1
        quantity = quantity or 1
        price_paise = int((base_rupees * Decimal(quantity) * (Decimal("1") - discount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        discount_paise = int((base_rupees * Decimal(quantity) * discount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return price_paise, discount_paise
    tier = proposal.target_tier or account["tier"]
    seats = proposal.proposed_seats or int(account["seats_contracted"])
    unit_price = growth_catalog.price_for_discount_paise(
        tier, discount, str(account.get("billing_cycle", "monthly")), Decimal(str(account.get("existing_discount", 0)))
    )
    base = Decimal(str(growth_catalog.TIERS[tier]["list_price"])) * Decimal(seats)
    discount_paise = int((base * discount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return unit_price * seats, discount_paise


def _store_approved_offer(proposal: Any, account: Mapping[str, Any], *, campaign_id: str | None = None) -> MerchantOffer:
    price_paise, discount_paise = _offer_terms(proposal, account)
    offer = MerchantOffer(
        offer_id=f"offer-{proposal.customer_id}-{uuid.uuid4().hex}",
        customer_id=proposal.customer_id,
        price_paise=price_paise,
        discount_value_paise=discount_paise,
        campaign_id=campaign_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    _offer_store.create_offer(offer)
    return offer


def _run_upsell_events(customer_id: str, *, bypass_cache: bool, output: queue.Queue[str | None]) -> None:
    def emit(event: str, data: Any) -> None:
        output.put(_sse(event, data))

    try:
        account = next((item for item in _load_accounts() if item.get("customer_id") == customer_id), None)
        if account is None:
            emit("error", {"message": "unknown customer"})
            return
        account = _reconcile_account(account)
        triggers = _trigger_info(account)
        emit("trigger_detection", {"customer_id": customer_id, **triggers, "triggered": any(triggers.values())})
        if not any(triggers.values()):
            emit("done", {"status": "skipped", "reason": "account is not triggered"})
            return
        emit("eligibility_check_started", {"customer_id": customer_id})
        now = AS_OF_DATETIME
        eligibility = filter_account(account, now=now)
        emit("eligibility_result", {"passed": eligibility.eligible, "reason": eligibility.reason, "allowed_options": _jsonable(eligibility.allowed_options), "discount_band": _jsonable(eligibility.discount_band)})
        if not eligibility.eligible:
            emit("done", {"status": "skipped", "reason": eligibility.reason})
            return
        cache_dir = Path(tempfile.mkdtemp(prefix="dashboard-upsell-")) if bypass_cache else GROWTH_ROOT / "tests" / "fixtures"
        model = _TracingModel(_new_model(), emit)
        def upsell_audit_writer(record: Mapping[str, Any]) -> None:
            _pipeline_audit_writer(record, pipeline="upsell")
            if record.get("event_type") == "FIELD_DISCARDED":
                emit("field_discard", record.get("payload", {}))
        proposer = Proposer(
            model,
            config=AgentConfig(cache_dir=cache_dir),
            audit_writer=upsell_audit_writer,
        )
        # Keep the judge-facing timeline causal: the model must finish before
        # the policy stage is announced. The proposer itself is unchanged;
        # dashboard orchestration calls its generation half, then adjudicates.
        proposal = proposer.propose(account, now=now, campaign_state=CampaignState(now=now))
        if proposal is None:
            if proposer.model_failures:
                emit("error", {"stage": "proposal", "message": proposer.model_failures[-1]})
            emit("done", {"status": "error"})
            return
        emit("policy_check_started", {})
        result = check_proposal(proposal, account, campaign_state=CampaignState(now=now))
        _emit_audit_record(
            {
                "event_type": "POLICY_VERDICT",
                "actor": "policy_engine",
                "subject_id": proposal.customer_id,
                "payload": {
                    "verdict": result.verdict,
                    "rules_fired": [item.rule_id for item in result.fired_rules],
                    "rule_reasons": {
                        item.rule_id: item.reason
                        for item in result.fired_rules
                        if item.reason is not None
                    },
                },
            },
            pipeline="upsell",
        )
        emit("policy_verdict", {
            "approved": result.approved,
            "rules_fired": [item.rule_id for item in result.fired_rules],
            "rule_results": [asdict(item) for item in result.rule_results],
        })
        if result.approved:
            offer = _store_approved_offer(proposal, account)
            emit("offer_stored", {"offer_id": offer.offer_id, "price_paise": offer.price_paise})
        emit("done", {"status": result.verdict})
    except Exception as exc:
        LOGGER.exception("dashboard upsell failed for %s", customer_id)
        emit("error", {"stage": "pipeline", "message": str(exc)})
        emit("done", {"status": "error"})
    finally:
        output.put(None)


class ConverseRequest(BaseModel):
    message: str


def _run_converse_events(
    customer_id: str,
    message: str,
    *,
    output: queue.Queue[str | None],
) -> None:
    """Run one customer-initiated turn through the existing offer pipeline."""

    def emit(event: str, data: Any) -> None:
        output.put(_sse(event, data))

    try:
        emit("customer_message_received", {"customer_id": customer_id, "message": message})
        account = next((item for item in _load_accounts() if item.get("customer_id") == customer_id), None)
        if account is None:
            emit("error", {"message": "unknown customer"})
            emit("done", {"status": "error", "reason": "unknown customer"})
            return

        account = _reconcile_account(account)
        emit("eligibility_check_started", {"customer_id": customer_id})
        now = AS_OF_DATETIME
        eligibility = filter_account(account, now=now)
        emit(
            "eligibility_result",
            {
                "passed": eligibility.eligible,
                "reason": eligibility.reason,
                "allowed_options": _jsonable(eligibility.allowed_options),
                "discount_band": _jsonable(eligibility.discount_band),
            },
        )
        if not eligibility.eligible:
            emit("done", {"status": "skipped", "reason": eligibility.reason})
            return

        def converse_audit_writer(record: Mapping[str, Any]) -> None:
            payload = dict(record.get("payload", {}))
            payload["entry_point"] = "customer_initiated"
            enriched = {**record, "payload": payload}
            _pipeline_audit_writer(enriched, pipeline="customer_initiated")
            if record.get("event_type") == "FIELD_DISCARDED":
                emit("field_discard", payload)

        model = _TracingModel(_new_model(), emit)
        proposer = Proposer(
            model,
            # A new directory makes each single turn a fresh model call while
            # preserving the normal ResponseCache implementation.
            config=AgentConfig(cache_dir=Path(tempfile.mkdtemp(prefix="customer-converse-"))),
            audit_writer=converse_audit_writer,
        )
        proposal = proposer.propose(
            account,
            now=now,
            campaign_state=CampaignState(now=now),
            prompt_text=get_customer_initiated_prompt(),
            extra_payload={
                "customer_message": {
                    "label": "UNTRUSTED CUSTOMER INPUT",
                    "text": message,
                }
            },
        )
        if proposal is None:
            if proposer.model_failures:
                emit("error", {"stage": "proposal", "message": proposer.model_failures[-1]})
            emit("done", {"status": "error"})
            return

        emit("policy_check_started", {"customer_id": customer_id})
        result = check_proposal(proposal, account, campaign_state=CampaignState(now=now))
        _emit_audit_record(
            {
                "event_type": "POLICY_VERDICT",
                "actor": "policy_engine",
                "subject_id": proposal.customer_id,
                "payload": {
                    "verdict": result.verdict,
                    "rules_fired": [item.rule_id for item in result.fired_rules],
                    "rule_reasons": {
                        item.rule_id: item.reason
                        for item in result.fired_rules
                        if item.reason is not None
                    },
                    "entry_point": "customer_initiated",
                },
            },
            pipeline="customer_initiated",
        )
        emit(
            "policy_verdict",
            {
                "approved": result.approved,
                "rules_fired": [item.rule_id for item in result.fired_rules],
                "rule_results": [asdict(item) for item in result.rule_results],
            },
        )
        offer_id = None
        if result.approved:
            offer = _store_approved_offer(proposal, account)
            offer_id = offer.offer_id
            emit("offer_stored", {"offer_id": offer.offer_id, "price_paise": offer.price_paise})
        emit(
            "done",
            {
                "status": result.verdict,
                "verdict": result.verdict,
                "proposal": _jsonable(asdict(proposal)),
                "reason": proposal.reason,
                "offer_id": offer_id,
            },
        )
    except Exception as exc:
        LOGGER.exception("customer conversation failed for %s", customer_id)
        emit("error", {"stage": "conversation", "message": str(exc)})
        emit("done", {"status": "error", "reason": str(exc)})
    finally:
        output.put(None)


def _run_campaign_events(*, output: queue.Queue[str | None]) -> None:
    def emit(event: str, data: Any) -> None:
        output.put(_sse(event, data))

    try:
        accounts = _load_accounts()
        emit("segmenter_started", {"scope": "cohort", "target_addon": CAMPAIGN_TARGET_ADDON})
        def audit_writer(record: Mapping[str, Any]) -> None:
            _pipeline_audit_writer(record, pipeline="campaign")
        cohorts = segment_accounts(accounts, target_addon=CAMPAIGN_TARGET_ADDON, audit_writer=audit_writer)
        for cohort in cohorts:
            emit("cohort_formed", {
                "scope": "cohort",
                "cohort_id": cohort.cohort_id,
                "member_count": len(cohort.account_ids),
                "filter": {"seat_band": cohort.seat_band, "tiers": list(cohort.tiers), "target_addon": CAMPAIGN_TARGET_ADDON},
                "member_ids": list(cohort.account_ids),
            })
        manager = ReservationManager(audit_writer=audit_writer)
        manager.register_campaign(CAMPAIGN_ID, CAMPAIGN_BUDGET_PAISE)
        state = CampaignState(campaign_id=CAMPAIGN_ID, allocated_budget_paise=CAMPAIGN_BUDGET_PAISE, now=AS_OF_DATETIME)
        for cohort in cohorts:
            cohort_id = cohort.cohort_id
            emit("strategist_call_started", {"scope": "cohort", "cohort_id": cohort_id})
            cohort = replace(
                cohort,
                accounts=tuple(_reconcile_account(item) for item in cohort.accounts),
            )
            def cohort_audit_writer(record: Mapping[str, Any], cohort_id: str = cohort_id) -> None:
                _pipeline_audit_writer(record, pipeline="campaign")
                if record.get("event_type") == "FIELD_DISCARDED":
                    emit("field_discard", {
                        "scope": "cohort",
                        "cohort_id": cohort_id,
                        **dict(record.get("payload", {})),
                    })

            model = _TracingModel(
                _new_model(),
                lambda event, data, cohort_id=cohort_id: emit(
                    event, {"scope": "cohort", "cohort_id": cohort_id, **data}
                ),
            )
            strategist = Strategist(
                model,
                config=AgentConfig(cache_dir=Path(tempfile.mkdtemp(prefix="dashboard-campaign-"))),
                audit_writer=cohort_audit_writer,
            )
            try:
                strategy = strategist.strategize(cohort, campaign_id=CAMPAIGN_ID, target_addon=CAMPAIGN_TARGET_ADDON, now=AS_OF_DATETIME, campaign_state=state)
            except Exception as exc:
                emit("error", {"scope": "cohort", "stage": "strategist", "cohort_id": cohort_id, "message": str(exc)})
                continue
            if strategy is None:
                emit("error", {"scope": "cohort", "stage": "strategist", "cohort_id": cohort_id, "message": strategist.model_failures[-1] if strategist.model_failures else "strategy unavailable"})
                continue
            emit("strategist_response", {"scope": "cohort", "cohort_id": cohort_id, "raw_json": strategy.raw_model_output, "latency_ms": None})
            emit("expansion_started", {"scope": "cohort", "cohort_id": cohort_id})
            for proposal in strategist.expand(strategy, cohort, target_addon=CAMPAIGN_TARGET_ADDON, now=AS_OF_DATETIME, campaign_state=state):
                member_id = proposal.customer_id
                account = next(item for item in cohort.accounts if item.get("customer_id") == member_id)
                account = _reconcile_account(account)
                member_event = {"scope": "member", "cohort_id": cohort_id, "member_id": member_id}
                emit("eligibility_check_started", member_event)
                emit("policy_check_started", member_event)
                result = check_proposal(proposal, account, campaign_state=state)
                _emit_audit_record(
                    {
                        "event_type": "POLICY_VERDICT",
                        "actor": "policy_engine",
                        "subject_id": member_id,
                        "payload": {
                            "cohort_id": cohort_id,
                            "member_id": member_id,
                            "verdict": result.verdict,
                            "rules_fired": [item.rule_id for item in result.fired_rules],
                            "rule_reasons": {
                                item.rule_id: item.reason
                                for item in result.fired_rules
                                if item.reason is not None
                            },
                        },
                    },
                    pipeline="campaign",
                )
                emit("policy_verdict", {**member_event, "approved": result.approved, "rules_fired": [item.rule_id for item in result.fired_rules], "rule_results": [asdict(item) for item in result.rule_results]})
                if not result.approved:
                    continue
                price_paise, discount_paise = _offer_terms(proposal, account)
                approval = manager.reserve(CAMPAIGN_ID, member_id, price_paise, discount_paise, now=datetime.now(timezone.utc))
                if not approval.approved:
                    emit("policy_verdict", {**member_event, "approved": False, "rules_fired": ["R6"], "rule_results": [{"rule_id": "R6", "passed": False, "reason": approval.reason}]})
                    continue
                offer = _store_approved_offer(proposal, account, campaign_id=CAMPAIGN_ID)
                emit("offer_stored", {**member_event, "offer_id": offer.offer_id, "price_paise": offer.price_paise})
                snapshot = manager.campaign_snapshot(CAMPAIGN_ID)
                emit("budget_updated", {"scope": "cohort", "cohort_id": cohort_id, "reserved_paise": snapshot["held_paise"], "remaining_paise": snapshot["remaining_paise"]})
        emit("done", {"scope": "cohort", "status": "complete"})
    except Exception as exc:
        LOGGER.exception("dashboard campaign failed")
        emit("error", {"scope": "cohort", "stage": "campaign", "message": str(exc)})
        emit("done", {"scope": "cohort", "status": "error"})
    finally:
        output.put(None)


@app.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parent / "static" / "dashboard.html", media_type="text/html")


@app.get("/api/accounts")
def dashboard_accounts() -> list[dict[str, Any]]:
    return [_account_summary(account) for account in _load_accounts()]


@app.get("/api/offers")
def dashboard_offers() -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT o.offer_id, o.customer_id, o.price_paise, o.currency,
                   o.status, o.expires_at, o.razorpay_order_id,
                   (SELECT t.occurred_at
                      FROM offer_transitions AS t
                     WHERE t.offer_id = o.offer_id
                     ORDER BY t.id DESC LIMIT 1) AS timestamp
              FROM merchant_offers AS o
             ORDER BY timestamp DESC, o.rowid DESC
            """
        ).fetchall()
        return [
            {
                "offer_id": row["offer_id"],
                "customer_id": row["customer_id"],
                "price_paise": row["price_paise"],
                "currency": row["currency"],
                "state": row["status"],
                "timestamp": row["timestamp"],
                "expires_at": row["expires_at"],
                "razorpay_order_id": row["razorpay_order_id"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def _stream_thread(target: Callable[..., None], **kwargs: Any) -> StreamingResponse:
    output: queue.Queue[str | None] = queue.Queue()
    thread = threading.Thread(target=target, kwargs={"output": output, **kwargs}, daemon=True)
    thread.start()

    def iterator() -> Iterator[str]:
        while True:
            item = output.get()
            if item is None:
                break
            yield item

    return StreamingResponse(iterator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/run/upsell/{customer_id}")
def dashboard_upsell(customer_id: str, bypass_cache: bool = Query(True)) -> StreamingResponse:
    return _stream_thread(_run_upsell_events, customer_id=customer_id, bypass_cache=bypass_cache)


@app.post("/api/converse/{customer_id}")
def dashboard_converse(customer_id: str, body: ConverseRequest) -> StreamingResponse:
    if not body.message.strip():
        raise HTTPException(422, "message must not be empty")
    return _stream_thread(
        _run_converse_events,
        customer_id=customer_id,
        message=body.message,
    )


@app.post("/api/run/campaign")
def dashboard_campaign() -> StreamingResponse:
    return _stream_thread(_run_campaign_events)


def _audit_rows_since(since_seq: int) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT seq, timestamp, event_type, actor, subject_id, payload_json FROM audit_events WHERE seq > ? ORDER BY seq",
            (since_seq,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@app.get("/api/audit/stream")
def audit_stream(since_seq: int = Query(0, ge=0)) -> StreamingResponse:
    def iterator() -> Iterator[str]:
        cursor = since_seq
        while True:
            rows = _audit_rows_since(cursor)
            if rows:
                for row in rows:
                    cursor = int(row["seq"])
                    yield _sse("audit_event", row)
                continue
            with _audit_condition:
                _audit_condition.wait(timeout=1.0)
            yield ": keep-alive\n\n"

    return StreamingResponse(iterator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/audit/tamper_demo")
def tamper_demo() -> StreamingResponse:
    def iterator() -> Iterator[str]:
        conn = connect()
        step = "selecting middle audit row"
        try:
            row = conn.execute("SELECT * FROM audit_events ORDER BY seq LIMIT 1 OFFSET (SELECT COUNT(*) / 2 FROM audit_events)").fetchone()
            if row is None:
                yield _sse("error", {"message": "audit chain is empty"})
                return
            original = dict(row)
            yield _sse("tamper_planned", {"seq": row["seq"], "original_payload": row["payload_json"]})
            step = "tamper transaction"
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
            conn.execute("UPDATE audit_events SET payload_json = ? WHERE seq = ?", (json.dumps({"tampered": True}), row["seq"]))
            conn.commit()
            step = "verifying tampered chain"
            yield _sse("chain_verified", {"result": verify_audit_chain()})
            step = "restore transaction"
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
            conn.execute("UPDATE audit_events SET payload_json = ? WHERE seq = ?", (original["payload_json"], row["seq"]))
            conn.execute("""CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                BEFORE UPDATE ON audit_events BEGIN
                    SELECT RAISE(ABORT, 'audit_events is immutable');
                END""")
            conn.commit()
            step = "verifying restored chain"
            yield _sse("chain_verified", {"result": verify_audit_chain()})
            yield _sse("done", {"status": "complete"})
        except Exception as exc:
            LOGGER.exception("audit tamper demo failed during %s", step)
            try:
                conn.rollback()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("""CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                    BEFORE UPDATE ON audit_events BEGIN
                        SELECT RAISE(ABORT, 'audit_events is immutable');
                    END""")
                conn.commit()
            except Exception:
                LOGGER.exception("audit tamper demo trigger recovery failed after %s", step)
                conn.rollback()
            yield _sse("error", {"message": f"{step}: {exc}"})
        finally:
            conn.close()

    return StreamingResponse(iterator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/metrics")
def dashboard_metrics() -> dict[str, Any]:
    conn = connect()
    try:
        rows = conn.execute("SELECT event_type, subject_id, payload_json FROM audit_events ORDER BY seq").fetchall()
        proposals = approved = rejected = paid = reserved = paid_paise = 0
        discards: dict[str, int] = {}
        by_pipeline: dict[str, dict[str, int]] = {"upsell": {"proposals": 0, "approved": 0, "rejected": 0, "paid": 0}, "campaign": {"proposals": 0, "approved": 0, "rejected": 0, "paid": 0}}
        injection: dict[str, dict[str, int]] = {"injection": {"proposals": 0, "approved": 0, "rejected": 0, "paid": 0}, "other": {"proposals": 0, "approved": 0, "rejected": 0, "paid": 0}}
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except ValueError:
                payload = {}
            pipeline = payload.get("pipeline") or ("campaign" if payload.get("campaign_id") else "upsell")
            if row["event_type"] in {"OFFER_STORED", "OFFER_PAID"} and payload.get("offer_id"):
                offer_row = conn.execute(
                    "SELECT campaign_id FROM merchant_offers WHERE offer_id = ?",
                    (payload["offer_id"],),
                ).fetchone()
                pipeline = "campaign" if offer_row and offer_row[0] else "upsell"
            if pipeline not in by_pipeline:
                pipeline = "upsell"
            audience = "injection" if row["subject_id"] in INJECTION_IDS else "other"
            if row["event_type"] == "PROPOSAL_GENERATED":
                proposals += 1; by_pipeline[pipeline]["proposals"] += 1; injection[audience]["proposals"] += 1
            elif row["event_type"] == "POLICY_VERDICT":
                if payload.get("verdict") == "approved":
                    approved += 1; by_pipeline[pipeline]["approved"] += 1; injection[audience]["approved"] += 1
                else:
                    rejected += 1; by_pipeline[pipeline]["rejected"] += 1; injection[audience]["rejected"] += 1
            elif row["event_type"] == "FIELD_DISCARDED":
                field = str(payload.get("field", "unknown")); discards[field] = discards.get(field, 0) + 1
            elif row["event_type"] == "RESERVATION_CREATED":
                reserved += int(payload.get("amount_paise", 0) or 0)
            elif row["event_type"] == "OFFER_PAID":
                paid += 1; by_pipeline[pipeline]["paid"] += 1; injection[audience]["paid"] += 1
                amount = conn.execute("SELECT price_paise FROM merchant_offers WHERE offer_id = ?", (payload.get("offer_id"),)).fetchone()
                paid_paise += int(amount[0]) if amount else 0
        return {
            "total_proposals": proposals,
            "total_approved": approved,
            "total_rejected": rejected,
            "total_paid": paid,
            "total_field_discards": discards,
            "total_budget_reserved_paise": reserved,
            "total_paid_paise": paid_paise,
            "by_pipeline": by_pipeline,
            "by_injection_subset": injection,
            "hash_chain": verify_audit_chain(),
        }
    finally:
        conn.close()
