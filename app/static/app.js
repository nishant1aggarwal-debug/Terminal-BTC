// Terminal-BTC dashboard — vanilla JS, polls /api/* endpoints.

const REFRESH_MS = 10_000;

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
const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
};

const classPN = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "");

async function fetchJSON(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return await r.json();
}

function renderStatusFlags(ov) {
  const host = document.getElementById("status-flags");
  const pills = [];
  pills.push(`<span class="pill ${ov.paper_mode ? "ok" : "bad"}">${ov.paper_mode ? "PAPER" : "LIVE"}</span>`);
  pills.push(`<span class="pill">${ov.signal_mode}</span>`);
  pills.push(`<span class="pill">${ov.data_source} · ${ov.timeframe}</span>`);
  pills.push(`<span class="pill">${(ov.symbols || []).length} symbols</span>`);
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
    tbody.innerHTML = `<tr><td colspan="7" class="muted">no open positions</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(p => `
    <tr>
      <td><strong>${p.symbol}</strong></td>
      <td><span class="tag ${p.side}">${p.side}</span></td>
      <td class="num">${fmtQty(p.qty)}</td>
      <td class="num">${fmtPrice(p.avg_entry)}</td>
      <td class="num">${fmtPrice(p.mark_price)}</td>
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
      <td>${fmtTime(d.ts)}</td>
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
      <td>${fmtTime(t.ts)}</td>
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
  document.getElementById("backtest-generated").textContent = bt.generated_at
    ? `generated ${fmtTime(bt.generated_at)}` : "not yet run";
  document.getElementById("backtest-count").textContent = bt.reports.length;

  const tbody = document.querySelector("#backtest-table tbody");
  if (!bt.reports.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">no backtests yet — click <em>Run backtest</em></td></tr>`;
    return;
  }
  tbody.innerHTML = bt.reports.map(r => `
    <tr>
      <td><strong>${r.symbol}</strong></td>
      <td class="num">${r.trades}</td>
      <td class="num">${r.trades ? r.win_rate_pct.toFixed(1) + "%" : "—"}</td>
      <td class="num">${r.profit_factor !== null && r.profit_factor !== undefined ? r.profit_factor.toFixed(2) : "—"}</td>
      <td class="num ${classPN(r.net_pnl_pct)}">${fmtSignedPct(r.net_pnl_pct)}</td>
      <td class="num neg">${r.max_drawdown_pct.toFixed(2)}%</td>
      <td class="num">${r.avg_hold_minutes.toFixed(0)}m</td>
    </tr>
  `).join("");
}

const fmtSignedPct = (n) => {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
};

async function refreshAll() {
  try {
    const [overview, positions, trades, decisions, equity, stats, signals, backtest] = await Promise.all([
      fetchJSON("/api/overview"),
      fetchJSON("/api/positions"),
      fetchJSON("/api/trades?limit=50"),
      fetchJSON("/api/decisions?limit=30"),
      fetchJSON("/api/equity?days=30"),
      fetchJSON("/api/stats"),
      fetchJSON("/api/signals/latest?limit=20"),
      fetchJSON("/api/backtest"),
    ]);
    renderStatusFlags(overview);
    renderKpis(overview);
    renderFearGreed(overview);
    renderPositions(positions);
    renderTrades(trades);
    renderDecisions(decisions);
    renderEquityChart(equity);
    renderStats(stats);
    renderSignals(signals);
    renderBacktest(backtest);
    document.getElementById("last-refresh").textContent = `refreshed ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    console.error(e);
    document.getElementById("last-refresh").textContent = `error: ${e.message}`;
  }
}

async function runBacktest() {
  const btn = document.getElementById("btn-backtest");
  btn.disabled = true; btn.textContent = "Backtesting…";
  try { await fetchJSON("/control/backtest-now", { method: "POST" }); }
  catch (e) { alert("backtest failed: " + e.message); }
  finally { btn.disabled = false; btn.textContent = "Run backtest"; await refreshAll(); }
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
document.getElementById("btn-macro").addEventListener("click", refreshMacro);
document.getElementById("btn-kill").addEventListener("click", () => toggleKill(true));
document.getElementById("btn-resume").addEventListener("click", () => toggleKill(false));

refreshAll();
setInterval(refreshAll, REFRESH_MS);
