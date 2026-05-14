// Terminal-BTC dashboard — vanilla JS, polls /api/* endpoints.

const REFRESH_MS = 10_000;        // full-dashboard poll
const MARKETS_REFRESH_MS = 3_000;  // live prices — fast loop

// TradingView widget. We deliberately decouple the CHART source from the
// trading DATA source: trading runs on whatever DATA_SOURCE is set (MEXC,
// Bybit, Kraken…), but the chart hits BINANCE because TradingView has the
// broadest USDT-pair coverage there. If a symbol isn't on Binance (rare —
// only the most obscure MEXC alts), we fall through to a small priority
// list of exchanges TradingView indexes well.
const _TV_CHART_SOURCES = ["BINANCE", "BYBIT", "OKX", "MEXC", "GATEIO"];
// Symbols where we know Binance doesn't list — pin them to a working exchange.
// Extend this map as you discover Binance-missing pairs.
const _TV_CHART_OVERRIDES = {
  // Common MEXC-exotic discoveries that TradingView shows on MEXC only.
  "SIREN/USDT": "MEXC", "LAB/USDT": "MEXC", "BILL/USDT": "MEXC",
  "AIGENSYN/USDT": "MEXC", "CSCOSTOCK/USDT": "MEXC", "RIVER/USDT": "MEXC",
  "TROLLSOL/USDT": "MEXC", "SKYAI/USDT": "MEXC", "UB/USDT": "MEXC",
  "B/USDT": "MEXC", "Q/USDT": "MEXC", "TRUTH/USDT": "MEXC",
  "HYPE/USDT": "MEXC", "TAO/USDT": "BYBIT",
};
let _tvWidget = null;
let _tvSymbol = "BTC/USDT";
let _tvInterval = "15";

function tvPair(symbol, source = "BINANCE") {
  const clean = symbol.replace("/", "").toUpperCase();
  return `${source.toUpperCase()}:${clean}`;
}

function _chartSourceFor(symbol) {
  // Per-symbol pin wins, else default to first source in the priority list.
  return _TV_CHART_OVERRIDES[symbol] || _TV_CHART_SOURCES[0];
}

// Studies layered onto every TradingView chart. These are the SAME indicators
// the bot's rules engine uses to score signals — plus VWAP, the single most-
// watched institutional reference (real desks execute against VWAP daily).
// No magic indicators, no "whale signals" — just the bot's brain on the
// screen so visual analysis lines up with what the bot will actually fire.
//
//   * EMA 20 / 50 / 200 — the bot's regime gate (bull stack = +0.30 score)
//   * RSI(14)            — momentum, bot's RSI contribution
//   * MACD(12,26,9)      — trend strength, bot's MACD contribution
//   * Bollinger Bands    — volatility envelope; bb_pct contribution
//   * Stochastic RSI     — early momentum reversals, bot's stoch contribution
//   * VWAP               — institutional benchmark
//   * Volume             — confirms moves; thin volume = lower confidence
//
// Note: MAExp with explicit length input requires the object form. The bare
// "@tv-basicstudies" string IDs are the standard library defaults.
const _TV_STUDIES = [
  // Three EMAs, each on its own pane, matching the 20/50/200 stack the bot
  // uses (rules_signal._score_ema).
  { id: "MAExp@tv-basicstudies", inputs: { length: 20 } },
  { id: "MAExp@tv-basicstudies", inputs: { length: 50 } },
  { id: "MAExp@tv-basicstudies", inputs: { length: 200 } },
  "RSI@tv-basicstudies",
  "MACD@tv-basicstudies",
  "BB@tv-basicstudies",
  "StochasticRSI@tv-basicstudies",
  "VWAP@tv-basicstudies",
  "Volume@tv-basicstudies",
];

function mountTradingView(symbol, interval, _ignoredSource) {
  const host = document.getElementById("tv-chart");
  if (!host) return;
  host.innerHTML = "";
  // The widget needs a container with a stable ID (it queries the DOM by ID).
  // Recreate the inner div on every remount so leftover state from a prior
  // chart can't bleed in.
  const containerId = "tv-chart-container";
  const inner = document.createElement("div");
  inner.id = containerId;
  inner.style.cssText = "width:100%;height:100%;";
  host.appendChild(inner);

  const source = _chartSourceFor(symbol);
  const sym = tvPair(symbol, source);

  // tv.js loads asynchronously from the CDN; if the page rendered before it
  // arrived we wait a beat and retry. Two short retries cover slow first
  // loads without spinning forever on a real outage.
  if (typeof window.TradingView === "undefined" || !window.TradingView.widget) {
    if (mountTradingView._retries === undefined) mountTradingView._retries = 0;
    if (mountTradingView._retries < 8) {
      mountTradingView._retries += 1;
      setTimeout(() => mountTradingView(symbol, interval), 400);
      return;
    }
    // Give up after ~3 s — show a fallback iframe so the user at least sees price.
    host.innerHTML =
      '<div style="padding:20px;color:#999;text-align:center;">' +
      'TradingView library failed to load.<br>' +
      '<a href="https://www.tradingview.com/chart/?symbol=' + encodeURIComponent(sym) +
      '" target="_blank" style="color:var(--accent);">Open chart on TradingView →</a></div>';
    return;
  }
  mountTradingView._retries = 0;

  /* eslint-disable no-undef */
  new TradingView.widget({
    autosize: true,
    symbol: sym,
    interval: String(interval),
    timezone: "Asia/Kolkata",  // matches the rest of the dashboard's IST display
    theme: "dark",
    style: "1",                  // candles
    locale: "en",
    enable_publishing: false,
    allow_symbol_change: true,
    container_id: containerId,
    studies: _TV_STUDIES,
    hide_side_toolbar: false,
    withdateranges: true,
    save_image: true,
  });
  /* eslint-enable no-undef */
}

