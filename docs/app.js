// NZ BESS revenue dashboard - reads JSON written nightly by pipeline/build.py.
// Colour rules: --b1..--b8 identify batteries (only batteries); --arb/--res/--fir/--sir are neutral
// revenue-stream colours; the calendar uses a blue/red diverging scale for magnitude.
const $ = (s) => document.querySelector(s);
const css = (v) => getComputedStyle($(".viz-root")).getPropertyValue(v).trim();
const D = {};
const S = { tab: "overview", from: null, to: null, sel: new Set(), gran: "month", split: "bess", hour: "arb", bess: null, dayMode: "day" };

// ---------- formatting ----------
const fmtN = (v, d = 0) => (v == null || !isFinite(v) ? "–" : v.toLocaleString("en-NZ", { maximumFractionDigits: d, minimumFractionDigits: d }));
const fmt$ = (v) => {
  if (v == null || !isFinite(v)) return "–";
  const a = Math.abs(v), s = v < 0 ? "−" : "";
  return s + (a >= 1e6 ? `$${(a / 1e6).toFixed(2)}m` : a >= 1e4 ? `$${(a / 1e3).toFixed(0)}k` : `$${fmtN(a)}`);
};
const fmtMWh = (v) => (Math.abs(v) >= 1000 ? `${fmtN(v / 1000, 1)} GWh` : `${fmtN(v)} MWh`);
const tint = (hex, a) => { const h = hex.replace("#", ""); const n = parseInt(h.length === 3 ? h.replace(/./g, "$&$&") : h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`; };
const addDays = (d, n) => { const x = new Date(d + "T00:00:00Z"); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); };
const nDays = (a, b) => Math.round((new Date(b) - new Date(a)) / 864e5) + 1;
const weekOf = (d) => { const x = new Date(d + "T00:00:00Z"); x.setUTCDate(x.getUTCDate() - ((x.getUTCDay() + 6) % 7)); return x.toISOString().slice(0, 10); };
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const prettyDate = (d) => `${+d.slice(8)} ${MON[+d.slice(5, 7) - 1]} ${d.slice(0, 4)}`;
const shortDate = (d) => `${+d.slice(8)} ${MON[+d.slice(5, 7) - 1]} ${d.slice(2, 4)}`;
const monthLabel = (m) => `${MON[+m.slice(5, 7) - 1]} ${m.slice(2, 4)}`;

const bess = (id) => D.bess.find((b) => b.id === id);
const colorOf = (id) => css(`--b${(D.bess.findIndex((b) => b.id === id) % 8) + 1}`);
const swatch = (id) => `<i class="sw" style="background:${colorOf(id)}"></i>`;

// ---------- plotly base ----------
function layout(extra = {}) {
  const t = css("--text-secondary"), g = css("--grid");
  const base = {
    margin: { l: 68, r: 16, t: 8, b: 44 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: "system-ui, sans-serif", size: 12, color: t },
    xaxis: { gridcolor: g, linecolor: g, tickcolor: g, zeroline: false },
    yaxis: { gridcolor: g, linecolor: g, zerolinecolor: css("--text-muted"), tickcolor: g },
    legend: { orientation: "h", y: -0.16, x: 0 }, bargap: 0.25,
    hoverlabel: { bgcolor: css("--surface-1"), bordercolor: css("--border"), font: { color: css("--text-primary") } },
  };
  for (const [k, v] of Object.entries(extra)) base[k] = (typeof v === "object" && !Array.isArray(v) && base[k]) ? { ...base[k], ...v } : v;
  return base;
}
const cfg = { displayModeBar: false, responsive: true };
const money = { tickprefix: "$", tickformat: "~s" };

// ---------- data helpers ----------
const inRange = (r) => r.date >= S.from && r.date <= S.to;
const sel = () => D.bess.filter((b) => S.sel.has(b.id));
function sum(rows) {
  const t = {};
  for (const r of rows) for (const [k, v] of Object.entries(r)) if (typeof v === "number") t[k] = (t[k] || 0) + v;
  t.n = rows.length;
  ["discharge_mwh", "charge_mwh", "arbitrage_rev", "reserve_rev", "fir_rev", "sir_rev", "total_rev", "discharge_rev", "charge_cost", "fir_mwh", "sir_mwh"].forEach((k) => (t[k] = t[k] || 0));
  t.sell = t.discharge_mwh ? t.discharge_rev / t.discharge_mwh : NaN;
  t.buy = t.charge_mwh ? t.charge_cost / t.charge_mwh : NaN;
  return t;
}
const perMWyr = (rev, mwDays) => (mwDays ? (rev / mwDays) * 365 : NaN);
const active = (r) => r.date >= (D.start[r.bess] || "9999");          // on or after the battery's first active day
const shortName = (id) => bess(id).name.replace(/ BESS$/, "");

// =============== SYSTEM EVENTS ===============
const EVK = { lrs: ["Low residual", "--ev-lrs"], hvdc: ["HVDC", "--ev-hvdc"], sep: ["Islands split", "--ev-sep"], event: ["Trip / frequency", "--ev-trip"] };
const evKind = (k) => (k === "lrs" ? "lrs" : k === "hvdc" ? "hvdc" : ["freq", "trip", "ufe"].includes(k) ? "event" : null);
const hm = (iso) => iso.slice(11, 16);
function eventsOn(day) {
  const a = `${day}T00:00`, b = `${addDays(day, 1)}T00:00`;
  return (D.ev?.events || []).filter((e) => e.start < b && e.end > a);
}
function dayTags(day, short = true) {
  const out = [], seen = new Set();
  for (const e of eventsOn(day)) {
    const k = evKind(e.kind); if (!k || (e.kind === "ufe" && e.approx)) continue;
    const t = k === "event" ? `${e.kind === "freq" ? "Freq." : "Trip"} ${hm(e.start)}` : k === "lrs" ? `Low residual ${hm(e.start)}–${hm(e.end)}${e.island ? " " + e.island : ""}` : "HVDC";
    if (seen.has(t)) continue; seen.add(t);
    out.push(`<span class="tag ${k}" title="${e.title.replace(/"/g, "")}">${t}</span>`);
  }
  const sp = D.ev?.days?.[day];
  if (sp) out.push(`<span class="tag sep" title="NI–SI gap up to $${fmtN(Math.abs(sp.max_gap))}/MWh">Islands split ×${sp.sep_tps}</span>`);
  return out.join("") || `<span class="muted">–</span>`;
}
function evList(days) {
  const items = [];
  for (const d of days) for (const e of eventsOn(d)) {
    const k = evKind(e.kind); if (!k && e.kind !== "lrs_early") continue;
    const key = e.ref + e.start; if (items.some((x) => x.key === key)) continue;
    const when = e.start.slice(0, 10) === e.end.slice(0, 10) || k === "event"
      ? `${days.length > 1 ? DOW[(new Date(e.start.slice(0, 10)).getUTCDay() + 6) % 7] + " " : ""}${hm(e.start)}${k === "event" ? "" : "–" + hm(e.end)}`
      : `${prettyDate(e.start.slice(0, 10))} ${hm(e.start)} → ${prettyDate(e.end.slice(0, 10))} ${hm(e.end)}`;
    const col = k ? css(EVK[k][1]) : "transparent";
    items.push({ key, html: `<a class="ev" href="${e.url}" target="_blank" rel="noopener" title="Open the Transpower notice"><i class="k" style="background:${col};${k ? "" : "border:1px dashed var(--text-muted)"}"></i>${e.title}${e.island ? " (" + e.island + ")" : ""} · ${when}${e.approx ? " (approx.)" : ""}</a>` });
  }
  for (const d of days) { const sp = D.ev?.days?.[d]; if (sp) items.push({ key: "sep" + d, html: `<span class="ev"><i class="k" style="background:${css("--ev-sep")}"></i>Islands split: ${sp.sep_tps} half-hour${sp.sep_tps > 1 ? "s" : ""}${days.length > 1 ? " on " + DOW[(new Date(d).getUTCDay() + 6) % 7] : ""}, NI–SI gap up to $${fmtN(Math.abs(sp.max_gap))}/MWh</span>` }); }
  $("#dayEvents").innerHTML = items.length ? `<span class="muted" style="align-self:center">What was going on:</span>` + items.map((x) => x.html).join("") : `<span class="muted">No Transpower notices or island price separation flagged.</span>`;
}

