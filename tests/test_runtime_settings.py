import pytest

from config import Settings, settings
from runtime_settings import get_use_testnet


def test_trading_mode_defaults_to_testnet():
    config = Settings(_env_file=None)

    assert config.trading_mode == "testnet"
    assert not hasattr(config, "bybit_testnet")


def test_invalid_trading_mode_defaults_to_testnet():
    config = Settings(_env_file=None, trading_mode="invalid")

    assert config.trading_mode == "testnet"


@pytest.mark.parametrize("value", [" LIVE ", "Live", "live"])
def test_live_trading_mode_is_case_and_whitespace_insensitive(value):
    config = Settings(_env_file=None, trading_mode=value)

    assert config.trading_mode == "live"


def test_live_trading_mode_keeps_bybit_on_live_path(monkeypatch):
    monkeypatch.setattr(settings, "trading_mode", "live")

    assert get_use_testnet("bybit") is False


def test_testnet_trading_mode_uses_bybit_testnet(monkeypatch):
    monkeypatch.setattr(settings, "trading_mode", "testnet")

    assert get_use_testnet("bybit") is True