const fmtUsd = (n, digits = 2) => {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  return sign + "$" + Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
};
const fmtSignedUsd = (n) => (n >= 0 ? "+" : "") + fmtUsd(n);
const fmtPct = (n) => (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
const fmtQty = (n) => n.toLocaleString(undefined, { maximumFractionDigits: 6 });
const fmtPrice = (n) => {
  if (n === null || n === undefined) return "—";
  const digits = n >= 100 ? 2 : n >= 1 ? 4 : 6;
  return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
};
// IST (Asia/Kolkata, UTC+5:30) is the canonical dashboard timezone — pinned
// explicitly so the same display works from any device regardless of the
// browser's locale. Server stamps every timestamp with explicit UTC
// (iso_utc in app/db.py adds +00:00), so JS converts correctly on its own.
const TZ = "Asia/Kolkata";

// Full date + 12-hour clock — "14 May, 08:28:18 PM". Use for headers /
// the dashboard's "last updated" labels where the full context is welcome.
const fmtTime = (iso) => {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-IN", {
    timeZone: TZ, day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true,
  });
};

// 12-hour clock with date inline — "08:28:18 PM · 14 May". The canonical
// per-row stamp; compact enough for a table cell, complete enough that
// you never have to guess which day a 2 AM event landed on.
const fmtClock = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  const t = d.toLocaleTimeString("en-IN", {
    timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true,
  });
  const day = d.toLocaleDateString("en-IN", {
    timeZone: TZ, day: "2-digit", month: "short",
  });
  return `${t} · ${day}`;
};

// Relative time: "5s ago", "12m ago", "3h ago", "2d ago". Refreshes on
// every dashboard render (10 s) so values stay current without re-renders.
const fmtAge = (iso) => {
  if (!iso) return "";
  const secs = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 0) return "0s ago";
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
};

// "08:28:18 PM · 14 May · 5m ago" — clock, date, and relative age all in
// one render. Every event row uses this so you can see at a glance "when
// exactly" and "how long ago" without parsing prose.
const fmtTimeWithAge = (iso) => {
  if (!iso) return "—";
  return `${fmtClock(iso)} <span class="muted">· ${fmtAge(iso)}</span>`;
};

const classPN = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "");

async function fetchJSON(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return await r.json();
}

function renderHeartbeat(ov) {
  const h = ov.scheduler || {};
  const schedEl = document.getElementById("hb-scheduler");
  const lastEl = document.getElementById("hb-last-tick");
  const nextEl = document.getElementById("hb-next-tick");
  const sumEl = document.getElementById("hb-last-summary");

  if (h.scheduler_running) {
    schedEl.textContent = `scheduler: live · every ${Math.round((h.poll_interval_sec || 300) / 60)}m`;
    schedEl.className = "hb-pill alive";
  } else {
    schedEl.textContent = "scheduler: down";
    schedEl.className = "hb-pill down";
  }

  if (h.last_tick_finished_at) {
    const ago = Math.round((Date.now() - new Date(h.last_tick_finished_at).getTime()) / 1000);
    const label = ago < 60 ? `${ago}s ago` : ago < 3600 ? `${Math.round(ago / 60)}m ago` : `${Math.round(ago / 3600)}h ago`;
    lastEl.textContent = `last tick: ${label}`;
    lastEl.className = ago > 600 ? "hb-pill stale" : "hb-pill alive";
  } else {
    lastEl.textContent = "last tick: pending";
    lastEl.className = "hb-pill stale";
  }

  if (h.next_tick_at) {
    const secs = Math.round((new Date(h.next_tick_at).getTime() - Date.now()) / 1000);
    nextEl.textContent = secs > 0 ? `next tick: ${secs < 60 ? secs + "s" : Math.round(secs / 60) + "m"}` : "next tick: now";
  } else {
    nextEl.textContent = "next tick: —";
  }

  const c = h.last_tick_summary?.counts;
  if (c) {
    sumEl.textContent = `last sweep: ${c.buy || 0}B · ${c.sell || 0}S · ${c.hold || 0}H${c.skipped ? " · " + c.skipped + " skipped" : ""}`;
  } else {
    sumEl.textContent = "awaiting first tick — click Run tick now";
  }

  // Drawdown circuit breaker pill
  const ddEl = document.getElementById("hb-drawdown");
  const dd = ov.drawdown;
  if (dd && dd.peak_equity_usdt > 0) {
    const ddPct = (dd.dd_pct * 100).toFixed(1);
    if (dd.multiplier_active) {
      ddEl.textContent = `DD ${ddPct}% · size ×${dd.multiplier}`;
      ddEl.className = "hb-pill stale";
    } else {
      ddEl.textContent = `DD ${ddPct}% · size ×1.0`;
      ddEl.className = dd.dd_pct > 0.05 ? "hb-pill stale" : "hb-pill alive";
    }
  } else {
    ddEl.textContent = "DD —";
    ddEl.className = "hb-pill muted";
  }

  // Regime pill — bull/bear/chop drives signal-side gating.
  const regEl = document.getElementById("hb-regime");
  const reg = ov.regime || "—";
  regEl.textContent = `regime ${reg}`;
  regEl.className = reg === "bull" ? "hb-pill alive"
    : reg === "bear" ? "hb-pill down"
    : reg === "chop" ? "hb-pill stale"
    : "hb-pill muted";

  // Kelly pill — shows whether sizing has switched from heuristic to Kelly.
  const kEl = document.getElementById("hb-kelly");
  if (ov.kelly && ov.kelly.fraction !== undefined) {
    kEl.textContent = `Kelly ${(ov.kelly.fraction * 100).toFixed(2)}% · n=${ov.kelly.sample_size}`;
    kEl.className = "hb-pill alive";
  } else {
    kEl.textContent = "size: heuristic (n<50)";
    kEl.className = "hb-pill muted";
  }
}