function evTable() {
  const rows = (D.ev?.tagrev?.rows || []).filter((r) => r[0] >= S.from && r[0] <= S.to && S.sel.has(r[1]));
  const H = ["Battery", "Total revenue", "During low residual CAN", "During HVDC outage", "While islands split", "In trip / frequency half-hours", "With no flagged event"];
  const cell = (v, tot) => `${fmt$(v)}<span class="muted"> · ${tot > 0 ? fmtN((100 * v) / tot) : "–"}%</span>`;
  $("#evTable").innerHTML = `<thead><tr>${H.map((h, i) => `<th class="${i ? "" : "l"}">${h}</th>`).join("")}</tr></thead><tbody>` + sel().map((b) => {
    const t = { lrs: 0, hvdc: 0, sep: 0, event: 0, none: 0 };
    rows.filter((r) => r[1] === b.id).forEach((r) => (t[r[2]] += r[3] + r[4]));
    const tot = sum(D.daily.filter((r) => r.bess === b.id && inRange(r))).total_rev;
    return `<tr><td>${swatch(b.id)} ${b.name}</td><td><b>${fmt$(tot)}</b></td><td>${cell(t.lrs, tot)}</td><td>${cell(t.hvdc, tot)}</td><td>${cell(t.sep, tot)}</td><td>${cell(t.event, tot)}</td><td>${cell(t.none, tot)}</td></tr>`;
  }).join("") + "</tbody>";
}

