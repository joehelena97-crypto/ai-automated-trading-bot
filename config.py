"""
Configuration for TradingView Webhook Bot.
Loads from environment variables (use .env for local dev).
"""
from __future__ import annotations

import os
import ipaddress
import stat
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Webhook
    app_env: str = "development"
    webhook_secret: str = ""
    require_webhook_secret: bool = True  # If True and secret is set, reject missing/wrong secret
    admin_api_key: str = ""
    rate_limit_per_minute: int = 30  # Max webhook requests per minute per client
    idempotency_ttl_seconds: int = 86400  # 24h - remember request_id for this long

    # Binance
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = False

    # Bybit
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = False

    # OKX
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_passphrase: str = ""
    okx_sandbox: bool = False

    # Hyperliquid (private key required; wallet address optional)
    hyperliquid_private_key: str = ""
    hyperliquid_wallet_address: str = ""
    hyperliquid_testnet: bool = False

    # Risk
    max_position_size_usdt: float = 1000.0
    max_leverage: int = 10
    max_daily_loss_usdt: float = 500.0
    allowed_symbols: str = "BTCUSDT,ETHUSDT"
    max_position_per_symbol_usdt: float = 0.0  # 0 = use max_position_size_usdt for all
    max_total_exposure_usdt: float = 0.0  # 0 = no cap on total notional
    cooldown_after_loss_minutes: int = 60  # Block new orders for N min after daily loss limit hit
    max_spread_pct: float = 0.0  # 0 = disabled; reject if (ask-bid)/mid > this

    # Execution
    dry_run: bool = False  # Log only, do not send orders
    order_retries: int = 2  # Retries with backoff for place_market_order
    order_retry_delay_seconds: float = 1.0

    # Trailing stop
    trailing_stop_check_interval_seconds: int = 30

    # DB & state
    db_path: str = "webhook_bot.db"

    # AI / LLM
    openai_api_key: str = ""  # Or set OPENAI_BASE_URL for compatible APIs (e.g. Azure, local)
    openai_base_url: str = ""  # Optional; default OpenAI
    llm_model: str = "gpt-4o-mini"  # Model for advisor and enhancer
    llm_advisor_enabled: bool = False  # Require LLM approval before placing order
    ai_enhancer_enabled: bool = False  # Filter or adjust TradingView alerts via LLM
    ai_signal_enabled: bool = False  # Allow /signals/generate to produce executable signals
    ai_signal_secret: str = ""  # Optional secret for POST /signals/generate (and execute)

    # Server
    host: str = "127.0.0.1"
    port: int = 8000
    trusted_proxy_ips: str = "127.0.0.1"

    @staticmethod
    def _is_placeholder_secret(value: str) -> bool:
        normalized = value.strip().lower()
        if not normalized:
            return True
        markers = (
            "replace_", "placeholder", "your_", "example", "changeme", "change-me",
            "default", "supersecret", "secret-key", "test-secret", "dev-secret",
            "local-secret", "admin123",
        )
        if any(marker in normalized for marker in markers) or normalized in {
            "secret", "password", "admin", "test", "123456", "12345678901234567890123456789012",
        }:
            return True
        if len(set(normalized)) < 12:
            return True
        for pattern in ("0123456789abcdef", "1234567890", "qwerty"):
            if len(normalized) % len(pattern) == 0 and normalized == pattern * (len(normalized) // len(pattern)):
                return True
        return False

    def validate_production_secrets(self) -> None:
        if self.app_env.strip().lower() not in {"production", "prod"}:
            return

        required = {
            "WEBHOOK_SECRET": self.webhook_secret,
            "ADMIN_API_KEY": self.admin_api_key,
        }
        invalid = [
            name for name, value in required.items()
            if len(value.strip()) < 32 or self._is_placeholder_secret(value)
        ]
        if invalid:
            raise RuntimeError(
                f"Production requires strong, non-placeholder secrets: {', '.join(invalid)}"
            )
        if not self.require_webhook_secret:
            raise RuntimeError("REQUIRE_WEBHOOK_SECRET must be true in production")

        exchange_fields = {
            "BINANCE_API_KEY": self.binance_api_key,
            "BINANCE_API_SECRET": self.binance_api_secret,
            "BYBIT_API_KEY": self.bybit_api_key,
            "BYBIT_API_SECRET": self.bybit_api_secret,
            "OKX_API_KEY": self.okx_api_key,
            "OKX_API_SECRET": self.okx_api_secret,
            "OKX_PASSPHRASE": self.okx_passphrase,
            "HYPERLIQUID_PRIVATE_KEY": self.hyperliquid_private_key,
        }
        for name, value in exchange_fields.items():
            if value and self._is_placeholder_secret(value):
                raise RuntimeError(f"{name} must not contain a placeholder value")
        for name, values in {
            "Binance": (self.binance_api_key, self.binance_api_secret),
            "Bybit": (self.bybit_api_key, self.bybit_api_secret),
            "OKX": (self.okx_api_key, self.okx_api_secret, self.okx_passphrase),
        }.items():
            if any(values) and not all(values):
                raise RuntimeError(f"All credentials must be configured when enabling {name}")

        if self.ai_enhancer_enabled or self.llm_advisor_enabled or self.ai_signal_enabled:
            if self._is_placeholder_secret(self.openai_api_key):
                raise RuntimeError("OPENAI_API_KEY is required when an AI feature is enabled")
        if self.ai_signal_secret and (
            len(self.ai_signal_secret.strip()) < 32
            or self._is_placeholder_secret(self.ai_signal_secret)
        ):
            raise RuntimeError("AI_SIGNAL_SECRET must be strong and non-placeholder")

        try:
            bind_ip = ipaddress.ip_address(self.host)
        except ValueError:
            raise RuntimeError("Production HOST must be a loopback or private IP address")
        if bind_ip.is_unspecified or not (bind_ip.is_loopback or bind_ip.is_private):
            raise RuntimeError("Production HOST must not be publicly reachable")

        env_file = os.path.abspath(".env")
        if os.path.isfile(env_file) and stat.S_IMODE(os.stat(env_file).st_mode) & 0o077:
            raise RuntimeError("Production .env file permissions must be restricted to its owner")

    @field_validator("trusted_proxy_ips")
    @classmethod
    def reject_unrestricted_proxy_trust(cls, value: str) -> str:
        for entry in value.split(","):
            entry = entry.strip()
            if entry == "*":
                raise ValueError("TRUSTED_PROXY_IPS must not include wildcard entries")
            try:
                network = ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError("TRUSTED_PROXY_IPS must contain valid IPs or CIDRs") from exc
            if network.prefixlen == 0:
                raise ValueError("TRUSTED_PROXY_IPS must not include catch-all networks")
        return value

    @field_validator("app_env")
    @classmethod
    def validate_app_environment(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"development", "dev", "test", "production", "prod"}:
            raise ValueError("APP_ENV must be development, test, or production")
        return normalized

    @property
    def allowed_symbols_list(self) -> List[str]:
        return [s.strip().upper() for s in self.allowed_symbols.split(",") if s.strip()]

    def exchange_enabled(self, name: str) -> bool:
        if name == "binance":
            return bool(self.binance_api_key and self.binance_api_secret)
        if name == "bybit":
            return bool(self.bybit_api_key and self.bybit_api_secret)
        if name == "okx":
            return bool(self.okx_api_key and self.okx_api_secret and self.okx_passphrase)
        if name == "hyperliquid":
            return bool(self.hyperliquid_private_key.strip())
        return False


settings = Settings()
