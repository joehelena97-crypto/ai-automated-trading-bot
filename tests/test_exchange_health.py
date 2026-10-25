import os
os.environ["APP_ENV"] = "test"

import ccxt
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from urllib.parse import urlencode

import server
from config import Settings, settings


ADMIN_KEY = "test-admin-key-with-at-least-thirty-two-characters"
PROD_WEBHOOK_SECRET = "wA4xD9mQ2pL7vR1sK8cF5nT3hB6yJ0uE"
PROD_ADMIN_KEY = "aZ5kN8rC1xV4mP7tD2qH9sE6fW3bG0yL"


class FakeClient:
    def __init__(self, timestamp=1_800_000_000_000, error=None):
        self.timestamp = timestamp
        self.error = error

    def fetch_time(self):
        if self.error:
            raise self.error
        return self.timestamp


class FakeExchange:
    def __init__(self, client):
        self.client = client

    def _get_client(self):
        return self.client


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    with TestClient(server.app, base_url="https://testserver") as test_client:
        yield test_client


def login_admin(client, key=ADMIN_KEY):
    return client.post(
        "/admin/session",
        headers={
            "Origin": "https://testserver",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        content=urlencode({"api_key": key}),
        follow_redirects=False,
    )


def set_configured_exchanges(monkeypatch, configured, exchanges):
    monkeypatch.setattr(settings.__class__, "exchange_enabled", lambda self, name: name in configured)
    monkeypatch.setattr(server, "get_all_exchanges", lambda: exchanges)


def test_exchange_health_reports_unconfigured_state(client, monkeypatch):
    set_configured_exchanges(monkeypatch, set(), {})

    response = client.get("/health/exchanges")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unconfigured",
        "exchanges": {
            "binance": "unconfigured",
            "bybit": "unconfigured",
            "okx": "unconfigured",
            "hyperliquid": "unconfigured",
        },
    }


def test_exchange_health_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(server._health_rate_limiter, "allow", lambda client_ip: False)

    response = client.get("/health/exchanges")

    assert response.status_code == 429


def test_exchange_health_reports_successful_read_only_check(client, monkeypatch):
    set_configured_exchanges(monkeypatch, {"binance"}, {"binance": FakeExchange(FakeClient())})

    response = client.get("/health/exchanges")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["exchanges"]["binance"] == "healthy"
    assert response.json()["exchanges"]["bybit"] == "unconfigured"


def test_exchange_health_sanitizes_failed_connectivity(client, monkeypatch):
    set_configured_exchanges(
        monkeypatch,
        {"binance"},
        {"binance": FakeExchange(FakeClient(error=RuntimeError("secret credential leaked")))},
    )

    response = client.get("/health/exchanges")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["exchanges"]["binance"] == "unavailable"
    assert "secret credential leaked" not in response.text


def test_exchange_health_marks_timeouts_degraded(client, monkeypatch):
    set_configured_exchanges(
        monkeypatch,
        {"binance"},
        {"binance": FakeExchange(FakeClient(error=ccxt.RequestTimeout("upstream detail")))},
    )

    response = client.get("/health/exchanges")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["exchanges"]["binance"] == "degraded"
    assert "upstream detail" not in response.text


@pytest.mark.parametrize(
    "malformed_timestamp",
    [None, 0, -1, float("inf"), float("nan"), "not-a-timestamp", True],
)
def test_exchange_health_marks_malformed_response_degraded(client, monkeypatch, malformed_timestamp):
    set_configured_exchanges(
        monkeypatch,
        {"binance"},
        {"binance": FakeExchange(FakeClient(timestamp=malformed_timestamp))},
    )

    response = client.get("/health/exchanges")

    assert response.status_code == 503
    assert response.json()["exchanges"]["binance"] == "degraded"


def test_exchange_health_reports_partial_outage_as_degraded(client, monkeypatch):
    set_configured_exchanges(
        monkeypatch,
        {"binance", "bybit"},
        {
            "binance": FakeExchange(FakeClient()),
            "bybit": FakeExchange(FakeClient(error=RuntimeError("upstream error"))),
        },
    )

    response = client.get("/health/exchanges")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["exchanges"]["binance"] == "healthy"
    assert response.json()["exchanges"]["bybit"] == "unavailable"
    assert "upstream error" not in response.text


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/exchanges/status"),
        ("put", "/exchanges/status"),
        ("get", "/balance"),
        ("get", "/metrics"),
        ("get", "/status"),
        ("put", "/settings/network"),
    ],
)
def test_sensitive_routes_reject_unauthenticated_requests(client, monkeypatch, method, path):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)

    request = getattr(client, method)
    response = request(path, json={}) if method == "put" else request(path)

    assert response.status_code == 401


def test_sensitive_routes_fail_closed_without_admin_key(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "")

    response = client.get("/metrics")

    assert response.status_code == 503


def test_admin_placeholder_key_fails_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS")

    response = login_admin(client)

    assert response.status_code == 503


def test_invalid_admin_login_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)

    response = login_admin(client, "wrong-admin-key")

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert "wrong-admin-key" not in response.text


