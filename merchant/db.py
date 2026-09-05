import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "merchant.sqlite3"
SCHEMA = """
CREATE TABLE sellers (id TEXT PRIMARY KEY, name TEXT NOT NULL, catalog_url TEXT NOT NULL);
CREATE TABLE products (id TEXT PRIMARY KEY, seller_id TEXT NOT NULL REFERENCES sellers(id), sku TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, price_minor INTEGER NOT NULL CHECK(price_minor >= 0), currency TEXT NOT NULL, gst_rate_bps INTEGER NOT NULL CHECK(gst_rate_bps >= 0), max_per_order INTEGER NOT NULL CHECK(max_per_order > 0), returnable INTEGER NOT NULL CHECK(returnable IN (0,1)), return_window_days INTEGER NOT NULL CHECK(return_window_days >= 0));
CREATE TABLE inventory (product_id TEXT PRIMARY KEY REFERENCES products(id), on_hand INTEGER NOT NULL CHECK(on_hand >= 0));
CREATE TABLE reservations (id TEXT PRIMARY KEY, quote_id TEXT NOT NULL, product_id TEXT NOT NULL REFERENCES products(id), qty INTEGER NOT NULL CHECK(qty > 0), state TEXT NOT NULL CHECK(state IN ('HELD','CONSUMED','RELEASED')), expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE quotes (id TEXT PRIMARY KEY, product_id TEXT NOT NULL REFERENCES products(id), qty INTEGER NOT NULL, unit_price_minor INTEGER NOT NULL, tax_minor INTEGER NOT NULL, shipping_minor INTEGER NOT NULL, total_minor INTEGER NOT NULL, currency TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('OPEN','CLAIMED','EXPIRED','CANCELLED')), expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL, signature TEXT NOT NULL, buyer_agent_id TEXT NOT NULL);
CREATE TABLE orders (id TEXT PRIMARY KEY, quote_id TEXT NOT NULL REFERENCES quotes(id), product_id TEXT NOT NULL REFERENCES products(id), qty INTEGER NOT NULL, total_minor INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('PENDING','PAID','FAILED','CANCELLED')), created_at INTEGER NOT NULL);
CREATE TABLE payments (id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), gateway_order_id TEXT, gateway_payment_id TEXT, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL, method TEXT, error_code TEXT, error_description TEXT, created_at INTEGER NOT NULL);
CREATE TABLE idempotency_keys (key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('IN_PROGRESS','COMPLETE')), response_json TEXT, order_id TEXT, created_at INTEGER NOT NULL);
CREATE TABLE audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);
CREATE TRIGGER audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is immutable');
END;
CREATE TRIGGER audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is immutable');
END;
CREATE TABLE policy_decisions (id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, rule TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    offer_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    details TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);
CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is immutable');
END;
CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is immutable');
END;
CREATE TABLE merchant_offers (
    offer_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    price_paise INTEGER NOT NULL CHECK(price_paise >= 0),
    discount_value_paise INTEGER NOT NULL CHECK(discount_value_paise >= 0),
    campaign_id TEXT,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('RESERVED','ACCEPTED','ORDER_CREATED','PAID','FAILED','EXPIRED')),
    currency TEXT NOT NULL,
    razorpay_order_id TEXT,
    failure_reason TEXT
);
CREATE TABLE offer_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id TEXT NOT NULL REFERENCES merchant_offers(offer_id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    reason TEXT
);
CREATE TABLE offer_payment_callbacks (
    payment_id TEXT PRIMARY KEY,
    offer_id TEXT NOT NULL REFERENCES merchant_offers(offer_id),
    order_id TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    currency TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE offer_payment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id TEXT,
    payment_id TEXT,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE campaign_reservation_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id TEXT NOT NULL UNIQUE,
    campaign_id TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    released_at TEXT NOT NULL,
    reason TEXT NOT NULL
);
"""


def available(conn: sqlite3.Connection, product_id: str, now: int) -> int:
    row = conn.execute("SELECT on_hand FROM inventory WHERE product_id = ?", (product_id,)).fetchone()
    if row is None:
        raise KeyError(product_id)
    held = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM reservations WHERE product_id = ? AND state = 'HELD' AND expires_at > ?", (product_id, now)).fetchone()[0]
    return row[0] - held


def reset_and_seed(path: Path = DB_PATH) -> None:
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    seed = json.loads((ROOT / "data/seed.json").read_text())
    conn.executemany("INSERT INTO sellers VALUES (:id,:name,:catalog_url)", seed["sellers"])
    conn.executemany("INSERT INTO products VALUES (:id,:seller_id,:sku,:title,:description,:price_minor,:currency,:gst_rate_bps,:max_per_order,:returnable,:return_window_days)", seed["products"])
    conn.executemany("INSERT INTO inventory VALUES (:id,:on_hand)", [{"id": p["id"], "on_hand": p["on_hand"]} for p in seed["products"]])
    conn.commit()
    conn.close()


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    existing_audit = conn.execute("PRAGMA table_info(audit_events)").fetchall()
    if existing_audit:
        existing_columns = {row[1] for row in existing_audit}
        required_columns = {
            "seq", "timestamp", "event_type", "actor", "subject_id",
            "payload_json", "prev_hash", "row_hash",
        }
        if existing_columns != required_columns:
            legacy_name = "audit_events_legacy"
            suffix = 1
            while conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (legacy_name,),
            ).fetchone():
                suffix += 1
                legacy_name = f"audit_events_legacy_{suffix}"
            conn.execute(f"ALTER TABLE audit_events RENAME TO {legacy_name}")
    # This runs on every fresh connection and intentionally re-arms audit
    # immutability triggers if a maintenance operation temporarily removes one.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            row_hash TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS audit_events_no_update
        BEFORE UPDATE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit_events is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
        BEFORE DELETE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit_events is immutable');
        END;
        CREATE TABLE IF NOT EXISTS merchant_offers (
            offer_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
            price_paise INTEGER NOT NULL CHECK(price_paise >= 0),
            discount_value_paise INTEGER NOT NULL CHECK(discount_value_paise >= 0),
            campaign_id TEXT, expires_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('RESERVED','ACCEPTED','ORDER_CREATED','PAID','FAILED','EXPIRED')),
            currency TEXT NOT NULL, razorpay_order_id TEXT, failure_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS offer_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, offer_id TEXT NOT NULL,
            from_status TEXT, to_status TEXT NOT NULL, occurred_at TEXT NOT NULL, reason TEXT
        );
        CREATE TABLE IF NOT EXISTS offer_payment_callbacks (
            payment_id TEXT PRIMARY KEY, offer_id TEXT NOT NULL, order_id TEXT NOT NULL,
            amount_paise INTEGER NOT NULL, currency TEXT NOT NULL, recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS offer_payment_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, offer_id TEXT, payment_id TEXT,
            event_type TEXT NOT NULL, detail TEXT NOT NULL, occurred_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS campaign_reservation_releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT, offer_id TEXT NOT NULL UNIQUE,
            campaign_id TEXT NOT NULL, amount_paise INTEGER NOT NULL,
            released_at TEXT NOT NULL, reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            offer_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            details TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS audit_log_no_update
        BEFORE UPDATE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'audit_log is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
        BEFORE DELETE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'audit_log is immutable');
        END;
        """
    )
    conn.commit()
    return conn