function renderMarkets(rows) {
  const host = document.getElementById("markets-grid");
  const updEl = document.getElementById("markets-updated");
  const titleEl = document.getElementById("markets-title");
  if (titleEl) {
    const n = rows?.length || 0;
    titleEl.textContent = `Live markets — ${n} USDT pairs`;
  }
  if (!rows || !rows.length) {
    host.innerHTML = `<div class="muted" style="padding:20px;">no market data yet</div>`;
    return;
  }
  updEl.textContent = `updated ${new Date().toLocaleTimeString()}`;
  host.innerHTML = rows.map(r => {
    if (r.error) {
      return `<div class="market-card flat"><div class="market-sym">${r.symbol}</div><div class="muted" style="font-size:11px;">fetch error</div></div>`;
    }
    const change = r.change_pct || 0;
    const cls = change > 0.1 ? "up" : change < -0.1 ? "down" : "flat";
    const selected = r.symbol === _tvSymbol ? " selected" : "";

    // Indicator chips — show the user why the engine likes or dislikes this coin.
    const chips = [];
    if (r.bias === "long") chips.push(`<span class="chip long">LONG</span>`);
    else if (r.bias === "short") chips.push(`<span class="chip short">SHORT</span>`);
    else chips.push(`<span class="chip watch">WATCH</span>`);
    if (r.rsi != null) {
      const rsiCls = r.rsi > 70 ? "bad" : r.rsi < 30 ? "good" : "";
      chips.push(`<span class="chip ${rsiCls}">RSI ${r.rsi.toFixed(0)}</span>`);
    }
    if (r.adx != null) {
      const adxCls = r.adx >= 25 ? "good" : r.adx < 20 ? "warn" : "";
      chips.push(`<span class="chip ${adxCls}">ADX ${r.adx.toFixed(0)}</span>`);
    }
    if (r.trend) {
      const arrow = r.trend === "up" ? "▲" : r.trend === "down" ? "▼" : "→";
      const tCls = r.trend === "up" ? "good" : r.trend === "down" ? "bad" : "";
      chips.push(`<span class="chip ${tCls}">${arrow} trend</span>`);
    }
    if (r.macd_hist != null) {
      const mCls = r.macd_hist > 0 ? "good" : "bad";
      chips.push(`<span class="chip ${mCls}">MACD ${r.macd_hist >= 0 ? "+" : ""}${r.macd_hist.toFixed(3)}</span>`);
    }
    if (r.news_sentiment != null && Math.abs(r.news_sentiment) > 0.1) {
      const nCls = r.news_sentiment > 0 ? "good" : "bad";
      chips.push(`<span class="chip ${nCls}">news ${r.news_sentiment >= 0 ? "+" : ""}${r.news_sentiment.toFixed(2)}</span>`);
    }

    return `
      <div class="market-card ${cls}${selected}" data-symbol="${r.symbol}">
        <div class="market-sym">${r.symbol}</div>
        <div class="market-price">${fmtPrice(r.last)}</div>
        <div class="market-change ${classPN(change)}">${change >= 0 ? "+" : ""}${change.toFixed(2)}%</div>
        <div class="market-vol">vol ${r.volume_24h ? (r.volume_24h / 1_000_000).toFixed(1) + "M" : "—"}</div>
        <div class="chips">${chips.join("")}</div>
      </div>
    `;
  }).join("");
  host.querySelectorAll(".market-card").forEach(card => {
    card.addEventListener("click", () => {
      _tvSymbol = card.getAttribute("data-symbol");
      document.getElementById("tv-symbol").value = _tvSymbol;
      mountTradingView(_tvSymbol, _tvInterval);
      host.querySelectorAll(".market-card").forEach(c => c.classList.remove("selected"));
      card.classList.add("selected");
    });
  });
  // Populate the tv-symbol dropdown. We refresh it on every render so that
  // newly-discovered MEXC symbols (auto-discovery adds top-N pairs from the
  // exchange) appear in the dropdown without needing a page reload. Listener
  // attachment is gated to once.
  const sel = document.getElementById("tv-symbol");
  if (sel) {
    const wanted = rows.map(r => r.symbol);
    const current = Array.from(sel.options).map(o => o.value);
    const same = wanted.length === current.length &&
                 wanted.every((s, i) => s === current[i]);
    if (!same) {
      sel.innerHTML = rows.map(r =>
        `<option value="${r.symbol}"${r.symbol === _tvSymbol ? " selected" : ""}>${r.symbol}</option>`
      ).join("");
      sel.value = _tvSymbol;
    }
    if (!sel._wired) {
      sel.addEventListener("change", (e) => {
        _tvSymbol = e.target.value;
        mountTradingView(_tvSymbol, _tvInterval);
      });
      document.getElementById("tv-interval").addEventListener("change", (e) => {
        _tvInterval = e.target.value;
        mountTradingView(_tvSymbol, _tvInterval);
      });
      sel._wired = true;
    }
  }
}

function renderStatusFlags(ov) {
  const host = document.getElementById("status-flags");
  const pills = [];
  pills.push(`<span class="pill ${ov.paper_mode ? "ok" : "bad"}">${ov.paper_mode ? "PAPER" : "LIVE"}</span>`);
  const marketLabel = (ov.market || "spot").toUpperCase();
  pills.push(`<span class="pill">${marketLabel}${ov.leverage ? " · " + ov.leverage + "x" : ""}${ov.margin_mode ? " · " + ov.margin_mode : ""}</span>`);
  pills.push(`<span class="pill">${ov.signal_mode}</span>`);
  pills.push(`<span class="pill">${ov.data_source} · ${ov.timeframe}</span>`);
  pills.push(`<span class="pill">${(ov.symbols || []).length} symbols · max ${ov.max_open_positions || "—"} open</span>`);
  if (ov.kill_switch?.enabled) {
    pills.push(`<span class="pill bad">KILL SWITCH</span>`);
  }
  host.innerHTML = pills.join("");
}