def test_admin_login_requires_https(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)
    with TestClient(server.app, base_url="http://testserver") as insecure_client:
        page_response = insecure_client.get("/admin/login")
        response = insecure_client.post(
            "/admin/session",
            headers={
                "Origin": "http://testserver",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            content=urlencode({"api_key": ADMIN_KEY}),
        )

    assert page_response.status_code == 400
    assert response.status_code == 400
    assert response.json()["detail"] == "HTTPS is required for admin access"


def test_valid_admin_login_sets_secure_http_only_session(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)

    response = login_admin(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    cookie = response.headers["set-cookie"].lower()
    assert "admin_session=" in cookie
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    assert ADMIN_KEY not in response.text


def test_admin_login_page_is_script_free_and_https_only(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)

    response = client.get("/admin/login")

    assert response.status_code == 200
    assert "type=\"password\"" in response.text
    assert "<script" not in response.text.lower()
    assert ADMIN_KEY not in response.text
    assert response.headers["content-security-policy"]


def test_valid_session_authorizes_protected_route(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)
    login_response = login_admin(client)
    assert login_response.status_code == 303

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "orders_placed" in response.json()


def test_malformed_admin_session_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)
    client.cookies.set("admin_session", "invalid.session.token")

    response = client.get("/metrics")

    assert response.status_code == 401


def test_expired_admin_session_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)
    now = 1_800_000_000
    monkeypatch.setattr(server.time, "time", lambda: now)
    assert login_admin(client).status_code == 303
    monkeypatch.setattr(server.time, "time", lambda: now + server._ADMIN_SESSION_MAX_AGE + 1)

    response = client.get("/metrics")

    assert response.status_code == 401


def test_admin_login_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)
    monkeypatch.setattr(server._admin_login_rate_limiter, "allow", lambda client_ip: False)

    response = login_admin(client)

    assert response.status_code == 429


def test_webhook_rate_limit_applies_before_payload_parsing(client, monkeypatch):
    monkeypatch.setattr(server, "get_rate_limiter", lambda: server.RateLimiter(0))

    response = client.post("/webhook", content="not-json", headers={"Content-Type": "application/json"})

    assert response.status_code == 429


def test_signal_generation_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "ai_signal_enabled", True)
    monkeypatch.setattr(server._signals_rate_limiter, "allow", lambda client_ip: False)
    monkeypatch.setattr(
        server,
        "generate_signal",
        lambda *_args, **_kwargs: pytest.fail("rate-limited signal request reached generation"),
    )

    response = client.post("/signals/generate", json={"exchange": "binance", "symbol": "BTCUSDT"})

    assert response.status_code == 429


def test_wildcard_trusted_proxy_setting_is_rejected():
    with pytest.raises(ValidationError):
        Settings(trusted_proxy_ips="127.0.0.1,*")


@pytest.mark.parametrize("trusted_proxies", ["0.0.0.0/0", "::/0", "not-an-ip"])
def test_unsafe_trusted_proxy_ranges_are_rejected(trusted_proxies):
    with pytest.raises(ValidationError):
        Settings(trusted_proxy_ips=trusted_proxies)


def test_production_rejects_public_bind_address():
    with pytest.raises(RuntimeError):
        Settings(
            app_env="production",
            webhook_secret=PROD_WEBHOOK_SECRET,
            admin_api_key=PROD_ADMIN_KEY,
            host="0.0.0.0",
        ).validate_production_secrets()


@pytest.mark.parametrize(
    "values",
    [
        {"webhook_secret": "", "admin_api_key": ""},
        {"webhook_secret": "REPLACE_WITH_A_RANDOM_WEBHOOK_SECRET"},
        {"admin_api_key": "REPLACE_WITH_AT_LEAST_32_RANDOM_CHARACTERS"},
        {"webhook_secret": "w" * 48},
        {"admin_api_key": "12345678901234567890123456789012"},
    ],
)
def test_production_requires_strong_non_placeholder_secrets(values):
    valid = {
        "app_env": "production",
        "webhook_secret": PROD_WEBHOOK_SECRET,
        "admin_api_key": PROD_ADMIN_KEY,
        "require_webhook_secret": True,
    }
    valid.update(values)

    with pytest.raises(RuntimeError):
        Settings(**valid).validate_production_secrets()


def test_production_requires_webhook_authentication():
    with pytest.raises(RuntimeError):
        Settings(
            app_env="production",
            webhook_secret=PROD_WEBHOOK_SECRET,
            admin_api_key=PROD_ADMIN_KEY,
            require_webhook_secret=False,
        ).validate_production_secrets()


def test_production_rejects_weak_optional_ai_signal_secret():
    with pytest.raises(RuntimeError, match="AI_SIGNAL_SECRET"):
        Settings(
            app_env="production",
            webhook_secret=PROD_WEBHOOK_SECRET,
            admin_api_key=PROD_ADMIN_KEY,
            ai_signal_secret="short-but-not-placeholder",
            require_webhook_secret=True,
        ).validate_production_secrets()


