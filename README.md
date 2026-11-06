# TradingView Webhook Bot

A webhook server that receives TradingView alerts and places orders on supported exchanges, with risk limits, take-profit/stop-loss, and optional AI review.

## Supported exchanges

- Binance
- Bybit
- OKX
- Hyperliquid

Each exchange can run on its testnet or sandbox. Live trading is off unless you set `TRADING_MODE=live`.

## How it works

1. TradingView sends a JSON alert to `POST /webhook` with `exchange`, `symbol`, `side`, and `size_usdt`. Optional fields include `leverage`, `take_profit`, `stop_loss`, and `trailing_stop`.
2. The bot checks the webhook secret and ignores duplicate `request_id` values.
3. Risk checks run first: position size, leverage, daily loss limit, symbol allowlist, and exposure caps. Any failed check rejects the order.
4. If enabled, an AI advisor can approve, reject, or adjust the order before it is sent.
5. The bot places a market order and, if set, the matching take-profit and stop-loss orders.

## Setup

1. Copy `.env.example` to `.env` and fill in your keys. Never commit `.env`.
2. Keep `TRADING_MODE=testnet` and `DRY_RUN=true` until you have tested the full flow.
3. Install dependencies with `pip install -r requirements.txt`.
4. Start the server with `python main.py`.

## Dashboard

`GET /dashboard` shows public exchange health. After admin sign-in, it also shows PnL, recent orders, and a Live/Testnet switch.

## Risk warning

Automated trading can lose money. Test on testnet first, and review the code and settings before using real funds.