function renderKpis(ov) {
  document.getElementById("kpi-equity").textContent = fmtUsd(ov.equity_usdt);
  document.getElementById("kpi-equity-sub").textContent = `from ${fmtUsd(ov.starting_equity_usdt)} start`;

  const total = document.getElementById("kpi-total-pnl");
  total.textContent = fmtSignedUsd(ov.total_pnl_usdt);
  total.className = "kpi-value " + classPN(ov.total_pnl_usdt);
  document.getElementById("kpi-total-pnl-pct").textContent = fmtPct(ov.total_pnl_pct);

  const realized = document.getElementById("kpi-realized-pnl");
  realized.textContent = fmtSignedUsd(ov.realized_pnl_usdt);
  realized.className = "kpi-value " + classPN(ov.realized_pnl_usdt);

  const unr = document.getElementById("kpi-unrealized-pnl");
  unr.textContent = fmtSignedUsd(ov.unrealized_pnl_usdt);
  unr.className = "kpi-value " + classPN(ov.unrealized_pnl_usdt);
  document.getElementById("kpi-open-count").textContent = `${ov.open_positions_count} open`;

  document.getElementById("kpi-cash").textContent = fmtUsd(ov.cash_usdt);
  document.getElementById("kpi-open-notional").textContent = `${fmtUsd(ov.open_notional_usdt)} in positions`;
}

function renderPositions(rows) {
  document.getElementById("positions-count").textContent = rows.length;
  const tbody = document.querySelector("#positions-table tbody");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="11" class="muted">no open positions</td></tr>`;
    return;
  }
  // Annotate target cells with the % distance from mark, so the user sees
  // "TP1 0.1410 (+3.2%)" — they know how far the price has to move before
  // a target fires. tp1_hit / tp2_hit show as ✓ markers.
  const cell = (price, distPct, hit) => {
    if (price == null) return `<span class="muted">—</span>`;
    const tag = hit ? ` <span class="tag good">✓</span>` : "";
    const dist = (distPct == null) ? "" :
      ` <span class="muted">(${distPct >= 0 ? "+" : ""}${distPct.toFixed(2)}%)</span>`;
    return `${fmtPrice(price)}${dist}${tag}`;
  };
  tbody.innerHTML = rows.map(p => `
    <tr>
      <td><strong>${p.symbol}</strong>${p.adds ? ` <span class="muted">+${p.adds}</span>` : ""}</td>
      <td><span class="tag ${p.side}">${p.side}</span></td>
      <td>${fmtTimeWithAge(p.opened_at)}</td>
      <td class="num">${fmtQty(p.qty)}</td>
      <td class="num">${fmtPrice(p.avg_entry)}</td>
      <td class="num">${fmtPrice(p.mark_price)}</td>
      <td class="num">${cell(p.sl_price, p.dist_to_sl_pct, false)}</td>
      <td class="num">${cell(p.tp1_price, p.dist_to_tp1_pct, p.tp1_hit)}</td>
      <td class="num">${cell(p.tp2_price, p.dist_to_tp2_pct, p.tp2_hit)}</td>
      <td class="num ${classPN(p.unrealized_pnl_usdt)}">${fmtSignedUsd(p.unrealized_pnl_usdt)}</td>
      <td class="num ${classPN(p.unrealized_pnl_pct)}">${fmtPct(p.unrealized_pnl_pct)}</td>
    </tr>
  `).join("");
}

function renderDecisions(rows) {
  document.getElementById("decisions-count").textContent = rows.length;
  const tbody = document.querySelector("#decisions-table tbody");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">no decisions yet — run a tick</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.slice(0, 20).map(d => `
    <tr>
      <td>${fmtTimeWithAge(d.ts)}</td>
      <td><strong>${d.symbol}</strong></td>
      <td><span class="tag ${d.action}">${d.action}</span></td>
      <td class="num">${fmtPrice(d.last_close)}</td>
      <td class="muted">${(d.reasoning || "").slice(0, 80)}</td>
    </tr>
  `).join("");
}

function renderTrades(rows) {
  document.getElementById("trades-count").textContent = rows.length;
  const tbody = document.querySelector("#trades-table tbody");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">no trades yet</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(t => `
    <tr>
      <td>${fmtTimeWithAge(t.ts)}</td>
      <td><strong>${t.symbol}</strong></td>
      <td><span class="tag ${t.side}">${t.side}</span></td>
      <td class="num">${fmtQty(t.filled_amount || t.amount)}</td>
      <td class="num">${fmtPrice(t.avg_price || t.price)}</td>
      <td><span class="tag ${t.status}">${t.status}</span></td>
    </tr>
  `).join("");
}

function renderEquityChart(data) {
  const svg = document.getElementById("equity-chart");
  const points = data.points || [];
  document.getElementById("equity-points-count").textContent = `${points.length} points`;
  svg.innerHTML = "";
  if (points.length < 2) {
    svg.innerHTML = `<text x="400" y="110" text-anchor="middle" class="chart-label">no trades yet — curve appears after first tick</text>`;
    return;
  }

  const W = 800, H = 220, pad = { top: 10, right: 40, bottom: 22, left: 50 };
  const xs = points.map(p => new Date(p.ts).getTime());
  const ys = points.map(p => p.equity);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const yPad = (yMax - yMin) * 0.1 || yMax * 0.01 || 1;
  const y0 = yMin - yPad, y1 = yMax + yPad;

  const xMap = x => pad.left + ((x - xMin) / (xMax - xMin || 1)) * (W - pad.left - pad.right);
  const yMap = y => H - pad.bottom - ((y - y0) / (y1 - y0 || 1)) * (H - pad.top - pad.bottom);

  const pathD = points.map((p, i) => {
    const x = xMap(xs[i]), y = yMap(ys[i]);
    return (i === 0 ? "M" : "L") + x.toFixed(1) + "," + y.toFixed(1);
  }).join(" ");
  const areaD = pathD + ` L${xMap(xMax).toFixed(1)},${(H - pad.bottom).toFixed(1)} L${xMap(xMin).toFixed(1)},${(H - pad.bottom).toFixed(1)} Z`;

  const ticks = 4;
  const gridLines = [];
  for (let i = 0; i <= ticks; i++) {
    const yv = y0 + (i / ticks) * (y1 - y0);
    const yp = yMap(yv);
    gridLines.push(`<line class="chart-axis" x1="${pad.left}" x2="${W - pad.right}" y1="${yp}" y2="${yp}" stroke-dasharray="2,4" opacity="0.3" />`);
    gridLines.push(`<text class="chart-label" x="${pad.left - 6}" y="${yp + 3}" text-anchor="end">$${yv.toFixed(0)}</text>`);
  }
  const xStart = new Date(xMin).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const xEnd = new Date(xMax).toLocaleDateString(undefined, { month: "short", day: "numeric" });

  svg.innerHTML = `
    ${gridLines.join("")}
    <path class="chart-area" d="${areaD}" />
    <path class="chart-line" d="${pathD}" />
    <text class="chart-label" x="${pad.left}" y="${H - 6}">${xStart}</text>
    <text class="chart-label" x="${W - pad.right}" y="${H - 6}" text-anchor="end">${xEnd}</text>
  `;
}

function renderStats(st) {
  document.getElementById("stats-count").textContent = `${st.trades} closed trades`;
  document.getElementById("stat-winrate").textContent = st.trades ? st.win_rate_pct.toFixed(1) + "%" : "—";
  document.getElementById("stat-wins-losses").textContent = `${st.wins}W · ${st.losses}L`;
  document.getElementById("stat-pf").textContent = st.profit_factor_inf
    ? "∞" : (st.profit_factor ? st.profit_factor.toFixed(2) : "—");
  document.getElementById("stat-expectancy").textContent = st.trades ? fmtSignedUsd(st.expectancy_usdt) : "—";
  document.getElementById("stat-avgwin").textContent = st.wins ? fmtSignedUsd(st.avg_win_usdt) : "—";
  document.getElementById("stat-avghold").textContent = st.trades
    ? `~${st.avg_hold_minutes.toFixed(0)}min avg hold` : "—";
  document.getElementById("stat-avgloss").textContent = st.losses ? fmtSignedUsd(st.avg_loss_usdt) : "—";
  document.getElementById("stat-dd").textContent = st.trades ? fmtUsd(st.max_drawdown_usdt) : "—";
  document.getElementById("stat-dd-pct").textContent = st.trades ? `${st.max_drawdown_pct.toFixed(2)}%` : "—";
  document.getElementById("stat-commission").textContent = st.trades ? "-" + fmtUsd(st.total_commission_usdt) : "—";
  document.getElementById("stat-funding").textContent = fmtSignedUsd(st.total_funding_usdt);

  // Promotion gauge.
  const target = st.min_win_rate_for_promotion_pct;
  const minTrades = st.min_trades_for_promotion;
  const minPf = st.min_profit_factor_for_promotion;
  const progressRate = Math.min(100, (st.win_rate_pct / target) * 100);
  const tradesProgress = Math.min(100, (st.trades / minTrades) * 100);
  const overall = Math.min(progressRate, tradesProgress);
  document.getElementById("promote-progress").style.width = overall.toFixed(1) + "%";
  document.getElementById("promote-progress-text").textContent =
    `${st.trades}/${minTrades} trades · ${st.win_rate_pct.toFixed(1)}%/${target}% win · PF ${st.profit_factor_inf ? "∞" : (st.profit_factor?.toFixed(2) || "—")}/${minPf}`;

  const banner = document.getElementById("promote-banner");
  if (st.ready_for_live) {
    banner.style.display = "block";
    document.getElementById("promote-text").textContent =
      `${st.trades} trades · ${st.win_rate_pct.toFixed(1)}% win rate · profit factor ${st.profit_factor?.toFixed(2) || "∞"}. Time to consider going live.`;
  } else {
    banner.style.display = "none";
  }

  // Per-symbol table.
  const tbody = document.querySelector("#by-symbol-table tbody");
  if (!st.by_symbol.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">no closed trades yet</td></tr>`;
  } else {
    tbody.innerHTML = st.by_symbol.map(r => `
      <tr>
        <td><strong>${r.symbol}</strong></td>
        <td class="num">${r.trades}</td>
        <td class="num">${r.win_rate_pct.toFixed(1)}%</td>
        <td class="num ${classPN(r.pnl_usdt)}">${fmtSignedUsd(r.pnl_usdt)}</td>
      </tr>
    `).join("");
  }
}

