# Terminal-BTC

Claude-driven BTC trading on Binance with TradingView alert context.

- **Claude (model: `claude-opus-4-7`) is the signal generator.** It reads a market snapshot (OHLCV + RSI/MACD/EMA/ATR + order book) and returns a structured `{action, size_pct, stop_loss, take_profit, confidence, reasoning}` via tool-use.
- **Binance via ccxt.** Defaults to **testnet + dry-run**. Real orders require BOTH `BINANCE_TESTNET=false` **and** `LIVE_TRADING=true`.
- **TradingView** alerts POST to `/tv/webhook` (shared-secret auth). Alerts feed context into Claude's next decision — they never execute orders directly.
- **Risk gates**: per-trade notional cap, daily loss cap, max open positions, symbol allowlist, DB-backed kill switch, idempotent `clientOrderId` via `sha1(decision_id)`.

## Quick start

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Configure
cp .env.example .env
# Fill: ANTHROPIC_API_KEY, BINANCE_API_KEY, BINANCE_API_SECRET
# Keep: BINANCE_TESTNET=true, LIVE_TRADING=false

# 3. Run
./scripts/dev_run.sh
# or: uvicorn app.main:app --reload --port 8000
```

Check `GET http://localhost:8000/status` — it must show `testnet=true`, `live_trading=false`, `real_orders_enabled=false`.

## Connect TradingView

1. Start the server locally.
2. Expose it publicly: `./scripts/ngrok_tunnel.sh` (requires [ngrok](https://ngrok.com)).
3. Open the ngrok HTTPS URL; in TradingView, create an alert on a chart:
   - **Webhook URL**: `https://<your-ngrok>/tv/webhook`
   - **Message** (paste the JSON from `scripts/tradingview_alert.pine`, replacing `REPLACE_ME_SECRET` with your `TRADINGVIEW_WEBHOOK_SECRET`):
     ```json
     {"secret":"<your-secret>","symbol":"{{ticker}}","price":{{close}},"alert":"ema_bull_cross","tf":"{{interval}}","ts":"{{time}}"}
     ```
4. Trigger an alert → the server logs `tv_webhook_accepted` and enqueues a tick. The consumer runs `market_data → claude_signal → risk → executor`.

## End-to-end verification (Binance testnet)

1. Generate a Binance **testnet** API key at https://testnet.binance.vision/ and set `.env`.
2. `uvicorn app.main:app` — `/healthz` returns `ok`; logs show `scheduler_started`.
3. `GET /status` — confirms testnet=true, live_trading=false.
4. `pytest` — risk, webhook-auth, and dry-run executor tests pass.
5. `POST /control/tick-now` — logs show the snapshot, Claude's decision JSON, the risk verdict, and a `DRY` trade row in `data/terminal_btc.db`.
6. Flip `LIVE_TRADING=true` (still testnet). Restart. Tick again: ccxt returns a testnet order id; the `Trade` row flips to `filled`; the position appears in the Binance testnet UI.
7. Fire a TradingView alert at the ngrok URL: `/tv/webhook` returns 202; a `Decision(source="tv")` row appears.
8. `POST /control/kill` → subsequent ticks refuse with `kill_switch: ...`.
9. Only after the above pass — consider real money. Set `BINANCE_TESTNET=false` **and** `LIVE_TRADING=true`. Keep `MAX_POSITION_USDT` tiny (e.g., `20`) for your first runs.

## Project layout

```
app/
├── main.py              # FastAPI app + lifespan
├── config.py            # pydantic-settings
├── db.py                # SQLModel engine
├── models.py            # Decision, Trade, Position, DailyPnL, KillSwitch
├── logging_setup.py     # structlog JSON
├── exchange/
│   └── binance_client.py
├── services/
│   ├── market_data.py   # OHLCV + indicators (ta)
│   ├── claude_signal.py # Anthropic SDK, prompt caching, tool-use
│   ├── risk.py          # caps, daily loss, kill switch
│   ├── executor.py      # dry-run vs live, idempotent orders
│   └── scheduler.py     # APScheduler tick loop + TV queue consumer
├── routes/
│   ├── webhook.py       # POST /tv/webhook
│   ├── health.py        # /healthz, /readyz, /status
│   └── control.py       # /control/kill, /resume, /tick-now
└── prompts/
    └── system_trader.md # cached Claude system prompt
```

## Safety checklist before going live

- [ ] Tested on Binance testnet end-to-end (webhook → tick → DRY trade → filled trade).
- [ ] `MAX_POSITION_USDT` and `MAX_DAILY_LOSS_USDT` set to amounts you can afford to lose.
- [ ] Kill switch tested (`POST /control/kill`).
- [ ] Daily Claude USD cap set (`DAILY_CLAUDE_USD_CAP`).
- [ ] Logs being captured somewhere durable.

## Control endpoints

- `GET /healthz` — liveness.
- `GET /status` — safety flags, current position, today's PnL and Claude spend.
- `POST /control/tick-now` — force one decision cycle immediately.
- `POST /control/kill` — body `{"reason": "..."}` — blocks all future trades.
- `POST /control/resume` — clears kill switch.

## Notes

- The price estimates used to enforce `DAILY_CLAUDE_USD_CAP` are rough; tune them in `app/services/claude_signal.py` if Anthropic pricing changes.
- The `ta` library is synchronous; indicator computation runs in a thread pool from the async scheduler tick.