// =============== OVERVIEW ===============
function overview() {
  const rows = D.daily.filter((r) => S.sel.has(r.bess) && inRange(r));
  const t = sum(rows);
  const mwDays = rows.filter(active).reduce((a, r) => a + (bess(r.bess).power_mw || 0), 0);
  const tiles = [
    ["Total revenue", fmt$(t.total_rev), `${prettyDate(S.from)} – ${prettyDate(S.to)}`],
    ["From arbitrage", fmt$(t.arbitrage_rev), t.total_rev ? `${fmtN((100 * t.arbitrage_rev) / t.total_rev)}% of total` : ""],
    ["From reserves", fmt$(t.reserve_rev), `FIR ${fmt$(t.fir_rev)} · SIR ${fmt$(t.sir_rev)}`],
    ["Revenue per MW", isFinite(perMWyr(t.total_rev, mwDays)) ? `${fmt$(perMWyr(t.total_rev, mwDays))}/yr` : "–", "annualised, all selected batteries"],
    ["Energy discharged", fmtMWh(t.discharge_mwh), `sold at $${fmtN(t.sell)}/MWh on average`],
  ];
  $("#tiles").innerHTML = tiles.map(([k, v, d]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("");
  cumChart(rows); perChart(rows); calChart(rows); hourChart(); topTables(rows); evTable(); cmpTable(rows);
}

function cumChart(rows) {
  const dates = []; for (let d = S.from; d <= S.to; d = addDays(d, 1)) dates.push(d);
  const traces = sel().map((b) => {
    const by = Object.fromEntries(rows.filter((r) => r.bess === b.id).map((r) => [r.date, r.total_rev]));
    let c = 0; const y = dates.map((d) => (c += by[d] || 0));
    return { type: "scatter", mode: "lines", name: b.name, x: dates, y, line: { color: colorOf(b.id), width: 2.5 },
      hovertemplate: `%{x|%d %b}: %{y:$,.0f}<extra>${b.name}</extra>` };
  });
  Plotly.react("cumChart", traces, layout({ hovermode: "x unified", yaxis: { ...money, title: { text: "cumulative revenue" } }, xaxis: { type: "date" } }), cfg);
}

function perChart(rows) {
  const key = (d) => (S.gran === "month" ? d.slice(0, 7) : weekOf(d));
  const keys = [...new Set(rows.map((r) => key(r.date)))].sort();
  const lab = keys.map((k) => (S.gran === "month" ? monthLabel(k) : `${+k.slice(8)} ${MON[+k.slice(5, 7) - 1]}`));
  $("#perTitle").textContent = S.gran === "month" ? "Revenue each month" : "Revenue each week";
  $("#perSub").textContent = S.split === "bess" ? "Stacked by battery" : "All selected batteries, split into arbitrage and reserves";
  const agg = (filter, f) => { const m = {}; rows.filter(filter).forEach((r) => { const k = key(r.date); m[k] = (m[k] || 0) + r[f]; }); return keys.map((k) => m[k] || 0); };
  const traces = S.split === "bess"
    ? sel().map((b) => ({ type: "bar", name: b.name, x: lab, y: agg((r) => r.bess === b.id, "total_rev"),
        marker: { color: colorOf(b.id), line: { color: css("--surface-1"), width: 1 } }, hovertemplate: `%{y:$,.0f}<extra>${b.name}</extra>` }))
    : [["arbitrage_rev", "Arbitrage", "--arb"], ["reserve_rev", "Reserves", "--res"]].map(([f, n, c]) => ({ type: "bar", name: n, x: lab, y: agg(() => true, f),
        marker: { color: css(c), line: { color: css("--surface-1"), width: 1 } }, hovertemplate: `%{y:$,.0f}<extra>${n}</extra>` }));
  Plotly.react("perChart", traces, layout({ barmode: "relative", hovermode: "x unified", xaxis: { type: "category" }, yaxis: { ...money, title: { text: "revenue" } } }), cfg);
}

function calChart(rows) {
  const dates = []; for (let d = S.from; d <= S.to; d = addDays(d, 1)) dates.push(d);
  const bs = sel().slice().reverse();
  const by = {}; rows.forEach((r) => (by[r.bess + r.date] = r));
  const z = bs.map((b) => dates.map((d) => (by[b.id + d] ? by[b.id + d].total_rev : null)));
  const vals = z.flat().filter((v) => v != null).map(Math.abs).sort((a, b) => a - b);
  const cap = vals.length ? vals[Math.floor(vals.length * 0.97)] || 1 : 1;
  const text = bs.map((b) => dates.map((d) => { const r = by[b.id + d]; return r
    ? `<b>${b.name}</b> · ${prettyDate(d)}<br>Total ${fmt$(r.total_rev)}<br>Arbitrage ${fmt$(r.arbitrage_rev)} · Reserves ${fmt$(r.reserve_rev)}<br>Peak price $${fmtN(r.node_price_max)}/MWh`
    : `${b.name} · ${prettyDate(d)}<br>no data`; }));
  Plotly.react("calChart", [{
    type: "heatmap", x: dates, y: bs.map((b) => b.name), z, text, hoverinfo: "text", xgap: 1, ygap: 3, zmid: 0, zmin: -cap, zmax: cap,
    colorscale: [[0, css("--neg")], [0.5, css("--mid")], [1, css("--pos")]],
    colorbar: { thickness: 10, len: 0.9, tickprefix: "$", tickformat: "~s", outlinewidth: 0 },
  }], layout({ margin: { l: 150, r: 10, t: 6, b: 34 }, xaxis: { type: "date", showgrid: false }, yaxis: { showgrid: false } }), cfg);
  const el = document.getElementById("calChart");
  el.removeAllListeners?.("plotly_click");
  el.on("plotly_click", (ev) => { const p = ev.points[0]; const b = bs[p.pointNumber[0]]; openDay(b.id, p.x.slice(0, 10)); });
}

function hourChart() {
  const f = S.hour === "arb" ? 3 : 4;
  $("#hourSub").textContent = S.hour === "arb"
    ? "Total arbitrage $ by hour of day (NZ time, hour starting) over the period. Charging hours show as negative (energy bought), discharge hours as positive (energy sold)."
    : "Total reserve $ by hour of day (NZ time, hour starting) over the period: when the batteries are paid to stand by.";
  const traces = sel().map((b) => {
    const y = Array(24).fill(0);
    D.hourly.rows.forEach((r) => { if (r[1] === b.id && r[0] >= S.from && r[0] <= S.to) y[r[2]] += r[f]; });
    return { type: "bar", name: b.name, x: y.map((_, h) => `${String(h).padStart(2, "0")}:00`), y, marker: { color: colorOf(b.id), line: { color: css("--surface-1"), width: 1 } },
      hovertemplate: `%{y:$,.0f}<extra>${b.name}</extra>` };
  });
  Plotly.react("hourChart", traces, layout({ barmode: "relative", hovermode: "x unified", xaxis: { type: "category" }, yaxis: { ...money, title: { text: S.hour === "arb" ? "net arbitrage" : "reserve revenue" } } }), cfg);
}

function topTables(rows) {
  const arb = rows.slice().sort((a, b) => b.arbitrage_rev - a.arbitrage_rev).slice(0, 10);
  $("#topArb").innerHTML = `<thead><tr><th>Date</th><th class="l">Battery</th><th>Arbitrage</th><th>Sold</th><th>Sell / buy $</th><th>Peak $</th><th class="l">What was happening</th></tr></thead><tbody>` +
    arb.map((r) => `<tr class="click" data-b="${r.bess}" data-d="${r.date}"><td>${shortDate(r.date)}</td><td class="l">${swatch(r.bess)} ${shortName(r.bess)}</td>
      <td><b>${fmt$(r.arbitrage_rev)}</b></td><td>${fmtN(r.discharge_mwh)} MWh</td><td>${fmtN(r.discharge_mwh ? r.discharge_rev / r.discharge_mwh : NaN)} / ${fmtN(r.charge_mwh ? r.charge_cost / r.charge_mwh : NaN)}</td>
      <td>${fmtN(r.node_price_max)}</td><td class="wrap">${dayTags(r.date)}</td></tr>`).join("") + "</tbody>";
  const res = rows.slice().sort((a, b) => b.reserve_rev - a.reserve_rev).slice(0, 10);
  $("#topRes").innerHTML = `<thead><tr><th>Date</th><th class="l">Battery</th><th>Reserves</th><th>FIR</th><th>SIR</th><th>Avg MW held</th><th class="l">What was happening</th></tr></thead><tbody>` +
    res.map((r) => `<tr class="click" data-b="${r.bess}" data-d="${r.date}"><td>${shortDate(r.date)}</td><td class="l">${swatch(r.bess)} ${shortName(r.bess)}</td>
      <td><b>${fmt$(r.reserve_rev)}</b></td><td>${fmt$(r.fir_rev)}</td><td>${fmt$(r.sir_rev)}</td><td>${fmtN((r.fir_mwh + r.sir_mwh) / 24)}</td><td class="wrap">${dayTags(r.date)}</td></tr>`).join("") + "</tbody>";
}

function cmpTable(rows) {
  const H = ["Battery", "Size", "Total revenue", "Arbitrage", "Reserves", "% from arbitrage", "Revenue per MW (annualised)"];
  $("#cmpTable").innerHTML = `<thead><tr>${H.map((h, i) => `<th class="${i < 2 ? "l" : ""}">${h}</th>`).join("")}</tr></thead><tbody>` + sel().map((b) => {
    const r = rows.filter((x) => x.bess === b.id); const t = sum(r);
    if (!r.length) return `<tr><td>${swatch(b.id)} ${b.name}</td><td colspan="${H.length - 1}" class="l muted">no data in this period</td></tr>`;
    return `<tr class="click" data-b="${b.id}"><td>${swatch(b.id)} ${b.name}${b.auto ? '<span class="badge">auto-detected</span>' : ""}</td><td class="l">${fmtN(b.power_mw)} MW / ${fmtN(b.energy_mwh)} MWh</td>
      <td><b>${fmt$(t.total_rev)}</b></td><td>${fmt$(t.arbitrage_rev)}</td><td>${fmt$(t.reserve_rev)}</td>
      <td>${t.total_rev > 0 ? fmtN((100 * t.arbitrage_rev) / t.total_rev) + "%" : "–"}</td><td>${fmt$(perMWyr(t.total_rev, r.filter(active).length * b.power_mw))}/MW-yr</td></tr>`;
  }).join("") + "</tbody>";
}

// =============== BATTERY ===============
function battery() {
  const b = bess(S.bess); if (!b) return;
  const all = D.daily.filter((r) => r.bess === b.id);
  const rows = all.filter(inRange), t = sum(rows);
  $("#bInfo").textContent = `${b.owner || ""} · ${fmtN(b.power_mw)} MW / ${fmtN(b.energy_mwh)} MWh · ${b.poc} (${b.gen_unit || "?"}/${b.load_unit || "?"}) · period ${prettyDate(S.from)} – ${prettyDate(S.to)}`;
  $("#bTiles").innerHTML = [
    ["Total revenue", fmt$(t.total_rev), `${fmt$(perMWyr(t.total_rev, rows.filter(active).length * b.power_mw))} per MW per year (annualised)`],
    ["From arbitrage", fmt$(t.arbitrage_rev), t.total_rev > 0 ? `${fmtN((100 * t.arbitrage_rev) / t.total_rev)}% of total` : ""],
    ["From reserves", fmt$(t.reserve_rev), `FIR ${fmt$(t.fir_rev)} · SIR ${fmt$(t.sir_rev)}`],
    ["Sell vs buy price", `$${fmtN(t.sell)} / $${fmtN(t.buy)}`, `spread $${fmtN(t.sell - t.buy)}/MWh`],
  ].map(([k, v, d]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("");

  const months = [...new Set(all.map((r) => r.date.slice(0, 7)))].sort();
  const m = (f) => months.map((k) => all.filter((r) => r.date.startsWith(k)).reduce((a, r) => a + r[f], 0));
  Plotly.react("bMonthChart", [
    { type: "bar", name: "Arbitrage", x: months.map(monthLabel), y: m("arbitrage_rev"), marker: { color: css("--arb") }, hovertemplate: "%{y:$,.0f}<extra>Arbitrage</extra>" },
    { type: "bar", name: "Reserves", x: months.map(monthLabel), y: m("reserve_rev"), marker: { color: css("--res") }, hovertemplate: "%{y:$,.0f}<extra>Reserves</extra>" },
  ], layout({ barmode: "relative", hovermode: "x unified", xaxis: { type: "category" }, yaxis: { ...money, title: { text: "revenue" } } }), cfg);

  const nAct = rows.filter(active).length || 1;
  const stats = [
    ["Energy discharged", fmtMWh(t.discharge_mwh)], ["Energy charged", fmtMWh(t.charge_mwh)],
    ["Round-trip efficiency (dispatch)", t.charge_mwh ? `${fmtN((100 * t.discharge_mwh) / t.charge_mwh)}%` : "–"],
    ["Full cycles per day", b.energy_mwh ? fmtN(t.discharge_mwh / b.energy_mwh / nAct, 2) : "–"],
    ["Average sell price", `$${fmtN(t.sell)}/MWh`], ["Average buy price", `$${fmtN(t.buy)}/MWh`],
    ["Average FIR held", `${fmtN(t.fir_mwh / 24 / nAct)} MW`], ["Average SIR held", `${fmtN(t.sir_mwh / 24 / nAct)} MW`],
    ["First active day", prettyDate(D.start[b.id])], ["Active days in period", fmtN(rows.filter(active).length)],
  ];
  $("#bStats").innerHTML = "<tbody>" + stats.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("") + "</tbody>";
  dayOrWeek();
}

// ---------- Day / Week view: three stacked panels (price, energy, reserves) ----------
const tpCache = {};
function tpMonth(m) {
  if (!tpCache[m]) tpCache[m] = fetch(`data/tp/${m}.json?v=${D.meta.generated_utc}`).then((r) => (r.ok ? r.json() : { cols: [], rows: [] }))
    .then((j) => j.rows.map((r) => Object.fromEntries(j.cols.map((c, i) => [c, r[i]]))));
  return tpCache[m];
}
const tpTime = (tp) => { const m = (tp - 1) * 30; return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; };
const tipLine = (sw, k, v) => `<div class="row"><span>${sw ? `<i class="sw" style="background:${sw}"></i>` : ""}${k}</span><span>${v}</span></div>`;
function placeTip(ev) {
  const tip = $("#dayTip"), card = $("#dayCard").getBoundingClientRect(), e = ev.event;
  tip.hidden = false;
  let left = e.clientX - card.left + 16, top = e.clientY - card.top + 12;
  if (left + tip.offsetWidth > card.width - 8) left = e.clientX - card.left - tip.offsetWidth - 16;
  if (top + tip.offsetHeight > card.height - 8) top = Math.max(8, card.height - tip.offsetHeight - 8);
  tip.style.left = `${left}px`; tip.style.top = `${top}px`;
}
const mktCache = {};
function mktMonth(m) {
  if (!mktCache[m]) mktCache[m] = fetch(`data/market/${m}.json?v=${D.meta.generated_utc}`).then((r) => (r.ok ? r.json() : { cols: [], rows: [] }))
    .then((j) => j.rows.map((r) => Object.fromEntries(j.cols.map((c, i) => [c, r[i]]))));
  return mktCache[m];
}
function threePanel(x, price, dis, chg, fir, sir, c, units, isDay, extra = {}) {
  const ln = (shape) => (isDay ? { shape: "hv" } : {});
  const traces = [
    { type: "scatter", mode: isDay ? "lines" : "lines+markers", name: isDay ? "Node price" : "Node price (daily avg)", x, y: price, line: { color: css("--price"), width: 2, ...ln() }, marker: { size: 7 }, yaxis: "y3" },
    { type: "bar", name: "Discharge (sold)", x, y: dis, marker: { color: c }, yaxis: "y2" },
    { type: "bar", name: "Charge (bought)", x, y: chg.map((v) => (v == null ? null : -v)), marker: { color: tint(c, 0.4) }, yaxis: "y2" },
    { type: "scatter", mode: isDay ? "lines" : "lines+markers", name: "FIR held", x, y: fir, line: { color: css("--fir"), width: 2, ...ln() }, marker: { size: 7 }, yaxis: "y" },
    { type: "scatter", mode: isDay ? "lines" : "lines+markers", name: "SIR held", x, y: sir, line: { color: css("--sir"), width: 2, dash: "dot", ...ln() }, marker: { size: 7 }, yaxis: "y" },
  ].concat(extra.traces || []).map((t) => ({ ...t, hoverinfo: "none" }));
  const ay = layout().yaxis, sp = { showspikes: true, spikemode: "across", spikethickness: 1, spikedash: "solid", spikecolor: css("--text-muted") };
  Plotly.react("dayChart", traces, layout({
    barmode: "relative", hovermode: "x", bargap: isDay ? 0.15 : 0.35, legend: { orientation: "h", y: -0.07, x: 0 }, margin: { l: 72, r: 16, t: 18, b: 36 },
    xaxis: { type: "category", nticks: isDay ? 13 : 8, ...sp },
    yaxis3: { ...ay, domain: [0.72, 1], tickprefix: "$", rangemode: "tozero", title: { text: "price $/MWh" } },
    yaxis2: { ...ay, domain: [0.30, 0.66], title: { text: `energy ${units}` } },
    yaxis: { ...ay, domain: [0, 0.24], rangemode: "tozero", title: { text: `reserve ${units}` } },
    shapes: extra.shapes || [],
    annotations: [["Price", 1], ["Energy: sold above zero, bought below", 0.66], ["Reserves held (paid to stand by)", 0.24]].map(([t, y]) => ({
      text: t, xref: "paper", yref: "paper", x: 0, y, xanchor: "left", yanchor: "bottom", showarrow: false, font: { size: 11, color: css("--text-muted") } })),
  }), cfg);
}

function dayOrWeek() { return S.dayMode === "week" ? weekView() : dayView(); }

async function dayView() {
  const b = bess(S.bess), day = $("#dayPick").value; if (!b || !day) return;
  $("#dayTitle").textContent = `Half-hour by half-hour: ${prettyDate(day)}`;
  $("#daySub").textContent = "Midnight to midnight. Hover any half-hour for its breakdown and the day's totals.";
  const rows = (await tpMonth(day.slice(0, 7))).filter((r) => r.bess === b.id && r.date === day).sort((a, x) => a.tp - x.tp);
  const tip = $("#dayTip"); tip.hidden = true;
  if (!rows.length) { $("#daySummary").innerHTML = `<span>No data for ${b.name} on ${prettyDate(day)}.</span>`; $("#dayEvents").innerHTML = ""; Plotly.purge("dayChart"); return; }
  const dr = D.daily.find((r) => r.bess === b.id && r.date === day), T = sum(dr ? [dr] : []);
  $("#daySummary").innerHTML = [["Day total", fmt$(T.total_rev)], ["Arbitrage", fmt$(T.arbitrage_rev)], ["Reserves", fmt$(T.reserve_rev)],
    ["Sold", `${fmtN(T.discharge_mwh)} MWh @ $${fmtN(T.sell)}`], ["Bought", `${fmtN(T.charge_mwh)} MWh @ $${fmtN(T.buy)}`]].map(([k, v]) => `<span>${k} <b>${v}</b></span>`).join("");
  const c = colorOf(b.id), x = rows.map((r) => tpTime(r.tp)), g = (f) => rows.map((r) => r[f]);
  // island prices + event shading
  const mk = Object.fromEntries((await mktMonth(day.slice(0, 7))).filter((m) => m.date === day).map((m) => [m.tp, m]));
  const ps = D.meta.price_separation || { min_gap: 50, min_ratio: 1.5 };
  const shapes = [], band = (i0, i1, col) => shapes.push({ type: "rect", xref: "x", yref: "paper", x0: i0 - 0.5, x1: i1 - 0.5, y0: 0, y1: 1, fillcolor: col, line: { width: 0 }, layer: "below" });
  rows.forEach((r, i) => { const m = mk[r.tp]; if (!m) return; const hi = Math.max(m.ni_price, m.si_price), lo = Math.max(1, Math.min(m.ni_price, m.si_price));
    if (Math.abs(m.ni_price - m.si_price) >= ps.min_gap && hi / lo >= ps.min_ratio) band(i, i + 1, css("--ev-sep")); });
  const d0 = new Date(`${day}T00:00:00Z`).getTime(), idx = (iso) => Math.max(0, Math.min(rows.length, (new Date(iso.slice(0, 16) + ":00Z").getTime() - d0) / 18e5));
  for (const e of eventsOn(day)) {
    const k = evKind(e.kind); if (!k || (e.kind === "ufe" && e.approx)) continue;
    if (k === "event") { const i = idx(e.start) - 0.5; shapes.push({ type: "line", xref: "x", yref: "paper", x0: i, x1: i, y0: 0, y1: 1, line: { color: css("--ev-trip"), width: 1.5, dash: "dot" } }); }
    else band(Math.floor(idx(e.start)), Math.ceil(idx(e.end)), css(EVK[k][1]));
  }
  const siTrace = Object.keys(mk).length ? [{ type: "scatter", mode: "lines", name: "South Island price (Benmore)", x, y: rows.map((r) => mk[r.tp]?.si_price ?? null), line: { color: css("--text-muted"), width: 1.5, dash: "dash", shape: "hv" }, yaxis: "y3" }] : [];
  threePanel(x, g("price"), g("discharge_mw"), g("charge_mw"), g("fir_mw"), g("sir_mw"), c, "MW", true, { traces: siTrace, shapes });
  evList([day]);
  const el = document.getElementById("dayChart");
  el.removeAllListeners?.("plotly_hover"); el.removeAllListeners?.("plotly_unhover");
  el.on("plotly_hover", (ev) => {
    const r = rows[ev.points[0].pointIndex];
    const il = r.fir_il_mw || r.sir_il_mw ? tipLine("", "&nbsp;&nbsp;of which while charging", `${fmtN(r.fir_il_mw, 1)} / ${fmtN(r.sir_il_mw, 1)} MW`) : "";
    const m = mk[r.tp];
    tip.innerHTML = `<h4>${tpTime(r.tp)}–${tpTime(r.tp + 1)} <span class="muted">· ${prettyDate(day)}</span></h4><div class="lbl">This half-hour</div>
      ${tipLine(css("--price"), "Node price", `$${fmtN(r.price, 2)}/MWh`)}
      ${m ? tipLine(css("--text-muted"), "South Island price", `$${fmtN(m.si_price, 2)}/MWh`) : ""}
      ${tipLine(c, "Sold (discharge)", `${fmtN(r.discharge_mw, 1)} MW · ${fmtN(r.discharge_mw / 2, 1)} MWh`)}
      ${tipLine(tint(c, 0.4), "Bought (charge)", `${fmtN(r.charge_mw, 1)} MW · ${fmtN(r.charge_mw / 2, 1)} MWh`)}
      ${tipLine(css("--fir"), "FIR held", `${fmtN(r.fir_mw, 1)} MW @ $${fmtN(r.fir_price, 2)}`)}
      ${tipLine(css("--sir"), "SIR held", `${fmtN(r.sir_mw, 1)} MW @ $${fmtN(r.sir_price, 2)}`)}${il}
      ${tipLine("", "Arbitrage", fmt$(r.arbitrage_rev))}${tipLine("", "Reserves", fmt$(r.fir_rev + r.sir_rev))}
      <hr><div class="lbl">Whole day</div>
      ${tipLine("", "Sold / bought", `${fmtN(T.discharge_mwh)} / ${fmtN(T.charge_mwh)} MWh`)}
      ${tipLine("", "Arbitrage", fmt$(T.arbitrage_rev))}${tipLine("", "Reserves (FIR / SIR)", `${fmt$(T.reserve_rev)} (${fmt$(T.fir_rev)} / ${fmt$(T.sir_rev)})`)}
      ${tipLine("", "<b>Total revenue</b>", `<b>${fmt$(T.total_rev)}</b>`)}`;
    placeTip(ev);
  });
  el.on("plotly_unhover", () => (tip.hidden = true));
}

function weekView() {
  const b = bess(S.bess), pick = $("#dayPick").value; if (!b || !pick) return;
  const mon = weekOf(pick), days = [...Array(7)].map((_, i) => addDays(mon, i));
  $("#dayTitle").textContent = `Day by day: week of ${prettyDate(mon)}`;
  $("#daySub").textContent = "Daily totals, Monday to Sunday. Hover a day for its breakdown and the week's totals.";
  const by = Object.fromEntries(D.daily.filter((r) => r.bess === b.id && days.includes(r.date)).map((r) => [r.date, r]));
  const rows = days.map((d) => by[d] || null), W = sum(rows.filter(Boolean)), tip = $("#dayTip"); tip.hidden = true;
  if (!rows.some(Boolean)) { $("#daySummary").innerHTML = `<span>No data for ${b.name} in the week of ${prettyDate(mon)}.</span>`; Plotly.purge("dayChart"); return; }
  $("#daySummary").innerHTML = [["Week total", fmt$(W.total_rev)], ["Arbitrage", fmt$(W.arbitrage_rev)], ["Reserves", fmt$(W.reserve_rev)],
    ["Sold", `${fmtN(W.discharge_mwh)} MWh @ $${fmtN(W.sell)}`], ["Bought", `${fmtN(W.charge_mwh)} MWh @ $${fmtN(W.buy)}`]].map(([k, v]) => `<span>${k} <b>${v}</b></span>`).join("");
  const c = colorOf(b.id), x = days.map((d, i) => `${DOW[i]} ${+d.slice(8)} ${MON[+d.slice(5, 7) - 1]}`), g = (f) => rows.map((r) => (r ? r[f] : null));
  const wshapes = [];
  days.forEach((d, i) => { const ks = new Set(eventsOn(d).map((e) => evKind(e.kind)).filter((k) => k === "lrs" || k === "hvdc"));
    [...ks].forEach((k) => wshapes.push({ type: "rect", xref: "x", yref: "paper", x0: i - 0.5, x1: i + 0.5, y0: 0.72, y1: 1, fillcolor: css(EVK[k][1]), line: { width: 0 }, layer: "below" })); });
  threePanel(x, g("node_price_avg"), g("discharge_mwh"), g("charge_mwh"), g("fir_mwh"), g("sir_mwh"), c, "MWh", false, { shapes: wshapes });
  evList(days);
  const el = document.getElementById("dayChart");
  el.removeAllListeners?.("plotly_hover"); el.removeAllListeners?.("plotly_unhover"); el.removeAllListeners?.("plotly_click");
  el.on("plotly_hover", (ev) => {
    const i = ev.points[0].pointIndex, r = rows[i]; if (!r) { tip.hidden = true; return; }
    const d = sum([r]);
    tip.innerHTML = `<h4>${DOW[i]} ${prettyDate(days[i])}</h4><div class="lbl">This day</div>
      ${tipLine(css("--price"), "Node price avg / peak", `$${fmtN(r.node_price_avg)} / $${fmtN(r.node_price_max)}`)}
      ${tipLine(c, "Sold (discharge)", `${fmtN(d.discharge_mwh, 1)} MWh @ $${fmtN(d.sell)}`)}
      ${tipLine(tint(c, 0.4), "Bought (charge)", `${fmtN(d.charge_mwh, 1)} MWh @ $${fmtN(d.buy)}`)}
      ${tipLine(css("--fir"), "FIR held", `${fmtN(d.fir_mwh)} MWh`)}${tipLine(css("--sir"), "SIR held", `${fmtN(d.sir_mwh)} MWh`)}
      ${tipLine("", "Arbitrage", fmt$(d.arbitrage_rev))}${tipLine("", "Reserves (FIR / SIR)", `${fmt$(d.reserve_rev)} (${fmt$(d.fir_rev)} / ${fmt$(d.sir_rev)})`)}
      ${tipLine("", "<b>Day total</b>", `<b>${fmt$(d.total_rev)}</b>`)}
      <hr><div class="lbl">Whole week</div>
      ${tipLine("", "Sold / bought", `${fmtN(W.discharge_mwh)} / ${fmtN(W.charge_mwh)} MWh`)}
      ${tipLine("", "Arbitrage", fmt$(W.arbitrage_rev))}${tipLine("", "Reserves (FIR / SIR)", `${fmt$(W.reserve_rev)} (${fmt$(W.fir_rev)} / ${fmt$(W.sir_rev)})`)}
      ${tipLine("", "<b>Week total</b>", `<b>${fmt$(W.total_rev)}</b>`)}<div class="muted" style="margin-top:4px">Click to open this day</div>`;
    placeTip(ev);
  });
  el.on("plotly_unhover", () => (tip.hidden = true));
  el.on("plotly_click", (ev) => { const d = days[ev.points[0].pointIndex]; if (by[d]) { $("#dayPick").value = d; setDayMode("day"); } });
}

function setDayMode(m) {
  S.dayMode = m;
  $("#dayMode").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.m === m));
  dayOrWeek();
}

// ---------- navigation ----------
function setTab(t) {
  S.tab = t;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === t));
  ["overview", "battery", "notes"].forEach((x) => ($(`#tab-${x}`).hidden = x !== t));
  if (t === "overview") overview(); else if (t === "battery") battery();
  window.dispatchEvent(new Event("resize"));
}
function openDay(id, date) {
  S.bess = id; $("#bSel").value = id;
  if (date) { $("#dayPick").value = date; S.dayMode = "day"; $("#dayMode").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x.dataset.m === "day")); }
  setTab("battery");
  if (date) setTimeout(() => $("#dayCard").scrollIntoView({ behavior: "smooth", block: "start" }), 50);
}
const segClick = (id, fn) => $(id).addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  $(id).querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b)); fn(b.dataset);
});

