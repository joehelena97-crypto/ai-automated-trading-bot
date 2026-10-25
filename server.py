"""
FastAPI webhook server for TradingView alerts.
Idempotency, rate limit, risk (daily PnL, exposure, cooldown, spread), dry-run, metrics, health, status.
"""
from __future__ import annotations

import asyncio
import base64
import ccxt
import hmac
import logging
import math
import secrets
import time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from config import settings
from db import (
    admin_session_is_active,
    create_admin_session,
    get_recent_orders,
    idempotency_seen,
    idempotency_set,
    init_db,
    revoke_admin_session,
    risk_state_get,
    risk_state_set,
    save_order,
)
from exchanges.registry import SUPPORTED_EXCHANGES, get_exchange, get_all_exchanges, reset_all_clients
from runtime_settings import get_effective_use_testnet, set_use_testnet_override
from logging_config import setup_logging
from metrics import get_all as get_metrics, inc
from models import WebhookPayload, Side, ExchangeName
from rate_limit import RateLimiter, get_rate_limiter
from ai.enhancer import enhancer_process
from ai.advisor import advisor_review
from ai.signals import generate_signal
from risk import run_risk_checks
from tpsl import amount_base_from_size_usdt, set_tp_sl_after_order
from retry import with_retry
from trailing_stop import add_trailing_pending, trailing_stop_loop

setup_logging(use_json=True)
logger = logging.getLogger(__name__)

COOLDOWN_KEY = "cooldown_until_ts"
_trailing_task: Optional[asyncio.Task] = None
_exchange_connected: dict[str, bool] = {}
_health_rate_limiter = RateLimiter(60, "exchange-health")
_admin_login_rate_limiter = RateLimiter(5, "admin-login")
_signals_rate_limiter = RateLimiter(settings.rate_limit_per_minute, "signals")
_ADMIN_SESSION_COOKIE = "admin_session"
_ADMIN_SESSION_MAX_AGE = 3600


def _ensure_exchange_connected() -> None:
    if _exchange_connected:
        return
    for name in get_all_exchanges():
        _exchange_connected.setdefault(name, True)


def _balance_for_exchange(exchange_name: str) -> Optional[float]:
    ex = get_exchange(exchange_name)
    if not ex:
        return None
    try:
        client = ex._get_client()
        bal = client.fetch_balance()
        usdt = (bal.get("USDT") or bal.get("usdt")) or {}
        total = usdt.get("total")
        if total is not None:
            return float(total)
        return None
    except Exception:
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _trailing_task
    settings.validate_production_secrets()
    init_db()
    logger.info("TradingView Webhook Bot starting", extra={"dry_run": settings.dry_run})
    _trailing_task = asyncio.create_task(trailing_stop_loop())
    yield
    if _trailing_task:
        _trailing_task.cancel()
        try:
            await _trailing_task
        except asyncio.CancelledError:
            pass
    logger.info("Shutting down")


app = FastAPI(title="TradingView Webhook Bot", lifespan=lifespan)

_DASHBOARD_PATH = Path(__file__).resolve().parent / "static" / "dashboard.html"


def _require_https(request: Request) -> None:
    if request.url.scheme != "https":
        raise HTTPException(status_code=400, detail="HTTPS is required for admin access")


def _admin_key_configured() -> bool:
    return (
        len(settings.admin_api_key.strip()) >= 32
        and not settings._is_placeholder_secret(settings.admin_api_key)
    )


def _admin_session_signature(payload: str) -> str:
    signature = hmac.new(
        settings.admin_api_key.encode(),
        b"admin-session:" + payload.encode(),
        digestmod="sha256",
    ).digest()
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def _new_admin_session() -> tuple[str, str]:
    issued_at = int(time.time())
    nonce = secrets.token_urlsafe(32)
    payload = f"{issued_at}.{nonce}"
    create_admin_session(nonce, issued_at + _ADMIN_SESSION_MAX_AGE)
    return nonce, f"{payload}.{_admin_session_signature(payload)}"


