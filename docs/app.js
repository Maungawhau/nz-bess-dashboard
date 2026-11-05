// NZ BESS dashboard - reads the JSON written nightly by pipeline/build.py and aggregates in the browser.
const $ = (s) => document.querySelector(s);
const css = (v) => getComputedStyle($(".viz-root")).getPropertyValue(v).trim();
const SLOTS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];
const state = { gran: "month", split: "bess", tight: "all", sel: new Set(), from: null, to: null };
let D = {};

const fmt$ = (v) => (Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(2)}m` : Math.abs(v) >= 1e3 ? `$${(v / 1e3).toFixed(0)}k` : `$${v.toFixed(0)}`);
const fmtN = (v, d = 0) => (v == null || !isFinite(v) ? "–" : v.toLocaleString("en-NZ", { maximumFractionDigits: d, minimumFractionDigits: d }));
const fmtMWh = (v) => (Math.abs(v) >= 1000 ? `${fmtN(v / 1000, 1)} GWh` : `${fmtN(v)} MWh`);

function periodKey(dateStr, g) {
  const d = new Date(dateStr + "T00:00:00Z");
  if (g === "day") return dateStr;
  if (g === "month") return dateStr.slice(0, 7);
  if (g === "year") return dateStr.slice(0, 4);
  const day = (d.getUTCDay() + 6) % 7; // Monday-based week, keyed by its Monday
  d.setUTCDate(d.getUTCDate() - day);
  return d.toISOString().slice(0, 10);
}
const periodLabel = (k) => (state.gran === "week" ? `wk of ${k}` : k);

function colorOf(id) {
  const i = D.bess.findIndex((b) => b.id === id);
  return css(SLOTS[i % SLOTS.length]);
}

function layout(extra = {}) {
  const t = css("--text-secondary"), g = css("--grid");
  return Object.assign({
    margin: { l: 64, r: 16, t: 10, b: 44 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: "system-ui, sans-serif", size: 12, color: t },
    xaxis: { gridcolor: g, linecolor: g, tickcolor: g, zeroline: false },
    yaxis: { gridcolor: g, linecolor: g, zerolinecolor: css("--neutral"), tickcolor: g },
    legend: { orientation: "h", y: -0.18, x: 0 }, hovermode: "x unified", bargap: 0.25,
    hoverlabel: { bgcolor: css("--surface-1"), bordercolor: css("--border"), font: { color: css("--text-primary") } },
  }, extra);
}
const cfg = { displayModeBar: false, responsive: true };
const pAxis = () => ({ ...layout().xaxis, type: state.gran === "day" ? "date" : "category" });

function filtered() {
  return D.daily.filter((r) => state.sel.has(r.bess) && r.date >= state.from && r.date <= state.to);
}

function aggregate(rows, keyFn) {
  const m = new Map();
  for (const r of rows) {
    const k = keyFn(r);
    const a = m.get(k) || {};
    for (const [f, v] of Object.entries(r)) if (typeof v === "number") a[f] = (a[f] || 0) + v;
    a._n = (a._n || 0) + 1;
    m.set(k, a);
  }
  return m;
}

function tiles(rows) {
  const s = aggregate(rows, () => "all").get("all") || {};
  const dp = s.discharge_mwh ? s.discharge_rev / s.discharge_mwh : NaN;
  const cp = s.charge_mwh ? s.charge_cost / s.charge_mwh : NaN;
  const disp = D.disp.filter((r) => r.date >= state.from && r.date <= state.to);
  const th = disp.reduce((a, r) => a + r.thermal_mwh, 0), bm = disp.reduce((a, r) => a + r.bess_mwh, 0);
  const T = [
    ["Total revenue", fmt$(s.total_rev || 0), `${state.sel.size} batter${state.sel.size === 1 ? "y" : "ies"}`],
    ["Arbitrage", fmt$(s.arbitrage_rev || 0), s.total_rev ? `${fmtN((100 * s.arbitrage_rev) / s.total_rev)}% of total` : ""],
    ["Reserves (FIR + SIR)", fmt$(s.reserve_rev || 0), `FIR ${fmt$(s.fir_rev || 0)} · SIR ${fmt$(s.sir_rev || 0)}`],
    ["Discharged", fmtMWh(s.discharge_mwh || 0), `charged ${fmtMWh(s.charge_mwh || 0)}`],
    ["Average spread", isFinite(dp - cp) ? `$${fmtN(dp - cp)}/MWh` : "–", `sell $${fmtN(dp)} · buy $${fmtN(cp)}`],
    ["Thermal displaced", fmtMWh(th), bm ? `${fmtN((100 * th) / bm)}% of all BESS discharge` : "indicative"],
  ];
  $("#tiles").innerHTML = T.map(([k, v, d]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("");
}

function revChart(rows) {
  const keys = [...new Set(rows.map((r) => periodKey(r.date, state.gran)))].sort();
  let traces;
  if (state.split === "bess") {
    traces = D.bess.filter((b) => state.sel.has(b.id)).map((b) => {
      const m = aggregate(rows.filter((r) => r.bess === b.id), (r) => periodKey(r.date, state.gran));
      return { type: "bar", name: b.name, x: keys.map(periodLabel), y: keys.map((k) => m.get(k)?.total_rev || 0),
        marker: { color: colorOf(b.id), line: { color: css("--surface-1"), width: 1 } },
        hovertemplate: "%{y:$,.0f}<extra>" + b.name + "</extra>" };
    });
  } else {
    const m = aggregate(rows, (r) => periodKey(r.date, state.gran));
    traces = [["arbitrage_rev", "Arbitrage", "--s1"], ["fir_rev", "FIR reserve", "--s2"], ["sir_rev", "SIR reserve", "--s3"]].map(([f, n, c]) => ({
      type: "bar", name: n, x: keys.map(periodLabel), y: keys.map((k) => m.get(k)?.[f] || 0),
      marker: { color: css(c), line: { color: css("--surface-1"), width: 1 } }, hovertemplate: "%{y:$,.0f}<extra>" + n + "</extra>" }));
  }
  Plotly.react("revChart", traces, layout({ barmode: "relative", xaxis: pAxis(), yaxis: { ...layout().yaxis, tickprefix: "$", tickformat: "~s", title: { text: "NZD" } } }), cfg);
}

function summaryTable(rows) {
  const m = aggregate(rows, (r) => r.bess);
  const days = Math.max(1, (new Date(state.to) - new Date(state.from)) / 864e5 + 1);
  const H = ["Battery", "MW / MWh", "Discharge MWh", "Charge MWh", "RTE", "Cycles/day", "Sell $/MWh", "Buy $/MWh", "Spread", "Arbitrage", "FIR", "SIR", "Total", "$/MW-yr (ann.)"];
  let html = `<thead><tr>${H.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>`;
  for (const b of D.bess.filter((b) => state.sel.has(b.id))) {
    const s = m.get(b.id);
    if (!s) { html += `<tr><td>${b.name}</td><td colspan="${H.length - 1}" style="text-align:left" class="muted">no data in range</td></tr>`; continue; }
    const dp = s.discharge_mwh ? s.discharge_rev / s.discharge_mwh : NaN, cp = s.charge_mwh ? s.charge_cost / s.charge_mwh : NaN;
    const active = Math.min(days, s._n);
    const flag = b.auto ? '<span class="badge">auto-detected</span>' : !b.load_unit ? '<span class="badge">no charging data</span>' : "";
    html += `<tr><td><span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:${colorOf(b.id)};margin-right:6px"></span>${b.name}${flag}</td>
      <td>${fmtN(b.power_mw)} / ${fmtN(b.energy_mwh)}</td><td>${fmtN(s.discharge_mwh)}</td><td>${fmtN(s.charge_mwh)}</td>
      <td>${s.charge_mwh ? fmtN((100 * s.discharge_mwh) / s.charge_mwh) + "%" : "–"}</td>
      <td>${b.energy_mwh ? fmtN(s.discharge_mwh / b.energy_mwh / active, 2) : "–"}</td>
      <td>${fmtN(dp)}</td><td>${fmtN(cp)}</td><td>${fmtN(dp - cp)}</td>
      <td>${fmt$(s.arbitrage_rev)}</td><td>${fmt$(s.fir_rev)}</td><td>${fmt$(s.sir_rev)}</td><td><b>${fmt$(s.total_rev)}</b></td>
      <td>${b.power_mw ? fmt$((s.total_rev / b.power_mw) * (365 / active)) : "–"}</td></tr>`;
  }
  $("#sumTable").innerHTML = html + "</tbody>";
}

function detail() {
  const id = $("#detailBess").value, b = D.bess.find((x) => x.id === id);
  if (!b) return;
  const rows = D.daily.filter((r) => r.bess === id && r.date >= state.from && r.date <= state.to);
  const keys = [...new Set(rows.map((r) => periodKey(r.date, state.gran)))].sort();
  const m = aggregate(rows, (r) => periodKey(r.date, state.gran));
  const x = keys.map(periodLabel), c = colorOf(id);
  Plotly.react("volChart", [
    { type: "bar", name: "Discharge", x, y: keys.map((k) => m.get(k).discharge_mwh), marker: { color: c }, hovertemplate: "%{y:,.0f} MWh<extra>Discharge</extra>" },
    { type: "bar", name: "Charge", x, y: keys.map((k) => -m.get(k).charge_mwh), marker: { color: c, opacity: 0.45 }, hovertemplate: "%{y:,.0f} MWh<extra>Charge</extra>" },
  ], layout({ barmode: "relative", xaxis: pAxis(), yaxis: { ...layout().yaxis, title: { text: "MWh" } } }), cfg);
  const pr = (k, a, b2) => (m.get(k)[b2] ? m.get(k)[a] / m.get(k)[b2] : null);
  Plotly.react("priceChart", [
    { type: "scatter", mode: "lines+markers", name: "Sell (discharge)", x, y: keys.map((k) => pr(k, "discharge_rev", "discharge_mwh")), line: { color: css("--s1"), width: 2 }, marker: { size: 8 } },
    { type: "scatter", mode: "lines+markers", name: "Buy (charge)", x, y: keys.map((k) => pr(k, "charge_cost", "charge_mwh")), line: { color: css("--s2"), width: 2 }, marker: { size: 8 } },
    { type: "scatter", mode: "lines", name: "Node average", x, y: keys.map((k) => m.get(k).node_price_avg / m.get(k)._n), line: { color: css("--neutral"), width: 2, dash: "dot" } },
  ], layout({ xaxis: pAxis(), yaxis: { ...layout().yaxis, tickprefix: "$", title: { text: "$/MWh" } }, hovermode: "x unified" }), cfg);

  const month = $("#profMonth").value;
  const p = D.profile.filter((r) => r.bess === id && r.month === month).sort((a, b2) => a.tp - b2.tp);
  const tpx = p.map((r) => r.tp);
  const tpLabel = (tp) => { const mins = (tp - 1) * 30; return `${String(Math.floor(mins / 60)).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}`; };
  const tl = tpx.map(tpLabel);
  Plotly.react("profChart", [
    { type: "bar", name: "Discharge", x: tl, y: p.map((r) => r.discharge_mw), marker: { color: c } },
    { type: "bar", name: "Charge", x: tl, y: p.map((r) => -r.charge_mw), marker: { color: c, opacity: 0.45 } },
    { type: "scatter", mode: "lines", name: "FIR cleared", x: tl, y: p.map((r) => r.fir_mw), line: { color: css("--s2"), width: 2 } },
    { type: "scatter", mode: "lines", name: "SIR cleared", x: tl, y: p.map((r) => r.sir_mw), line: { color: css("--s3"), width: 2 } },
  ], layout({ barmode: "relative", yaxis: { ...layout().yaxis, title: { text: "MW" } }, xaxis: { ...layout().xaxis, nticks: 12 } }), cfg);
  Plotly.react("profPriceChart", [
    { type: "scatter", mode: "lines", name: "Node price", x: tl, y: p.map((r) => r.price), line: { color: css("--s1"), width: 2 }, fill: "tozeroy", fillcolor: "rgba(42,120,214,0.08)" },
  ], layout({ showlegend: false, yaxis: { ...layout().yaxis, tickprefix: "$", title: { text: "$/MWh" } }, xaxis: { ...layout().xaxis, nticks: 12 } }), cfg);
}

function displacement() {
  let rows = D.disp.filter((r) => r.date >= state.from && r.date <= state.to);
  if (state.tight === "tight") rows = rows.filter((r) => r.tight);
  const keys = [...new Set(rows.map((r) => periodKey(r.date, state.gran)))].sort();
  const m = aggregate(rows, (r) => periodKey(r.date, state.gran));
  const F = [["disp_gas_mwh", "Gas", "--s2"], ["disp_coal/gas_mwh", "Huntly coal/gas", "--s8"], ["disp_diesel_mwh", "Diesel", "--s4"],
    ["disp_hydro_mwh", "Hydro", "--s1"], ["disp_geothermal_mwh", "Geothermal", "--s3"], ["disp_other_mwh", "Other / wind / solar", "--neutral"]];
  const traces = F.map(([f, n, c]) => ({ type: "bar", name: n, x: keys.map(periodLabel), y: keys.map((k) => m.get(k)?.[f] || 0),
    marker: { color: css(c), line: { color: css("--surface-1"), width: 1 } }, hovertemplate: "%{y:,.0f} MWh<extra>" + n + "</extra>" }));
  traces.push({ type: "bar", name: "Shortfall (stack exhausted)", x: keys.map(periodLabel),
    y: keys.map((k) => { const a = m.get(k); if (!a) return 0; const s = F.reduce((t, [f]) => t + (a[f] || 0), 0) + (a["disp_wind_mwh"] || 0) + (a["disp_solar_mwh"] || 0); return Math.max(a.bess_mwh - s, 0); }),
    marker: { color: css("--surface-1"), line: { color: css("--text-muted"), width: 1 }, pattern: { shape: "/", fgcolor: css("--text-muted") } } });
  Plotly.react("dispChart", traces, layout({ barmode: "stack", xaxis: pAxis(), yaxis: { ...layout().yaxis, title: { text: "MWh" } } }), cfg);

  const t = D.tight.filter((r) => r.date >= state.from && r.date <= state.to).slice(0, 40);
  const H = ["Date", "TP", "Island", "BESS MW", "Ref price", "Headroom MW", "Gas MW", "Coal/gas MW", "Shortfall MW", "Next units in stack", "Counterfactual $/MWh"];
  $("#tightTable").innerHTML = `<thead><tr>${H.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>` + t.map((r) =>
    `<tr><td>${r.date}</td><td>${r.tp}</td><td>${r.island}</td><td>${fmtN(r.bess_mw)}</td><td>$${fmtN(r.ref_price)}</td><td>${fmtN(r.headroom_mw)}</td>
     <td>${fmtN(r.disp_gas_mw)}</td><td>${fmtN(r["disp_coal/gas_mw"])}</td><td>${fmtN(r.unmet_mw)}</td><td style="text-align:left">${r.displaced_units || ""}</td>
     <td>${r.unmet_mw > 1 ? "scarcity" : "$" + fmtN(r.cf_price)}</td></tr>`).join("") + "</tbody>";
}

function render() {
  const rows = filtered();
  tiles(rows); revChart(rows); summaryTable(rows); detail(); displacement();
}

function initControls() {
  const dates = D.daily.map((r) => r.date).sort();
  const first = dates[0], last = dates[dates.length - 1];
  state.from = first; state.to = last;
  $("#from").value = first; $("#to").value = last;
  $("#from").min = $("#to").min = first; $("#from").max = $("#to").max = last;
  $("#coverage").textContent = `Trading dates ${first} to ${last} · updated ${D.meta.generated_utc.replace("T", " ")} UTC`;

  const withData = new Set(D.daily.filter((r) => r.discharge_mwh > 0 || r.reserve_rev > 0).map((r) => r.bess));
  D.bess.forEach((b) => withData.has(b.id) && state.sel.add(b.id));
  $("#bessChips").innerHTML = D.bess.map((b) => `<label class="chip"><input type="checkbox" value="${b.id}" ${state.sel.has(b.id) ? "checked" : ""}>
     <span class="sw" style="background:${colorOf(b.id)}"></span>${b.name}</label>`).join("");
  $("#bessChips").addEventListener("change", (e) => { e.target.checked ? state.sel.add(e.target.value) : state.sel.delete(e.target.value); render(); });

  $("#detailBess").innerHTML = D.bess.filter((b) => withData.has(b.id)).map((b) => `<option value="${b.id}">${b.name}</option>`).join("");
  const months = [...new Set(D.profile.map((r) => r.month))].sort();
  $("#profMonth").innerHTML = months.map((m) => `<option>${m}</option>`).join("");
  $("#profMonth").value = months[months.length - 1];
  $("#detailBess").onchange = $("#profMonth").onchange = detail;

  const seg = (sel, attr, key) => $(sel).addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    $(sel).querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
    state[key] = b.dataset[attr]; render();
  });
  seg("#gran", "g", "gran"); seg("#revSplit", "s", "split"); seg("#tightSel", "t", "tight");
  $("#from").onchange = (e) => { state.from = e.target.value; render(); };
  $("#to").onchange = (e) => { state.to = e.target.value; render(); };

  const t = D.meta.settings.tight_market;
  $("#tPrice").textContent = t.price_threshold; $("#tHead").textContent = t.headroom_threshold_mw; $("#tCap").textContent = t.headroom_price_cap;
  $("#nWarn").textContent = D.meta.warnings.length;
  $("#warnList").innerHTML = D.meta.warnings.map((w) => `<li>${w}</li>`).join("");
}

(async () => {
  const get = (f) => fetch(`data/${f}?v=${Date.now()}`).then((r) => r.json());
  [D.bess, D.daily, D.profile, D.disp, D.tight, D.meta] = await Promise.all(
    ["bess.json", "daily.json", "profile.json", "displacement_daily.json", "tight_periods.json", "meta.json"].map(get));
  if (!D.daily.length) { $("#coverage").textContent = "No data yet - the first nightly run hasn't completed."; return; }
  initControls(); render();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
})();