function renderSignals(rows) {
  document.getElementById("signals-count").textContent = rows.length;
  const host = document.getElementById("signals-cards");
  if (!rows.length) {
    host.innerHTML = `<div class="muted" style="padding: 20px;">no actionable signals yet — let the scheduler run a few ticks.</div>`;
    return;
  }
  host.innerHTML = rows.slice(0, 12).map(s => {
    const conf = Math.round((s.confidence || 0) * 100);
    const rr = s.rr_ratio ? `1 : ${s.rr_ratio.toFixed(2)}` : "—";
    const plainText = [
      `${s.symbol} ${s.side}`,
      `Entry: ${fmtPrice(s.entry_price)}`,
      `Stop: ${fmtPrice(s.stop_loss)}`,
      `Take Profit: ${fmtPrice(s.take_profit)}`,
      `Size: ${s.size_pct_of_equity?.toFixed(2)}% of equity`,
      `Confidence: ${conf}%`,
      `Reasoning: ${s.reasoning}`,
    ].join("\n");
    return `
      <div class="signal-card ${s.side.toLowerCase()}">
        <div class="signal-head">
          <div class="signal-symbol">${s.symbol}</div>
          <div class="signal-side ${s.side.toLowerCase()}">${s.side}</div>
        </div>
        <div class="signal-rows">
          <span class="k">Entry</span><span class="v">${fmtPrice(s.entry_price)}</span>
          <span class="k">Stop loss</span><span class="v neg">${fmtPrice(s.stop_loss)}</span>
          <span class="k">Take profit</span><span class="v pos">${fmtPrice(s.take_profit)}</span>
          <span class="k">R:R</span><span class="v">${rr}</span>
          <span class="k">Size</span><span class="v">${s.size_pct_of_equity?.toFixed(2)}%</span>
          <span class="k">Confidence</span><span class="v">${conf}%</span>
        </div>
        <div class="conf-bar"><div class="conf-fill" style="width:${conf}%"></div></div>
        <div class="signal-meta">${fmtTime(s.ts)} — ${(s.reasoning || "").slice(0, 70)}</div>
        <button class="signal-copy" data-payload="${plainText.replace(/"/g, "&quot;")}">Copy order details</button>
      </div>
    `;
  }).join("");
  host.querySelectorAll(".signal-copy").forEach(btn => {
    btn.addEventListener("click", async () => {
      const payload = btn.getAttribute("data-payload").replace(/&quot;/g, '"');
      try {
        await navigator.clipboard.writeText(payload);
        btn.textContent = "Copied ✓";
        btn.classList.add("copied");
        setTimeout(() => { btn.textContent = "Copy order details"; btn.classList.remove("copied"); }, 1800);
      } catch (e) { alert("copy failed: " + e.message); }
    });
  });
}

