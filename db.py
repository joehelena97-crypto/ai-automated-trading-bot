"""
SQLite persistence: idempotency keys and order history.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from config import settings


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _get_conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS idempotency (
                key TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_key TEXT,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                size_usdt REAL NOT NULL,
                leverage INTEGER,
                order_id TEXT,
                success INTEGER NOT NULL,
                message TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS risk_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_sessions (
                nonce TEXT PRIMARY KEY,
                expires_at REAL NOT NULL,
                revoked_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry ON admin_sessions(expires_at);
            CREATE TABLE IF NOT EXISTS rate_limit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket TEXT NOT NULL,
                occurred_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket_time
                ON rate_limit_events(bucket, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_rate_limit_time
                ON rate_limit_events(occurred_at);
            CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_orders_exchange_symbol ON orders(exchange, symbol);
        """)


@contextmanager
def get_cursor():
    conn = _get_conn()
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


def idempotency_seen(key: str) -> bool:
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM idempotency WHERE key = ?", (key,))
        return cur.fetchone() is not None


def idempotency_set(key: str, ttl_seconds: int) -> None:
    now = time.time()
    with get_cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO idempotency (key, created_at) VALUES (?, ?)",
            (key, now),
        )
    # Prune old keys (simple: delete older than ttl)
    with get_cursor() as cur:
        cur.execute("DELETE FROM idempotency WHERE created_at < ?", (now - ttl_seconds,))


def save_order(
    request_key: Optional[str],
    exchange: str,
    symbol: str,
    side: str,
    size_usdt: float,
    leverage: Optional[int],
    order_id: Optional[str],
    success: bool,
    message: str = "",
) -> None:
    with get_cursor() as cur:
        cur.execute(
            """INSERT INTO orders (request_key, exchange, symbol, side, size_usdt, leverage, order_id, success, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (request_key, exchange, symbol, side, size_usdt, leverage, order_id, 1 if success else 0, message, time.time()),
        )


def get_recent_orders(limit: int = 50) -> List[Dict[str, Any]]:
    with get_cursor() as cur:
        cur.execute(
            """SELECT request_key, exchange, symbol, side, size_usdt, leverage, order_id, success, message, created_at
               FROM orders ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def risk_state_get(key: str) -> Optional[str]:
    with get_cursor() as cur:
        cur.execute("SELECT value FROM risk_state WHERE key = ?", (key,))
        row = cur.fetchone()
    return row["value"] if row else None


def risk_state_set(key: str, value: str) -> None:
    with get_cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO risk_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, time.time()),
        )


def create_admin_session(nonce: str, expires_at: float) -> None:
    with get_cursor() as cur:
        cur.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (time.time(),))
        cur.execute(
            "INSERT INTO admin_sessions (nonce, expires_at, revoked_at) VALUES (?, ?, NULL)",
            (nonce, expires_at),
        )


def revoke_admin_session(nonce: str) -> None:
    with get_cursor() as cur:
        cur.execute(
            "UPDATE admin_sessions SET revoked_at = ? WHERE nonce = ? AND revoked_at IS NULL",
            (time.time(), nonce),
        )


def admin_session_is_active(nonce: str, now: float) -> bool:
    with get_cursor() as cur:
        cur.execute(
            """SELECT 1 FROM admin_sessions
               WHERE nonce = ? AND expires_at > ? AND revoked_at IS NULL""",
            (nonce, now),
        )
        return cur.fetchone() is not None


def consume_rate_limit(bucket: str, max_requests: int, window_seconds: float, now: float) -> bool:
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM rate_limit_events WHERE occurred_at <= ?",
            (now - max(window_seconds, 60.0),),
        )
        conn.execute(
            "DELETE FROM rate_limit_events WHERE bucket = ? AND occurred_at <= ?",
            (bucket, now - window_seconds),
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_events WHERE bucket = ?",
            (bucket,),
        ).fetchone()[0]
        if count >= max_requests:
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO rate_limit_events (bucket, occurred_at) VALUES (?, ?)",
            (bucket, now),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
