/* OptionDesk dashboard -- vanilla JS, no external dependencies. */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const fmt = (n, d = 0) => (n === null || n === undefined || Number.isNaN(n)) ? "—"
  : Number(n).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (n, d = 0) => (n === null || n === undefined || Number.isNaN(n)) ? "—" : `${(n * 100).toFixed(d)}%`;

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

/* ------------------------------------------------------------------ tabs */
$$(".tab").forEach(btn => btn.addEventListener("click", () => {
  $$(".tab").forEach(b => b.classList.toggle("active", b === btn));
  $$(".panel").forEach(p => p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`));
  const t = btn.dataset.tab;
  if (t === "signals") loadSignals();
  if (t === "chain") loadChain();
  if (t === "stocks") loadStocks();
  if (t === "rules") { loadRules(); loadSettings(); }
}));

/* ---------------------------------------------------------------- health */
async function refreshHealth() {
  try {
    const h = await api("/api/health");
    const set = (id, text, cls) => { const el = $(id); el.textContent = text; el.className = `pill ${cls || ""}`; };
    set("#provider-pill", `provider: ${h.provider}`, h.ok ? "ok" : "bad");
    set("#market-pill", `market: ${h.market_open ? "OPEN" : "closed"}`, h.market_open ? "ok" : "");
    set("#clock-pill", h.clock || h.ist_now);
    set("#tg-pill", `telegram: ${h.telegram_configured ? "on" : "off"}`, h.telegram_configured ? "ok" : "");
    $("#advance-btn").style.display = h.provider === "replay" ? "" : "none";
    if (h.last_scan) {
      $("#scan-meta").textContent =
        `Last scan ${h.last_scan.ts} · ${h.last_scan.scanned} instruments · ` +
        `${h.last_scan.n_signals} setups · ${h.last_scan.duration_s}s`;
    }
  } catch (e) { $("#provider-pill").textContent = `health error: ${e.message}`; }
}

$("#scan-btn").addEventListener("click", async () => {
  $("#scan-btn").textContent = "Scanning…";
  try { await api("/api/scan", { method: "POST", body: JSON.stringify({}) }); await loadSignals(); }
  catch (e) { alert(e.message); }
  finally { $("#scan-btn").textContent = "Scan now"; refreshHealth(); }
});

$("#advance-btn").addEventListener("click", async () => {
  await api("/api/clock/advance?minutes=5", { method: "POST" });
  refreshHealth();
  if ($("#tab-chain").classList.contains("active")) loadChain();
});

/* --------------------------------------------------------------- signals */
async function loadSignals() {
  const kind = $("#signal-kind").value;
  let rows = [];
  try { rows = await api(`/api/signals?limit=30${kind ? `&kind=${kind}` : ""}`); }
  catch (e) { $("#signals-list").innerHTML = `<p class="muted">${e.message}</p>`; return; }
  if (!rows.length) {
    $("#signals-list").innerHTML =
      `<p class="muted">No setup currently meets the filters. That is a valid answer: the rules
       exist to keep you out of trades where you have no edge. Press <b>Scan now</b> to re-check,
       or tune the thresholds under Rules &amp; settings.</p>`;
    return;
  }
  $("#signals-list").innerHTML = "";
  rows.forEach(sig => $("#signals-list").appendChild(signalCard(sig)));
}
$("#signal-kind").addEventListener("change", loadSignals);

function signalCard(sig) {
  const el = document.createElement("div");
  el.className = "card";
  const p = sig.plan || {};
  const legs = (p.legs || []).map(l => `
    <tr><td>${l.side === "SELL" ? "SELL" : "BUY"}</td>
        <td class="${l.option_type === "CE" ? "ce" : "pe"}">${l.option_type}</td>
        <td>${fmt(l.strike)}</td><td>${fmt(l.premium, 2)}</td>
        <td>${fmt(l.lots)} lot / ${fmt(l.quantity)} qty</td>
        <td>${(l.delta || 0).toFixed(2)}Δ</td></tr>`).join("");
  const be = (p.breakevens || []).map(b => fmt(b, 1)).join(" / ") || "—";
  el.innerHTML = `
    <h2>${sig.headline}
      <span class="badge score">${fmt(sig.score)} / 100</span>
      <span class="badge ${sig.direction}">${sig.direction}</span></h2>
    <div class="muted">${sig.kind.replace("_", " ")} · IVR ${fmt(sig.iv_context?.iv_rank, 0)} ·
      IVP ${fmt(sig.iv_context?.iv_percentile, 0)} · ${p.dte} DTE · lot ${fmt(p.lot_size)}</div>
    <div class="metrics">
      <div class="metric"><div class="k">${p.net_credit >= 0 ? "net credit" : "net cost"}</div>
        <div class="v">${fmt(Math.abs(p.net_credit || 0), 2)}</div></div>
      <div class="metric"><div class="k">max profit</div><div class="v" style="color:var(--green)">${fmt(p.max_profit)}</div></div>
      <div class="metric"><div class="k">max loss</div><div class="v" style="color:var(--red)">${fmt(p.max_loss)}</div></div>
      <div class="metric"><div class="k">R : R</div><div class="v">${fmt(p.risk_reward, 2)}</div></div>
      <div class="metric"><div class="k">P(short OTM)</div><div class="v">${pct(p.prob_short_otm)}</div></div>
      <div class="metric"><div class="k">P(max profit)</div><div class="v">${pct(p.prob_max_profit)}</div></div>
      <div class="metric"><div class="k">breakevens</div><div class="v">${be}</div></div>
      <div class="metric"><div class="k">margin est.</div><div class="v">${fmt(p.margin_estimate)}</div></div>
    </div>
    <table class="grid"><thead><tr><th>Side</th><th>Type</th><th>Strike</th><th>Price</th><th>Size</th><th>Delta</th></tr></thead>
      <tbody>${legs}</tbody></table>
    <div class="chart">${payoffSVG(p)}</div>
    <ul class="why">${(sig.reasons || []).slice(0, 7).map(r => `<li>${r}</li>`).join("")}</ul>
    <ul class="exits">${(p.exit_rules || []).map(r => `<li>${r.name}: ${r.description}</li>`).join("")}</ul>
    ${(p.warnings || []).length ? `<div class="warn">⚠ ${(p.warnings || []).join(" · ")}</div>` : ""}`;
  return el;
}

/* ---------------------------------------------------------- payoff chart */
function payoffSVG(plan, opts = {}) {
  const prices = plan.payoff?.prices || opts.prices || [];
  const pnl = plan.payoff?.pnl || opts.pnl || [];
  if (prices.length < 3) return "";
  const W = 620, H = 210, padL = 52, padR = 12, padT = 10, padB = 22;
  const xmin = Math.min(...prices), xmax = Math.max(...prices);
  let ymin = Math.min(...pnl, 0), ymax = Math.max(...pnl, 0);
  const padY = (ymax - ymin) * 0.08 || 1;
  ymin -= padY; ymax += padY;
  const X = v => padL + (v - xmin) / (xmax - xmin) * (W - padL - padR);
  const Y = v => padT + (ymax - v) / (ymax - ymin) * (H - padT - padB);
  const pts = prices.map((p, i) => [X(p), Y(pnl[i])]);
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const zeroY = Y(0);

  // filled regions split at zero crossings
  const above = [], below = [];
  for (let i = 1; i < pts.length; i++) {
    const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
    const v0 = pnl[i - 1], v1 = pnl[i];
    const target = v0 >= 0 && v1 >= 0 ? above : (v0 <= 0 && v1 <= 0 ? below : null);
    if (target) target.push([x0, y0, x1, y1]);
    else { // crossing: interpolate
      const t = v0 / (v0 - v1);
      const xc = x0 + (x1 - x0) * t;
      (v0 > 0 ? above : below).push([x0, y0, xc, zeroY]);
      (v1 > 0 ? above : below).push([xc, zeroY, x1, y1]);
    }
  }
  const poly = segs => segs.map(([x0, y0, x1, y1]) =>
    `<polygon points="${x0.toFixed(1)},${y0.toFixed(1)} ${x1.toFixed(1)},${y1.toFixed(1)} ${x1.toFixed(1)},${zeroY.toFixed(1)} ${x0.toFixed(1)},${zeroY.toFixed(1)}" />`).join("");

  const markers = (plan.breakevens || []).map(b => {
    if (b < xmin || b > xmax) return "";
    return `<line x1="${X(b)}" x2="${X(b)}" y1="${padT}" y2="${H - padB}" stroke="#f0b429" stroke-dasharray="3 3" opacity=".7"/>
            <text x="${X(b) + 3}" y="${padT + 10}">BE ${fmt(b)}</text>`;
  }).join("");
  const spot = plan.spot || opts.spot;
  const spotLine = (spot && spot >= xmin && spot <= xmax)
    ? `<line x1="${X(spot)}" x2="${X(spot)}" y1="${padT}" y2="${H - padB}" stroke="#4da3ff" stroke-dasharray="5 3"/>
       <text x="${X(spot) + 3}" y="${H - padB - 4}" fill="#4da3ff">spot ${fmt(spot)}</text>` : "";

  return `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="none" style="margin-top:10px">
    <g fill="#2ecc71" opacity=".18">${poly(above)}</g>
    <g fill="#ff5c5c" opacity=".18">${poly(below)}</g>
    <line x1="${padL}" x2="${W - padR}" y1="${zeroY}" y2="${zeroY}" stroke="#8b98a5" stroke-width="1"/>
    <polyline points="${line}" fill="none" stroke="#e6edf3" stroke-width="1.8"/>
    ${markers}${spotLine}
    <text x="${padL}" y="${padT + 10}">${fmt(ymax)}</text>
    <text x="${padL}" y="${H - padB - 4}">${fmt(ymin)}</text>
    <text x="${padL}" y="${H - 6}">${fmt(xmin)}</text>
    <text x="${W - padR - 60}" y="${H - 6}">${fmt(xmax)}</text>
  </svg>`;
}

/* ----------------------------------------------------------------- chain */
let chainExpiries = [];
async function loadChain() {
  const u = $("#chain-underlying").value;
  const idx = Math.max(0, $("#chain-expiry").selectedIndex);
  let data;
  try { data = await api(`/api/chain?underlying=${u}&expiry_index=${idx}&around_atm=14`); }
  catch (e) { $("#chain-table").innerHTML = `<tr><td>${e.message}</td></tr>`; return; }
  chainExpiries = data.expiries;
  $("#chain-expiry").innerHTML = chainExpiries.map((e, i) =>
    `<option ${i === idx ? "selected" : ""}>${i}: ${e}</option>`).join("");
  const ivr = data.iv_context.iv_rank;
  $("#chain-head").innerHTML = `
    <div class="stat"><div class="k">${data.underlying}</div><div class="v">${fmt(data.spot, 2)}</div></div>
    <div class="stat"><div class="k">expiry</div><div class="v">${data.expiry}</div>
      <div class="muted">${data.dte} DTE · lot ${data.lot_size}</div></div>
    <div class="stat"><div class="k">ATM IV</div><div class="v">${fmt(data.atm_iv * 100, 2)}%</div></div>
    <div class="stat" style="min-width:170px"><div class="k">IV rank / percentile</div>
      <div class="v">${fmt(ivr, 0)} / ${fmt(data.iv_context.iv_percentile, 0)}</div>
      <div class="bar"><i style="width:${ivr}%"></i></div></div>
    <div class="stat"><div class="k">expected move</div><div class="v">±${fmt(data.expected_move_points, 0)} pts</div></div>
    <div class="stat"><div class="k">trend</div><div class="v">${data.trend.direction}</div>
      <div class="muted">RSI ${fmt(data.trend.rsi, 0)} · ADX ${fmt(data.trend.adx, 0)}</div></div>
    <div class="stat"><div class="k">source</div><div class="v">${data.source}</div>
      <div class="muted">${data.fetched_at}</div></div>`;
  $("#chain-table").innerHTML = `
    <thead><tr>
      <th>CALL OI</th><th>Δ</th><th>IV%</th><th>CE LTP</th><th>spread%</th>
      <th>STRIKE</th>
      <th>PE LTP</th><th>spread%</th><th>IV%</th><th>Δ</th><th>PUT OI</th>
    </tr></thead>
    <tbody>${data.rows.map(r => `
      <tr class="${Math.abs(r.strike - data.spot) < (data.rows[1] ? (data.rows[1].strike - data.rows[0].strike) / 2 : 1) ? "atm" : ""}">
        <td>${fmt(r.ce?.oi)}</td><td>${r.ce ? r.ce.delta.toFixed(2) : "—"}</td>
        <td>${r.ce ? fmt(r.ce.iv, 1) : "—"}</td>
        <td class="ce">${r.ce ? fmt(r.ce.ltp, 2) : "—"}</td>
        <td>${r.ce ? fmt(r.ce.spread_pct, 1) : "—"}</td>
        <td><b>${fmt(r.strike)}</b></td>
        <td class="pe">${r.pe ? fmt(r.pe.ltp, 2) : "—"}</td>
        <td>${r.pe ? fmt(r.pe.spread_pct, 1) : "—"}</td>
        <td>${r.pe ? fmt(r.pe.iv, 1) : "—"}</td>
        <td>${r.pe ? r.pe.delta.toFixed(2) : "—"}</td>
        <td>${fmt(r.pe?.oi)}</td>
      </tr>`).join("")}</tbody>`;
}
$("#chain-underlying").addEventListener("change", loadChain);
$("#chain-expiry").addEventListener("change", loadChain);

/* ---------------------------------------------------------------- stocks */
let stockRows = [];
async function loadStocks() {
  try { stockRows = await api("/api/universe"); }
  catch (e) { $("#stocks-table").innerHTML = `<tr><td>${e.message}</td></tr>`; return; }
  renderStocks();
}
function renderStocks() {
  const q = $("#stock-search").value.trim().toUpperCase();
  const v = $("#stock-verdict").value;
  const rows = stockRows.filter(r => (!q || r.underlying.includes(q)) && (!v || r.verdict === v));
  $("#stocks-table").innerHTML = `
    <thead><tr><th>Symbol</th><th>Spot</th><th>Expiry</th><th>DTE</th><th>Lot</th><th>ATM IV%</th>
      <th>IV rank</th><th>IV pct</th><th>Trend</th><th>RSI</th><th>ADX</th><th>Mom%</th>
      <th>Exp move%</th><th>Verdict</th></tr></thead>
    <tbody>${rows.map(r => `
      <tr><td><b>${r.underlying}</b></td><td>${fmt(r.spot, 2)}</td><td>${r.expiry || "—"}</td>
        <td>${r.dte ?? "—"}</td><td>${fmt(r.lot_size)}</td><td>${fmt(r.atm_iv, 1)}</td>
        <td>${fmt(r.iv_rank, 0)}</td><td>${fmt(r.iv_percentile, 0)}</td>
        <td style="color:${r.trend === "bullish" ? "var(--green)" : r.trend === "bearish" ? "var(--red)" : "var(--muted)"}">${r.trend || "—"}</td>
        <td>${fmt(r.rsi, 0)}</td><td>${fmt(r.adx, 0)}</td><td>${fmt(r.momentum_pct, 1)}</td>
        <td>${fmt(r.expected_move_pct, 1)}</td>
        <td>${verdictBadge(r.verdict)}</td></tr>`).join("")}</tbody>`;
}
function verdictBadge(v) {
  const map = { buy: ["var(--green)", "buy premium"], spread: ["var(--amber)", "debit spread"],
                avoid: ["var(--muted)", "no trade"], wait: ["var(--muted)", "wait"] };
  const [c, t] = map[v] || ["var(--muted)", v || "—"];
  return `<span style="color:${c}">${t}</span>`;
}
$("#stock-search").addEventListener("input", renderStocks);
$("#stock-verdict").addEventListener("change", renderStocks);

/* -------------------------------------------------------------- lab */
const PRESETS = {
  iron_condor: s => [
    { side: "SELL", type: "PE", strike: s - 350, premium: 0 },
    { side: "BUY", type: "PE", strike: s - 600, premium: 0 },
    { side: "SELL", type: "CE", strike: s + 350, premium: 0 },
    { side: "BUY", type: "CE", strike: s + 600, premium: 0 }],
  bull_put: s => [{ side: "SELL", type: "PE", strike: s - 300, premium: 0 },
                  { side: "BUY", type: "PE", strike: s - 550, premium: 0 }],
  bear_call: s => [{ side: "SELL", type: "CE", strike: s + 300, premium: 0 },
                   { side: "BUY", type: "CE", strike: s + 550, premium: 0 }],
  call_debit: s => [{ side: "BUY", type: "CE", strike: s - 100, premium: 0 },
                    { side: "SELL", type: "CE", strike: s + 250, premium: 0 }],
  long_call: s => [{ side: "BUY", type: "CE", strike: s - 100, premium: 0 }],
};
function labRow(leg = {}) {
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td><select class="l-side"><option ${leg.side === "BUY" ? "selected" : ""}>SELL</option>
        <option ${leg.side === "BUY" ? "" : ""}>BUY</option></select></td>
    <td><select class="l-type"><option ${leg.type === "PE" ? "" : "selected"}>CE</option>
        <option ${leg.type === "PE" ? "selected" : ""}>PE</option></select></td>
    <td><input class="l-strike" type="number" step="5" value="${leg.strike ?? 0}" /></td>
    <td><input class="l-prem" type="number" step="0.05" value="${leg.premium ?? 0}" placeholder="auto" /></td>
    <td><button class="btn small l-del">✕</button></td>`;
  if (leg.side === "BUY") tr.querySelector(".l-side").value = "BUY";
  tr.querySelector(".l-del").addEventListener("click", () => tr.remove());
  return tr;
}
$("#lab-add-leg").addEventListener("click", () => $("#lab-legs tbody").appendChild(labRow()));
$$(".presets .btn").forEach(b => b.addEventListener("click", () => {
  const s = Number($("#lab-spot").value) || 25000;
  const body = $("#lab-legs tbody");
  body.innerHTML = "";
  PRESETS[b.dataset.preset](s).forEach(l => body.appendChild(labRow(l)));
  calcLab();
}));
$("#lab-calc").addEventListener("click", calcLab);

async function calcLab() {
  const legs = $$("#lab-legs tbody tr").map(tr => ({
    side: tr.querySelector(".l-side").value,
    option_type: tr.querySelector(".l-type").value,
    strike: Number(tr.querySelector(".l-strike").value),
    premium: Number(tr.querySelector(".l-prem").value) || 0,
  })).filter(l => l.strike > 0);
  if (!legs.length) return;
  const payload = {
    spot: Number($("#lab-spot").value), dte: Number($("#lab-dte").value),
    iv: Number($("#lab-iv").value) / 100, lot_size: Number($("#lab-lot").value),
    lots: Number($("#lab-lots").value), legs,
  };
  try {
    const r = await api("/api/payoff", { method: "POST", body: JSON.stringify(payload) });
    const mp = r.max_profit === Infinity ? "unlimited" : fmt(r.max_profit);
    const ml = r.max_loss === Infinity ? "unlimited" : fmt(r.max_loss);
    $("#lab-summary").innerHTML = `
      <div class="metric"><div class="k">net ${r.net_credit_per_unit >= 0 ? "credit" : "cost"}</div>
        <div class="v">${fmt(Math.abs(r.net_credit_per_unit), 2)}/unit</div></div>
      <div class="metric"><div class="k">max profit</div><div class="v" style="color:var(--green)">${mp}</div></div>
      <div class="metric"><div class="k">max loss</div><div class="v" style="color:var(--red)">${ml}</div></div>
      <div class="metric"><div class="k">breakevens</div><div class="v">${(r.breakevens || []).map(b => fmt(b, 1)).join(" / ") || "—"}</div></div>
      <div class="metric"><div class="k">net delta</div><div class="v">${r.greeks.delta.toFixed(1)}</div></div>
      <div class="metric"><div class="k">net theta</div><div class="v">${r.greeks.theta.toFixed(0)}</div></div>
      <div class="metric"><div class="k">net vega</div><div class="v">${r.greeks.vega.toFixed(0)}</div></div>
      <div class="metric"><div class="k">margin est.</div><div class="v">${fmt(r.margin_estimate)}</div></div>`;
    $("#lab-chart").innerHTML = payoffSVG(
      { payoff: { prices: r.prices, pnl: r.pnl }, breakevens: r.breakevens, spot: payload.spot });
  } catch (e) { $("#lab-summary").textContent = e.message; }
}

/* ---------------------------------------------------------- rules/settings */
async function loadRules() {
  try {
    const r = await api("/api/rules");
    $("#rules-box").innerHTML = Object.entries(r).map(([k, items]) => `
      <div class="rules-block"><h4>${k.replace(/_/g, " ")}</h4>
        <ul>${items.map(i => `<li>${i}</li>`).join("")}</ul></div>`).join("");
  } catch (e) { $("#rules-box").textContent = e.message; }
}
const EDITABLE = {
  nifty_spreads: ["min_iv_rank", "short_delta", "wing_delta", "min_pop", "min_credit_pct_of_width",
                  "dte_min", "dte_max", "max_spread_pct", "min_oi", "take_profit_pct",
                  "stop_loss_multiple", "roll_dte"],
  stock_options: ["max_iv_rank", "delta_min", "delta_max", "adx_min", "momentum_min_pct",
                  "max_premium_pct_of_spot", "max_spread_pct", "min_oi", "take_profit_pct",
                  "stop_loss_pct", "expiry_index"],
  account: ["capital", "max_risk_per_trade_pct", "max_lots_per_trade", "max_open_trades"],
  alerts: ["min_score", "dedupe_minutes", "max_alerts_per_scan"],
  scheduler: ["interval_seconds"],
};
let cfgCache = {};
async function loadSettings() {
  cfgCache = await api("/api/config");
  $("#settings-box").innerHTML = Object.entries(EDITABLE).map(([section, keys]) =>
    keys.map(k => {
      const v = cfgCache[section]?.[k];
      return `<label>${section}.${k}<input data-s="${section}" data-k="${k}" value="${v}" /></label>`;
    }).join("")).join("");
  const h = await api("/api/health");
  $("#store-stats").textContent = JSON.stringify(h.store, null, 2);
}
$("#save-settings").addEventListener("click", async () => {
  const patch = {};
  $$("#settings-box input").forEach(i => {
    patch[i.dataset.s] = patch[i.dataset.s] || {};
    const raw = i.value;
    patch[i.dataset.s][i.dataset.k] = Number.isNaN(Number(raw)) ? raw : Number(raw);
  });
  try {
    await api("/api/config", { method: "POST", body: JSON.stringify(patch) });
    $("#settings-msg").textContent = "Saved. The next scan uses these values.";
  } catch (e) { $("#settings-msg").textContent = e.message; }
});
$("#test-alert").addEventListener("click", async () => {
  try {
    const r = await api("/api/alerts/test", { method: "POST" });
    $("#settings-msg").textContent = r.sent ? "Test alert sent ✅" : r.detail;
  } catch (e) { $("#settings-msg").textContent = e.message; }
});

/* ------------------------------------------------------------------ boot */
(async function boot() {
  $("#lab-legs tbody").appendChild(labRow({ side: "SELL", type: "PE", strike: 25130 }));
  $("#lab-legs tbody").appendChild(labRow({ side: "BUY", type: "PE", strike: 24880 }));
  await refreshHealth();
  await loadSignals();
  setInterval(refreshHealth, 30000);
  setInterval(() => { if ($("#tab-signals").classList.contains("active")) loadSignals(); }, 60000);
})();