def _valid_admin_session(token: str | None) -> bool:
    if not token or not _admin_key_configured():
        return False
    try:
        issued_at_text, nonce, signature = token.split(".", maxsplit=2)
        issued_at = int(issued_at_text)
    except (ValueError, TypeError):
        return False
    now = int(time.time())
    if not nonce or issued_at > now + 60 or now - issued_at > _ADMIN_SESSION_MAX_AGE:
        return False
    payload = f"{issued_at_text}.{nonce}"
    return (
        hmac.compare_digest(signature, _admin_session_signature(payload))
        and admin_session_is_active(nonce, now)
    )


def _admin_session_nonce(token: str) -> str | None:
    try:
        issued_at_text, nonce, _signature = token.split(".", maxsplit=2)
        int(issued_at_text)
        return nonce
    except (ValueError, TypeError):
        return None


def _require_admin(request: Request) -> None:
    _require_https(request)
    if not _admin_key_configured():
        raise HTTPException(status_code=503, detail="Admin access is not configured")
    if not _valid_admin_session(request.cookies.get(_ADMIN_SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="Admin authorization required")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Same-origin request required")


@app.middleware("http")
async def prevent_admin_response_caching(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in {
        "/admin/session",
        "/admin/session/logout",
        "/signals/generate",
        "/exchanges/status",
        "/balance",
        "/metrics",
        "/status",
        "/settings/network",
    }:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=RedirectResponse)
async def root():
    """Redirect root to dashboard."""
    return RedirectResponse(url="/dashboard", status_code=302)


@app.get("/dashboard", response_class=FileResponse)
async def dashboard():
    """Professional dashboard: exchange status, metrics, daily PnL, recent orders."""
    if not _DASHBOARD_PATH.exists():
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return FileResponse(_DASHBOARD_PATH, media_type="text/html")


@app.get("/admin/login")
async def admin_login_page(request: Request):
    _require_https(request)
    if not _admin_key_configured():
        raise HTTPException(status_code=503, detail="Admin access is not configured")

    response = HTMLResponse(
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Admin sign in</title>
  <meta name="referrer" content="no-referrer">
  <style>
    body { font: 16px system-ui, sans-serif; margin: 3rem auto; max-width: 28rem; padding: 0 1rem; }
    input, button { box-sizing: border-box; font: inherit; margin-top: 0.75rem; padding: 0.65rem; width: 100%; }
  </style>
</head>
<body>
  <main>
    <h1>Admin sign in</h1>
    <p>Use this page only over HTTPS.</p>
    <form method="post" action="/admin/session" autocomplete="on">
      <label for="api_key">Admin API key</label>
      <input id="api_key" name="api_key" type="password" autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
    <p><a href="/dashboard">Return to dashboard</a></p>
  </main>
</body>
</html>"""
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/admin/session")
async def admin_session(request: Request):
    _require_https(request)
    if not _admin_key_configured():
        raise HTTPException(status_code=503, detail="Admin access is not configured")
    client_ip = request.client.host if request.client else "unknown"
    if not _admin_login_rate_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="Admin login rate limit exceeded")
    if request.headers.get("origin") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Same-origin request required")

    content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Form-encoded credentials required")
    content_length = request.headers.get("content-length")
    if content_length and (not content_length.isdigit() or int(content_length) > 4096):
        raise HTTPException(status_code=413, detail="Login request is too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            raise HTTPException(status_code=413, detail="Login request is too large")
    try:
        form = parse_qs(bytes(body).decode("utf-8"), keep_blank_values=True, strict_parsing=True)
        credentials = form.get("api_key", [])
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid login form")
    if len(credentials) != 1:
        raise HTTPException(status_code=400, detail="Invalid login form")
    if not hmac.compare_digest(credentials[0], settings.admin_api_key):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")

    _nonce, session_token = _new_admin_session()
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        _ADMIN_SESSION_COOKIE,
        session_token,
        max_age=_ADMIN_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/admin/session/logout")
async def admin_session_logout(request: Request):
    _require_admin(request)
    token = request.cookies.get(_ADMIN_SESSION_COOKIE)
    nonce = _admin_session_nonce(token) if token else None
    if nonce:
        revoke_admin_session(nonce)
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(
        _ADMIN_SESSION_COOKIE,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


def _verify_secret(payload: WebhookPayload) -> bool:
    if not settings.webhook_secret:
        return not settings.require_webhook_secret
    if settings._is_placeholder_secret(settings.webhook_secret):
        return False
    if not payload.secret or settings._is_placeholder_secret(payload.secret):
        return False
    return hmac.compare_digest(payload.secret, settings.webhook_secret)


def _daily_pnl_for_exchange(exchange_name: str) -> float:
    ex = get_exchange(exchange_name)
    if not ex:
        return 0.0
    try:
        return ex.fetch_daily_pnl_usdt()
    except Exception:
        return 0.0


def _exposure_for_exchange(exchange_name: str) -> float:
    ex = get_exchange(exchange_name)
    if not ex:
        return 0.0
    try:
        return ex.fetch_total_exposure_usdt()
    except Exception:
        return 0.0


def _exposure_by_symbol(exchange_name: str) -> dict[str, float]:
    """Return symbol -> notional USDT for open positions."""
    ex = get_exchange(exchange_name)
    if not ex:
        return {}
    try:
        client = ex._get_client()
        positions = client.fetch_positions()
        out: dict[str, float] = {}
        for p in positions or []:
            sym = (p.get("symbol") or p.get("symbolId") or "").replace("/", "").upper()
            if not sym:
                continue
            notional = abs(float(p.get("notional", 0) or 0))
            if notional <= 0 and p.get("contracts"):
                notional = abs(float(p.get("contracts", 0) or 0) * float(p.get("markPrice") or p.get("last", 0) or 0))
            if sym not in out:
                out[sym] = 0.0
            out[sym] += notional
        return out
    except Exception:
        return {}


def _cooldown_active() -> bool:
    raw = risk_state_get(COOLDOWN_KEY)
    if not raw:
        return False
    try:
        return time.time() < float(raw)
    except Exception:
        return False


def _execute_payload(payload: WebhookPayload, request_key: str) -> tuple[int, dict]:
    """
    Run risk checks, optional advisor, then place order (or dry-run). Returns (status_code, content_dict).
    """
    _ensure_exchange_connected()
    if not _exchange_connected.get(payload.exchange.value, True):
        return 400, {"ok": False, "error": f"Exchange {payload.exchange.value} is disconnected"}
    exchange = get_exchange(payload.exchange.value)
    if not exchange:
        return 400, {"ok": False, "error": f"Exchange {payload.exchange.value} not enabled or configured"}

    daily_pnl = _daily_pnl_for_exchange(payload.exchange.value)
    total_exposure = _exposure_for_exchange(payload.exchange.value)
    _by_sym = _exposure_by_symbol(payload.exchange.value)
    exposure_by_symbol = lambda s: _by_sym.get(s, 0.0)
    cooldown_active = _cooldown_active()

    spread_bid, spread_ask, spread_mid = 0.0, 0.0, 0.0
    if settings.max_spread_pct > 0:
        spread_bid, spread_ask, spread_mid = exchange.get_orderbook(payload.symbol_upper(), 5)

    risk_result = run_risk_checks(
        payload,
        daily_pnl_usdt=daily_pnl,
        current_total_exposure_usdt=total_exposure,
        current_exposure_by_symbol=exposure_by_symbol,
        cooldown_active=cooldown_active,
        spread_bid=spread_bid,
        spread_ask=spread_ask,
        spread_mid=spread_mid,
    )
    if not risk_result.ok:
        inc("webhooks_risk_rejected")
        if "Daily loss limit" in risk_result.message and settings.cooldown_after_loss_minutes > 0:
            cooldown_until = time.time() + settings.cooldown_after_loss_minutes * 60
            risk_state_set(COOLDOWN_KEY, str(cooldown_until))
        return 400, {"ok": False, "error": risk_result.message}

    size_usdt = risk_result.adjusted_size_usdt or payload.size_usdt
    leverage = risk_result.adjusted_leverage

    current_price = exchange.get_last_price(payload.symbol_upper()) or 1.0
    advisor_result = advisor_review(payload, size_usdt, leverage, current_price)
    if not advisor_result.approve:
        return 400, {"ok": False, "error": f"Advisor rejected: {advisor_result.reason}"}
    if advisor_result.modify:
        if "size_usdt" in advisor_result.modify and advisor_result.modify["size_usdt"] is not None:
            size_usdt = min(float(advisor_result.modify["size_usdt"]), size_usdt)
        if "leverage" in advisor_result.modify and advisor_result.modify["leverage"] is not None:
            leverage = int(advisor_result.modify["leverage"])
        if "take_profit" in advisor_result.modify or "stop_loss" in advisor_result.modify:
            updates = {}
            if advisor_result.modify.get("take_profit") is not None:
                updates["take_profit"] = float(advisor_result.modify["take_profit"])
            if advisor_result.modify.get("stop_loss") is not None:
                updates["stop_loss"] = float(advisor_result.modify["stop_loss"])
            if updates:
                payload = payload.model_copy(update=updates)

    if settings.dry_run:
        inc("orders_dry_run")
        idempotency_set(request_key, settings.idempotency_ttl_seconds)
        return 200, {"ok": True, "dry_run": True, "size_usdt": size_usdt, "leverage": leverage}

    def _place():
        return exchange.place_from_payload(payload, size_usdt=size_usdt, leverage=leverage)

    try:
        order_result = with_retry(_place)
    except Exception as e:
        inc("orders_failed")
        inc("errors")
        save_order(request_key, payload.exchange.value, payload.symbol_upper(), payload.side.value, size_usdt, leverage, None, False, str(e))
        return 502, {"ok": False, "error": str(e)}

    if not order_result.success:
        inc("orders_failed")
        save_order(request_key, payload.exchange.value, payload.symbol_upper(), payload.side.value, size_usdt, leverage, order_result.order_id, False, order_result.message)
        return 502, {"ok": False, "error": order_result.message, "order_id": order_result.order_id}

    inc("orders_placed")
    idempotency_set(request_key, settings.idempotency_ttl_seconds)
    save_order(request_key, payload.exchange.value, payload.symbol_upper(), payload.side.value, size_usdt, leverage, order_result.order_id, True, order_result.message)

    if payload.take_profit or payload.stop_loss:
        try:
            price = exchange.get_last_price(payload.symbol_upper()) or 1.0
            amount_base = amount_base_from_size_usdt(size_usdt, price)
            tpsl_result = set_tp_sl_after_order(
                exchange, payload.symbol_upper(), payload.side.value, amount_base,
                payload.take_profit, payload.stop_loss,
            )
            if tpsl_result.success:
                inc("tpsl_placed")
            else:
                inc("tpsl_failed")
        except Exception:
            inc("tpsl_failed")

    if payload.trailing_stop and payload.trailing_activation_pct is not None:
        try:
            entry_price = exchange.get_last_price(payload.symbol_upper())
            if entry_price > 0:
                add_trailing_pending(
                    payload.exchange.value, payload.symbol_upper(), payload.side.value,
                    entry_price, payload.trailing_activation_pct, size_usdt,
                )
        except Exception:
            pass

    return 200, {
        "ok": True,
        "order_id": order_result.order_id,
        "message": order_result.message,
        "size_usdt": size_usdt,
        "leverage": leverage,
    }


@app.get("/health")
async def health():
    """Basic liveness."""
    return {"status": "ok"}


@app.get("/health/exchanges")
def health_exchanges(request: Request):
    """Public, read-only exchange connectivity without account data."""
    client_ip = request.client.host if request.client else "unknown"
    if not _health_rate_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="Health check rate limit exceeded")

    try:
        exchanges = get_all_exchanges()
    except Exception:
        logger.error("Exchange registry initialization failed")
        exchanges = {}

    result: dict[str, str] = {}
    for name in SUPPORTED_EXCHANGES:
        if not settings.exchange_enabled(name):
            result[name] = "unconfigured"
            continue

        exchange = exchanges.get(name)
        if exchange is None:
            result[name] = "unavailable"
            logger.warning("Configured exchange is missing from registry", extra={"exchange": name})
            continue

        try:
            timestamp = exchange._get_client().fetch_time()
            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp)
                or timestamp <= 0
            ):
                result[name] = "degraded"
                logger.warning("Exchange health check returned malformed data", extra={"exchange": name})
            else:
                result[name] = "healthy"
        except ccxt.RequestTimeout:
            result[name] = "degraded"
            logger.warning("Exchange health check timed out", extra={"exchange": name})
        except Exception:
            result[name] = "unavailable"
            logger.warning("Exchange health check failed", extra={"exchange": name})

    configured_statuses = [state for state in result.values() if state != "unconfigured"]
    if not configured_statuses:
        overall = "unconfigured"
    elif all(state == "healthy" for state in configured_statuses):
        overall = "healthy"
    elif any(state == "healthy" for state in configured_statuses):
        overall = "degraded"
    else:
        overall = "unavailable"
    status_code = 200 if overall in ("healthy", "degraded") else 503
    return JSONResponse(status_code=status_code, content={"status": overall, "exchanges": result})


@app.get("/exchanges/status", dependencies=[Depends(_require_admin)])
async def exchanges_status():
    """Which exchanges are connected (allowed to receive orders)."""
    _ensure_exchange_connected()
    return dict(_exchange_connected)


@app.put("/exchanges/status", dependencies=[Depends(_require_admin)])
async def exchanges_status_update(request: Request):
    """Set connect/disconnect per exchange. Body: { \"binance\": true, \"bybit\": false, ... }."""
    _ensure_exchange_connected()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a map of exchange -> boolean")
    for name, connected in body.items():
        if not isinstance(name, str) or name.lower() not in _exchange_connected:
            raise HTTPException(status_code=400, detail="Unknown exchange")
        if not isinstance(connected, bool):
            raise HTTPException(status_code=400, detail="Exchange status must be a boolean")
    for name, connected in body.items():
        _exchange_connected[name.lower()] = connected
    return {"exchanges": dict(_exchange_connected)}


@app.get("/balance", dependencies=[Depends(_require_admin)])
async def balance():
    """USDT balance per exchange (total from fetch_balance). None if unavailable."""
    _ensure_exchange_connected()
    result = {}
    for name in get_all_exchanges():
        if _exchange_connected.get(name, True):
            result[name] = _balance_for_exchange(name)
        else:
            result[name] = None
    return result


@app.get("/metrics", dependencies=[Depends(_require_admin)])
async def metrics():
    """Observability: counters."""
    return get_metrics()


@app.get("/status", dependencies=[Depends(_require_admin)])
async def status():
    """Admin: recent orders, daily PnL hint, risk state."""
    orders = get_recent_orders(limit=20)
    daily_pnl_by_exchange = {}
    for name in get_all_exchanges():
        daily_pnl_by_exchange[name] = _daily_pnl_for_exchange(name)
    cooldown = _cooldown_active()
    return {
        "recent_orders": orders,
        "daily_pnl_by_exchange": daily_pnl_by_exchange,
        "cooldown_active": cooldown,
        "dry_run": settings.dry_run,
        "use_testnet": get_effective_use_testnet(),
    }


@app.put("/settings/network", dependencies=[Depends(_require_admin)])
async def settings_network(request: Request):
    """Switch all exchanges between live and testnet. Body: { "use_testnet": true | false }."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(body, dict) or not isinstance(body.get("use_testnet"), bool):
        raise HTTPException(status_code=400, detail="use_testnet must be a boolean")
    use_testnet = body["use_testnet"]
    set_use_testnet_override(use_testnet)
    reset_all_clients()
    return {"ok": True, "use_testnet": use_testnet}


@app.post("/webhook")
async def webhook(request: Request):
    """Receive TradingView webhook JSON and execute trade with risk checks."""
    inc("webhooks_received")
    client_ip = request.client.host if request.client else "unknown"
    if not get_rate_limiter().allow(client_ip):
        inc("webhooks_rate_limited")
        return JSONResponse(status_code=429, content={"ok": False, "error": "Rate limit exceeded"})

    try:
        body = await request.json()
    except Exception as e:
        inc("webhooks_validation_error")
        inc("errors")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        payload = WebhookPayload.model_validate(body)
    except Exception as e:
        inc("webhooks_validation_error")
        inc("errors")
        raise HTTPException(status_code=422, detail="Invalid payload")

    request_key = payload.idempotency_key()
    if idempotency_seen(request_key):
        inc("webhooks_duplicate")
        return JSONResponse(status_code=200, content={"ok": True, "duplicate": True, "message": "Already processed"})

    if not _verify_secret(payload):
        inc("webhooks_auth_failed")
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    # AI enhancer: filter or adjust alert
    enhancer_result = enhancer_process(payload)
    if not enhancer_result.allow:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"Enhancer rejected: {enhancer_result.reason}"})
    if enhancer_result.adjustments:
        allowed = {"size_usdt", "take_profit", "stop_loss", "leverage"}
        updates = {k: v for k, v in enhancer_result.adjustments.items() if k in allowed and v is not None}
        if updates:
            payload = payload.model_copy(update=updates)

    status_code, content = _execute_payload(payload, request_key)
    if status_code == 400 and "Risk check" in str(content.get("error", "")):
        logger.warning("Risk check failed", extra={"message": content.get("error"), "symbol": payload.symbol_upper()})
    elif content.get("ok") and content.get("order_id"):
        logger.info("Order placed", extra={"exchange": payload.exchange.value, "symbol": payload.symbol_upper(), "side": payload.side.value})
    return JSONResponse(status_code=status_code, content=content)


@app.post("/signals/generate")
async def signals_generate(request: Request):
    """
    AI signal layer: generate buy/sell/hold from market context.
    Body: { "exchange": "binance", "symbol": "BTCUSDT", "default_size_usdt": 100, "execute": false, "secret": "optional" }
    If execute=true and signal is buy/sell, runs through same risk + execution as webhook.
    """
    try:
        body = await request.json() or {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    exchange_name = (body.get("exchange") or "binance").lower()
    symbol = (body.get("symbol") or "BTCUSDT").upper().strip()
    default_size = float(body.get("default_size_usdt") or 100)
    execute = bool(body.get("execute", False))
    secret = body.get("secret") or settings.webhook_secret

    if not settings.ai_signal_enabled:
        return JSONResponse(status_code=400, content={"ok": False, "error": "AI signal layer is disabled (AI_SIGNAL_ENABLED=false)"})
    client_ip = request.client.host if request.client else "unknown"
    if not _signals_rate_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="Signal generation rate limit exceeded")

    signal = generate_signal(exchange_name, symbol, default_size_usdt=default_size)
    out = {"ok": True, "signal": signal}

    if signal.get("action") == "hold" or not signal.get("size_usdt"):
        return JSONResponse(status_code=200, content={**out, "executed": False})

    if not execute:
        return JSONResponse(status_code=200, content={**out, "executed": False})

    _require_admin(request)

    if settings.ai_signal_secret and secret != settings.ai_signal_secret:
        return JSONResponse(status_code=401, content={"ok": False, "error": "Invalid secret for signal execution"})

    try:
        payload = WebhookPayload(
            secret=secret,
            exchange=ExchangeName(exchange_name),
            symbol=symbol,
            side=Side(signal["action"]),
            size_usdt=float(signal["size_usdt"]),
            leverage=signal.get("leverage"),
            take_profit=signal.get("take_profit"),
            stop_loss=signal.get("stop_loss"),
            request_id=f"signal-{int(time.time())}-{symbol}",
        )
    except Exception as e:
        return JSONResponse(status_code=422, content={"ok": False, "error": str(e), "signal": signal})

    request_key = payload.idempotency_key()
    if idempotency_seen(request_key):
        return JSONResponse(status_code=200, content={**out, "executed": True, "duplicate": True})
    status_code, content = _execute_payload(payload, request_key)
    out["execution"] = content
    out["executed"] = status_code == 200 and content.get("ok") is True
    return JSONResponse(status_code=status_code, content=out)