function renderFearGreed(ov) {
  const fg = ov.fear_greed;
  const fill = document.getElementById("fg-fill");
  const valueEl = document.getElementById("fg-value");
  const labelEl = document.getElementById("fg-label");
  const fetchedEl = document.getElementById("fg-fetched");
  const nudgeEl = document.getElementById("fg-nudge");
  if (!fg) {
    valueEl.textContent = "—";
    labelEl.textContent = "awaiting first fetch";
    fill.style.left = "50%";
    fetchedEl.textContent = "—";
    nudgeEl.textContent = "F&G adjusts signal confidence at extremes (<20 fear, >80 greed).";
    return;
  }
  const v = fg.value;
  valueEl.textContent = v.toFixed(0);
  labelEl.textContent = fg.classification || "—";
  fill.style.left = `${Math.max(0, Math.min(100, v))}%`;
  fetchedEl.textContent = `updated ${fmtTime(fg.fetched_at)}`;
  let nudge = "Neutral — no confidence adjustment.";
  if (v >= 80) nudge = "Extreme greed — longs are dampened, shorts boosted.";
  else if (v <= 20) nudge = "Extreme fear — longs boosted, shorts dampened.";
  nudgeEl.textContent = nudge;
}

function renderBacktest(bt) {
  document.getElementById("bt-symbols").textContent = bt.symbols_covered;
  document.getElementById("bt-trades").textContent = bt.total_trades;
  document.getElementById("bt-winrate").textContent = bt.total_trades
    ? bt.overall_win_rate_pct.toFixed(1) + "%" : "—";
  const pnlEl = document.getElementById("bt-pnl");
  pnlEl.textContent = bt.symbols_covered ? fmtSignedPct(bt.avg_pnl_pct) : "—";
  pnlEl.className = "stat-value " + classPN(bt.avg_pnl_pct);

  const st = bt.state || {};
  let label;
  if (st.running) {
    label = `running ${st.completed}/${st.total} · ${st.last_symbol || "…"}`;
  } else if (bt.generated_at) {
    label = `generated ${fmtTime(bt.generated_at)}`;
  } else {
    label = "not yet run";
  }
  document.getElementById("backtest-generated").textContent = label;
  document.getElementById("backtest-count").textContent = bt.reports.length;

  const tbody = document.querySelector("#backtest-table tbody");
  if (!bt.reports.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="muted">no backtests yet — click <em>Run backtest</em></td></tr>`;
    return;
  }
  tbody.innerHTML = bt.reports.map(r => {
    const isWR = r.is_win_rate_pct ?? r.win_rate_pct;
    const oosWR = r.oos_win_rate_pct;
    const isPF = r.is_profit_factor ?? r.profit_factor;
    const oosPF = r.oos_profit_factor;
    // "Honest?" flag: green if IS-OOS gap < 15pp, amber if 15-30, red if > 30.
    let honest = "—";
    let honestCls = "muted";
    if (oosWR !== null && oosWR !== undefined && isWR != null) {
      const gap = Math.abs(isWR - oosWR);
      if (gap < 15) { honest = "✓ real"; honestCls = "pos"; }
      else if (gap < 30) { honest = "⚠ maybe"; honestCls = "warn"; }
      else { honest = "✗ overfit"; honestCls = "neg"; }
    }
    return `
      <tr>
        <td><strong>${r.symbol}</strong></td>
        <td class="num">${r.trades}</td>
        <td class="num">${r.trades ? isWR.toFixed(1) + "%" : "—"}</td>
        <td class="num">${isPF !== null && isPF !== undefined ? isPF.toFixed(2) : "—"}</td>
        <td class="num">${r.oos_trades ?? "—"}</td>
        <td class="num">${oosWR !== null && oosWR !== undefined ? oosWR.toFixed(1) + "%" : "—"}</td>
        <td class="num">${oosPF !== null && oosPF !== undefined ? oosPF.toFixed(2) : "—"}</td>
        <td class="num ${classPN(r.oos_net_pnl_pct || 0)}">${r.oos_net_pnl_pct !== null ? fmtSignedPct(r.oos_net_pnl_pct) : "—"}</td>
        <td class="num ${honestCls}">${honest}</td>
      </tr>
    `;
  }).join("");
}

const fmtSignedPct = (n) => {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
};

// Alert feed state — we dedupe browser notifications by alert id.
let _seenAlertIds = new Set();
let _audioCtx = null;

function beep() {
  try {
    if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const o = _audioCtx.createOscillator();
    const g = _audioCtx.createGain();
    o.connect(g); g.connect(_audioCtx.destination);
    o.frequency.value = 880; o.type = "triangle";
    g.gain.setValueAtTime(0.08, _audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, _audioCtx.currentTime + 0.25);
    o.start(); o.stop(_audioCtx.currentTime + 0.25);
  } catch (e) { /* audio unsupported — silent */ }
}

function browserNotify(alert) {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try {
    new Notification(alert.title, {
      body: alert.message || "",
      tag: `tbtc-${alert.id}`,
      icon: "/ui/favicon.ico",
    });
  } catch (e) { /* ignore */ }
}

function renderAlerts(data) {
  const feed = document.getElementById("alerts-feed");
  const badge = document.getElementById("alerts-unread");
  const items = data.items || [];
  const unread = data.unread || 0;

  if (unread > 0) {
    badge.style.display = "inline-block";
    badge.textContent = unread;
  } else {
    badge.style.display = "none";
  }

  // Fire browser notifications + beep for any alert IDs we haven't seen yet.
  // Skip the first refresh so we don't dump 50 notifications at page load.
  if (_seenAlertIds.size > 0) {
    const fresh = items.filter(a => !_seenAlertIds.has(a.id) && !a.read);
    if (fresh.length > 0) {
      beep();
      fresh.slice(0, 3).forEach(browserNotify);  // cap bursts at 3
    }
  }
  items.forEach(a => _seenAlertIds.add(a.id));

  if (!items.length) {
    feed.innerHTML = `<div class="muted" style="padding:14px;">no alerts yet — signals and position events will show up here as they happen.</div>`;
    return;
  }
  feed.innerHTML = items.slice(0, 40).map(a => {
    const clock = fmtClock(a.ts);
    const ago = fmtAge(a.ts);
    const severity = a.severity || "info";
    const unreadCls = a.read ? "" : "unread";
    const styleBadge = a.style
      ? `<span class="style-badge style-${a.style.toLowerCase()}">${a.style}</span>`
      : "";
    // For SIGNAL kind, render structured price levels prominently so the user
    // can copy them to their exchange without parsing prose.
    let levels = "";
    if (a.kind === "SIGNAL" && a.price !== null && a.price !== undefined) {
      const px = (v) => v == null ? "—" : fmtPrice(v);
      levels = `
        <div class="alert-levels">
          <span class="lvl"><b>Entry</b> ${px(a.price)}</span>
          <span class="lvl neg"><b>SL</b> ${px(a.sl)}</span>
          <span class="lvl pos"><b>TP1</b> ${px(a.tp1)}</span>
          <span class="lvl pos"><b>TP2</b> ${px(a.tp2)}</span>
          ${a.confidence != null ? `<span class="lvl"><b>Conf</b> ${Math.round(a.confidence * 100)}%</span>` : ""}
        </div>`;
    }
    return `
      <div class="alert-row ${severity} ${unreadCls}">
        <div class="alert-kind ${a.kind}">${a.kind}</div>
        <div class="alert-main">
          <div class="alert-title">${escapeHtml(a.title)}${styleBadge}</div>
          ${levels}
          <div class="alert-msg">${escapeHtml(a.message || "")}</div>
        </div>
        <div class="alert-time">
          <div class="alert-time-clock">${clock}</div>
          <div class="alert-time-age muted">${ago}</div>
        </div>
      </div>
    `;
  }).join("");
}

function relTime(iso) {
  if (!iso) return "—";
  const secs = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function enableBrowserAlerts() {
  if (typeof Notification === "undefined") {
    alert("This browser doesn't support native notifications.");
    return;
  }
  if (Notification.permission === "granted") {
    alert("Browser alerts are already enabled.");
    return;
  }
  const perm = await Notification.requestPermission();
  const btn = document.getElementById("btn-enable-browser-notify");
  if (perm === "granted") {
    btn.textContent = "Browser alerts ON";
    btn.disabled = true;
    new Notification("Terminal-BTC alerts enabled", {
      body: "You'll get a browser push on every signal, open, TP, SL, and close.",
    });
  } else {
    btn.textContent = "Enable browser alerts";
  }
}

async function markAllAlertsRead() {
  try {
    await fetchJSON("/control/notifications/mark-read", { method: "POST" });
    await refreshAll();
  } catch (e) { alert("failed: " + e.message); }
}

function renderOverrides(data) {
  const host = document.getElementById("overrides-list");
  const badge = document.getElementById("overrides-count");
  const items = data.items || [];
  if (items.length > 0) {
    badge.style.display = "inline-block";
    badge.textContent = items.length;
  } else {
    badge.style.display = "none";
  }
  if (!items.length) {
    host.innerHTML = `<div class="muted" style="padding:14px;">no active overrides yet — auditor runs nightly at 04:00 UTC (or click Run audit).</div>`;
    return;
  }
  host.innerHTML = items.slice(0, 15).map(o => {
    const exp = relTime(o.expires_at);
    return `
      <div class="override-row">
        <div class="override-sym">${escapeHtml(o.symbol)}</div>
        <span class="override-param">${escapeHtml(o.param_key)}=${escapeHtml(o.param_value)}</span>
        <div class="override-reason">${escapeHtml(o.reason)}</div>
        <div class="override-exp">${o.source}<br/>exp ${exp}</div>
      </div>
    `;
  }).join("");
}

function renderNews(rows) {
  const host = document.getElementById("news-feed");
  const countEl = document.getElementById("news-count");
  countEl.textContent = `${rows.length} headlines`;
  if (!rows.length) {
    host.innerHTML = `<div class="muted" style="padding:14px;">no news fetched yet — waiting for first refresh.</div>`;
    return;
  }
  host.innerHTML = rows.slice(0, 30).map(n => {
    const sent = n.sentiment_score || 0;
    const sentCls = sent > 0.1 ? "success" : sent < -0.1 ? "danger" : "info";
    const sentLabel = sent > 0.1 ? "BULL" : sent < -0.1 ? "BEAR" : "NEUTRAL";
    const sentBadgeCls = sent > 0.1 ? "TP1" : sent < -0.1 ? "SL" : "INFO";
    const ago = relTime(n.ts);
    const symbols = (n.currencies || []).slice(0, 3).join(" · ") || "—";
    return `
      <div class="alert-row ${sentCls}">
        <div class="alert-kind ${sentBadgeCls}">${sentLabel}</div>
        <div class="alert-main">
          <div class="alert-title"><a href="${escapeHtml(n.url)}" target="_blank" rel="noopener" style="color:var(--text);">${escapeHtml(n.title)}</a></div>
          <div class="alert-msg">${symbols}</div>
        </div>
        <div class="alert-time">${ago}</div>
      </div>
    `;
  }).join("");
}

async function runAudit() {
  const btn = document.getElementById("btn-audit");
  btn.disabled = true; btn.textContent = "Auditing…";
  try {
    await fetchJSON("/control/audit-now", { method: "POST" });
  } catch (e) { alert("audit failed: " + e.message); }
  finally {
    setTimeout(() => { btn.disabled = false; btn.textContent = "Run audit"; refreshAll(); }, 4000);
  }
}

// Fast standalone loop JUST for live prices — keeps the numbers ticking
// every 3s without re-running the full 10-endpoint refresh.
async function fastMarketsRefresh() {
  try {
    const markets = await fetchJSON("/api/markets");
    renderMarkets(markets);
  } catch (e) { /* silent — main refresh will surface errors */ }
}

async function refreshAll() {
  try {
    const [overview, positions, trades, decisions, equity, stats, signals, backtest, markets, alerts, overrides, newsFeed] = await Promise.all([
      fetchJSON("/api/overview"),
      fetchJSON("/api/positions"),
      fetchJSON("/api/trades?limit=50"),
      fetchJSON("/api/decisions?limit=30"),
      fetchJSON("/api/equity?days=30"),
      fetchJSON("/api/stats"),
      fetchJSON("/api/signals/latest?limit=20"),
      fetchJSON("/api/backtest"),
      fetchJSON("/api/markets"),
      fetchJSON("/api/notifications?limit=50"),
      fetchJSON("/api/overrides"),
      fetchJSON("/api/news?limit=30"),
    ]);
    window._tvSource = (overview.data_source || "bybit").toUpperCase();
    renderStatusFlags(overview);
    renderHeartbeat(overview);
    renderKpis(overview);
    renderFearGreed(overview);
    renderMarkets(markets);
    renderPositions(positions);
    renderTrades(trades);
    renderDecisions(decisions);
    renderEquityChart(equity);
    renderStats(stats);
    renderSignals(signals);
    renderBacktest(backtest);
    renderAlerts(alerts);
    renderOverrides(overrides);
    renderNews(newsFeed);

    // Sync Run-backtest button label with real backtest state.
    const btBtn = document.getElementById("btn-backtest");
    if (btBtn && overview.backtest) {
      if (overview.backtest.running) {
        btBtn.disabled = true;
        btBtn.textContent = `Backtesting ${overview.backtest.completed}/${overview.backtest.total}…`;
      } else if (btBtn.disabled && !btBtn.textContent.startsWith("Starting")) {
        btBtn.disabled = false;
        btBtn.textContent = "Run backtest";
      }
    }

    document.getElementById("last-refresh").textContent = `refreshed ${new Date().toLocaleTimeString()}`;

    // Auto-fire the first tick if the DB is truly empty, so new users see data
    // immediately instead of waiting 5 minutes for the scheduler.
    if (!window._autoTicked && !overview.last_decision_ts && !overview.scheduler?.last_tick_finished_at) {
      window._autoTicked = true;
      console.log("auto-firing first tick");
      fetch("/control/tick-now", { method: "POST" }).then(() => refreshAll());
    }
  } catch (e) {
    console.error(e);
    document.getElementById("last-refresh").textContent = `error: ${e.message}`;
  }
}

async function runBacktest() {
  const btn = document.getElementById("btn-backtest");
  btn.disabled = true; btn.textContent = "Starting…";
  try {
    await fetchJSON("/control/backtest-now", { method: "POST" });
    // Server runs it in the background; the dashboard's 10s poll will
    // surface progress via /api/overview.backtest.running.
    btn.textContent = "Backtesting…";
    await refreshAll();
  } catch (e) {
    alert("backtest failed to start: " + e.message);
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "Run backtest";
    }, 2000);
  }
}

async function refreshMacro() {
  const btn = document.getElementById("btn-macro");
  btn.disabled = true; btn.textContent = "Fetching…";
  try { await fetchJSON("/control/macro-refresh", { method: "POST" }); }
  catch (e) { alert("macro refresh failed: " + e.message); }
  finally { btn.disabled = false; btn.textContent = "Refresh F&G"; await refreshAll(); }
}

async function runTick() {
  const btn = document.getElementById("btn-tick");
  btn.disabled = true;
  btn.textContent = "Ticking…";
  try {
    const r = await fetchJSON("/control/tick-now", { method: "POST" });
    console.log("tick result", r);
  } catch (e) {
    console.error(e);
    alert("tick failed: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run tick now";
    await refreshAll();
  }
}

async function toggleKill(enabled) {
  const url = enabled ? "/control/kill" : "/control/resume";
  const body = enabled ? { reason: "via dashboard" } : {};
  try {
    await fetchJSON(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    alert("failed: " + e.message);
  }
  await refreshAll();
}

document.getElementById("btn-tick").addEventListener("click", runTick);
document.getElementById("btn-refresh").addEventListener("click", refreshAll);
document.getElementById("btn-backtest").addEventListener("click", runBacktest);
document.getElementById("btn-audit").addEventListener("click", runAudit);
document.getElementById("btn-macro").addEventListener("click", refreshMacro);
document.getElementById("btn-kill").addEventListener("click", () => toggleKill(true));
document.getElementById("btn-resume").addEventListener("click", () => toggleKill(false));
document.getElementById("btn-enable-browser-notify").addEventListener("click", enableBrowserAlerts);
document.getElementById("btn-mark-read").addEventListener("click", markAllAlertsRead);

// Reflect the current browser-notification permission state on the button.
if (typeof Notification !== "undefined" && Notification.permission === "granted") {
  const b = document.getElementById("btn-enable-browser-notify");
  b.textContent = "Browser alerts ON";
  b.disabled = true;
}

// Mount TradingView chart immediately with the default symbol. The chart
// source is derived from the symbol itself (see _chartSourceFor), so it
// doesn't depend on /api/overview having loaded yet.
mountTradingView(_tvSymbol, _tvInterval);

refreshAll();
setInterval(refreshAll, REFRESH_MS);
// Fast live-price loop — keeps the Market cards ticking every 3s.
setInterval(fastMarketsRefresh, MARKETS_REFRESH_MS);