def test_valid_production_secrets_pass_validation():
    Settings(
        app_env="production",
        webhook_secret=PROD_WEBHOOK_SECRET,
        admin_api_key=PROD_ADMIN_KEY,
        require_webhook_secret=True,
    ).validate_production_secrets()


def test_production_secrets_validated_before_app_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "webhook_secret", "")
    monkeypatch.setattr(settings, "admin_api_key", "")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "startup.db"))

    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET, ADMIN_API_KEY"):
        with TestClient(server.app):
            pass


def test_production_rejects_insecure_dotenv_permissions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("unused=true\n")
    env_file.chmod(0o644)

    with pytest.raises(RuntimeError):
        Settings(
            app_env="production",
            webhook_secret=PROD_WEBHOOK_SECRET,
            admin_api_key=PROD_ADMIN_KEY,
            require_webhook_secret=True,
        ).validate_production_secrets()


@pytest.mark.parametrize(
    ("configured", "required", "incoming", "expected"),
    [
        ("valid-webhook-secret-value-0123456789", True, "", False),
        ("valid-webhook-secret-value-0123456789", True, "wrong-secret", False),
        ("valid-webhook-secret-value-0123456789", True, "REPLACE_WITH_A_RANDOM_WEBHOOK_SECRET", False),
        ("REPLACE_WITH_A_RANDOM_WEBHOOK_SECRET", True, "REPLACE_WITH_A_RANDOM_WEBHOOK_SECRET", False),
        ("", True, "", False),
        ("", False, "", True),
    ],
)
def test_webhook_authentication_secret_validation(monkeypatch, configured, required, incoming, expected):
    from models import ExchangeName, Side, WebhookPayload

    monkeypatch.setattr(settings, "webhook_secret", configured)
    monkeypatch.setattr(settings, "require_webhook_secret", required)
    payload = WebhookPayload(
        secret=incoming,
        exchange=ExchangeName.BINANCE,
        symbol="BTCUSDT",
        side=Side.BUY,
        size_usdt=10,
    )

    assert server._verify_secret(payload) is expected


def test_dashboard_does_not_expose_admin_key_or_store_it(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert ADMIN_KEY not in response.text
    assert "sessionStorage" not in response.text
    assert "Authorization" not in response.text


def test_admin_mutation_requires_same_origin(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)
    assert login_admin(client).status_code == 303

    response = client.put(
        "/exchanges/status",
        headers={"Origin": "https://attacker.example"},
        json={"binance": True},
    )

    assert response.status_code == 403


def test_admin_logout_revokes_browser_session(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)
    login_response = login_admin(client)
    assert login_response.status_code == 303
    session_cookie = login_response.headers["set-cookie"].split(";", maxsplit=1)[0].split("=", maxsplit=1)[1]

    response = client.post("/admin/session/logout", headers={"Origin": "https://testserver"})
    client.cookies.set("admin_session", session_cookie)
    after_logout = client.get("/metrics")

    assert response.status_code == 200
    assert after_logout.status_code == 401


@pytest.mark.parametrize(
    ("configured_secret", "payload_secret", "include_secret", "expected_status"),
    [
        ("valid-webhook-secret-value-0123456789", "wrong-value", True, 401),
        (
            "valid-webhook-secret-value-0123456789",
            "REPLACE_WITH_A_RANDOM_WEBHOOK_SECRET",
            True,
            401,
        ),
        ("", "", True, 401),
        ("valid-webhook-secret-value-0123456789", "", False, 422),
    ],
)
def test_webhook_rejects_missing_placeholder_or_invalid_secret(
    client, monkeypatch, configured_secret, payload_secret, include_secret, expected_status
):
    monkeypatch.setattr(settings, "webhook_secret", configured_secret)
    monkeypatch.setattr(settings, "require_webhook_secret", True)
    monkeypatch.setattr(
        server,
        "_execute_payload",
        lambda *_args, **_kwargs: pytest.fail("invalid webhook secret reached execution"),
    )
    payload = {
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "side": "buy",
        "size_usdt": 10,
        "request_id": f"auth-test-{expected_status}-{include_secret}-{payload_secret[:4]}",
    }
    if include_secret:
        payload["secret"] = payload_secret

    response = client.post("/webhook", json=payload)

    assert response.status_code == expected_status
    if configured_secret:
        assert configured_secret not in response.text
    if payload_secret:
        assert payload_secret not in response.text


def test_exchange_status_rejects_non_boolean_values(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)
    monkeypatch.setattr(server, "_exchange_connected", {"binance": True})
    assert login_admin(client).status_code == 303

    response = client.put(
        "/exchanges/status",
        headers={"Origin": "https://testserver"},
        json={"binance": "false"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Exchange status must be a boolean"


def test_network_setting_rejects_non_boolean_values(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY)
    assert login_admin(client).status_code == 303

    response = client.put(
        "/settings/network",
        headers={"Origin": "https://testserver"},
        json={"use_testnet": "false"},
    )

    assert response.status_code == 400