function init() {
  const dates = D.daily.map((r) => r.date).sort(), first = dates[0], last = dates[dates.length - 1];
  D.first = first; D.last = last;
  S.from = first; S.to = last;
  $("#coverage").textContent = `Data ${prettyDate(first)} – ${prettyDate(last)} · updated ${D.meta.generated_utc.replace("T", " ")} UTC`;
  ["#from", "#to", "#dayPick"].forEach((s) => { $(s).min = first; $(s).max = last; });
  $("#from").value = first; $("#to").value = last; $("#dayPick").value = last;

  const withData = new Set(D.daily.filter((r) => r.discharge_mwh > 0 || r.reserve_rev > 0).map((r) => r.bess));
  D.bess = D.bess.filter((b) => withData.has(b.id));
  D.start = {};
  D.daily.forEach((r) => { if ((r.discharge_mwh > 0 || r.charge_mwh > 0 || r.reserve_rev > 0) && (!D.start[r.bess] || r.date < D.start[r.bess])) D.start[r.bess] = r.date; });
  D.bess.forEach((b) => S.sel.add(b.id));
  S.bess = D.bess[0]?.id;
  $("#bessChips").innerHTML = D.bess.map((b) => `<label class="chip"><input type="checkbox" value="${b.id}" checked>${swatch(b.id)}${b.name}</label>`).join("");
  $("#bessChips").addEventListener("change", (e) => { e.target.checked ? S.sel.add(e.target.value) : S.sel.delete(e.target.value); overview(); });
  $("#bSel").innerHTML = D.bess.map((b) => `<option value="${b.id}">${b.name}</option>`).join("");
  $("#bSel").onchange = (e) => { S.bess = e.target.value; battery(); };

  document.querySelector(".tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setTab(b.dataset.tab); });
  const setRange = (f, t) => { S.from = f < first ? first : f; S.to = t; $("#from").value = S.from; $("#to").value = S.to; overview(); };
  $("#from").onchange = (e) => { S.from = e.target.value; $("#quick").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", false)); overview(); };
  $("#to").onchange = (e) => { S.to = e.target.value; $("#quick").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", false)); overview(); };
  segClick("#quick", (d) => setRange(d.q === "ytd" ? `${last.slice(0, 4)}-01-01` : addDays(last, -(+d.q - 1)), last));
  segClick("#perGran", (d) => { S.gran = d.g; perChart(D.daily.filter((r) => S.sel.has(r.bess) && inRange(r))); });
  segClick("#perSplit", (d) => { S.split = d.s; perChart(D.daily.filter((r) => S.sel.has(r.bess) && inRange(r))); });
  segClick("#hourMode", (d) => { S.hour = d.h; hourChart(); });
  segClick("#dayMode", (d) => setDayMode(d.m));
  ["#topArb", "#topRes", "#cmpTable"].forEach((s) => $(s).addEventListener("click", (e) => { const tr = e.target.closest("tr.click"); if (tr) openDay(tr.dataset.b, tr.dataset.d); }));
  const shift = (n) => { const step = S.dayMode === "week" ? 7 : 1; let v = addDays($("#dayPick").value, n * step); v = v < first ? first : v > last ? last : v; $("#dayPick").value = v; dayOrWeek(); };
  $("#dayPrev").onclick = () => shift(-1); $("#dayNext").onclick = () => shift(1);
  $("#dayPick").onchange = dayOrWeek;

  $("#nWarn").textContent = (D.meta.warnings || []).length;
  $("#warnList").innerHTML = (D.meta.warnings || []).map((w) => `<li>${w}</li>`).join("");
}

(async () => {
  const get = (f) => fetch(`data/${f}?v=${Date.now()}`).then((r) => r.json());
  [D.bess, D.daily, D.hourly, D.meta] = await Promise.all(["bess.json", "daily.json", "hourly.json", "meta.json"].map(get));
  D.ev = await get("events.json").catch(() => ({ events: [], days: {}, tagrev: { rows: [] } }));
  if (!D.daily.length) { $("#coverage").textContent = "No data yet: the first nightly run hasn't completed."; return; }
  init(); setTab("overview");
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => setTab(S.tab));
})();
