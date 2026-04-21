# Terminal-BTC

Paper-trading bot for BTC, ETH, SOL, XRP, and other high-volume USDT pairs against **real** market prices, with a pluggable signal generator and exchange data source.

- **Defaults**: `PAPER_MODE=true` + `SIGNAL_MODE=rules` + `DATA_SOURCE=bybit` → runs with **no API keys**. No real orders ever.
- **Market data**: pulled from a real public exchange (Bybit by default, swap to Binance or Kraken via `DATA_SOURCE`). No keys, no testnet.
- **Universe**: 8 pairs by default — BTC, ETH, SOL, XRP, DOGE, ADA, AVAX, LINK. Edit `TRADE_SYMBOLS` to add more; `SYMBOL_ALLOWLIST` is a hard filter (16 pairs out of the box).
- **Signal**: local EMA/RSI/MACD/ATR engine (`app/services/rules_signal.py`). Swap in Claude later with `SIGNAL_MODE=claude` + `ANTHROPIC_API_KEY`.
- **Risk gates**: per-trade cap, daily loss cap, max concurrent positions, symbol allowlist, DB-backed kill switch, idempotent `clientOrderId`.
- **TradingView** is optional — free TV tier has no webhooks; the scheduler polls every `POLL_INTERVAL_SEC` and sweeps all symbols in turn. If you later upgrade, the `/tv/webhook` route is wired.

## Quick start (no accounts required)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env       # defaults are paper + rules — no edits needed
./scripts/dev_run.sh       # http://localhost:8000
```

Then in another terminal:

```bash
curl -s http://localhost:8000/status | python -m json.tool
curl -s -X POST http://localhost:8000/control/tick-now | python -m json.tool
```

`/status` should show `"paper_mode": true, "signal_mode": "rules", "data_source": "bybit", "real_orders_enabled": false` and the list of symbols.
`/control/tick-now` sweeps every symbol in `TRADE_SYMBOLS` — real public data → rules engine → paper decision (and `dry` trade row if the signal isn't `hold`).

## Data sources

| `DATA_SOURCE` | Notes |
|---|---|
| `bybit` (default) | Globally accessible public API; same USDT pairs as Binance. |
| `binance` | Largest liquidity; **geo-blocked** in US, UK, and a few other regions. |
| `kraken` | Solid fallback when Binance/Bybit are blocked. Symbols auto-normalize (BTC/USDT works). |

No API keys needed for any of them — market data is public.

## Signal modes

| `SIGNAL_MODE` | Needs | Cost | Notes |
|---|---|---|---|
| `rules` (default) | nothing | free | EMA20/50/200 trend + RSI filter + ATR-based SL/TP. Deterministic. |
| `claude` | `ANTHROPIC_API_KEY` + credits | per-call tokens | Opus signal via tool-use, cached system prompt, capped by `DAILY_CLAUDE_USD_CAP`. |
| `hold` | nothing | free | Always returns `hold`. For plumbing/smoke tests. |

Switch any time by editing `.env` and restarting.

## Paper → exchange promotion

All three conditions must be true for a real order to leave the process:

1. `PAPER_MODE=false`
2. `LIVE_TRADING=true`
3. `BINANCE_TESTNET=false` (to hit mainnet) **or** `BINANCE_TESTNET=true` with Binance testnet keys (if you want a sandbox)

Until then, `executor.py` writes `status="dry"` rows to SQLite and updates a paper `Position`.

## Control endpoints

- `GET /healthz` — liveness.
- `GET /status` — safety flags, current (paper) position, today's PnL.
- `POST /control/tick-now` — force a decision cycle immediately.
- `POST /control/kill` — body `{"reason":"..."}` — blocks all future trades.
- `POST /control/resume` — clears kill switch.

## TradingView (only if you pay for webhooks)

If you're on TV Free, skip this — the scheduler drives everything.

1. `./scripts/ngrok_tunnel.sh` → copy the `https://...ngrok-free.app` URL.
2. Paste the template from `scripts/tradingview_alert.pine` into the Pine editor; replace `REPLACE_ME_SECRET` with `TRADINGVIEW_WEBHOOK_SECRET`.
3. Create an alert → set Webhook URL to `https://<ngrok>/tv/webhook`, set the message body to the JSON in the template.

Alerts never execute orders — they enqueue into the decision loop as added context.

## Project layout

```
app/
├── main.py              # FastAPI app + lifespan
├── config.py            # PAPER_MODE, SIGNAL_MODE, and all flags
├── db.py, models.py     # SQLModel
├── exchange/
│   └── binance_client.py  # split: public (data) vs authed (orders)
├── services/
│   ├── market_data.py     # real public Binance OHLCV + indicators
│   ├── rules_signal.py    # local EMA/RSI/MACD engine (default)
│   ├── claude_signal.py   # Anthropic, tool-use, cached prompt
│   ├── signal.py          # dispatcher (rules | claude | hold)
│   ├── risk.py            # caps, kill switch
│   ├── executor.py        # paper-first, idempotent
│   └── scheduler.py       # APScheduler tick + TV queue consumer
├── routes/              # /healthz /status /control/* /tv/webhook
└── prompts/system_trader.md
```

## Tests

```bash
pytest -q
```
Covers rules signal (long/short/hold/exit), risk gates, paper-only executor + idempotent replay, and webhook auth.
