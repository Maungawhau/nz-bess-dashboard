"""Nightly build: fetch new EMI days, compute metrics, write dashboard data.

    python -m pipeline.build                 # incremental (what the nightly job runs)
    python -m pipeline.build --from 2026-03-01 --to 2026-03-07   # (re)process a range
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from . import bess as B
from . import displacement as D
from . import emi
from . import events as E

PROC = "data/processed"
OUT = "docs/data"
NZ = ZoneInfo("Pacific/Auckland")


def load_config(path=os.environ.get("BESS_CONFIG", "config/bess.yaml")):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return cfg["settings"], cfg["bess"]


def load_fleet():
    p = emi.latest_file("fleet")
    if not p:
        return None
    want = ["poc", "unit", "fuel", "tech", "name", "capacity", "trader", "decommissioned"]
    df = emi.resolve(emi.read_csv(p, usecols=emi.usecols_for(want)), want, "fleet")
    for c in ("poc", "unit", "fuel", "tech", "name", "trader"):
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()
    return df


def autodetect(fleet, registry, warnings):
    """Add batteries in EMI's plant list (tech BAT) that aren't in the config - flagged for confirmation."""
    if fleet is None or "poc" not in fleet.columns or "unit" not in fleet.columns:
        return registry
    txt = (fleet["fuel"].astype(str) if "fuel" in fleet else "") + " " + (fleet["tech"].astype(str) if "tech" in fleet else "")
    bat = fleet[txt.str.upper().str.contains(r"\bBAT|BATTERY|BESS")]
    if "decommissioned" in bat.columns:
        bat = bat[bat["decommissioned"].isna()]
    known = {(str(b.get("poc")).upper(), str(u).upper()) for b in registry for u in (b.get("gen_unit"), b.get("load_unit")) if u}
    for _, r in bat.drop_duplicates(["poc", "unit"]).iterrows():
        if (r["poc"].upper(), r["unit"].upper()) in known:
            continue
        nid = f"auto_{r['unit'].lower()}"
        registry.append({"id": nid, "name": str(r.get("name", r["unit"])), "owner": str(r.get("trader", "")),
                         "island": "NI", "power_mw": float(r.get("capacity", 0) or 0), "energy_mwh": None,
                         "commissioned": "2000-01-01", "poc": r["poc"], "gen_unit": r["unit"], "load_unit": None,
                         "auto": True})
        warnings.append(f"New battery auto-detected: {r.get('name', '')} at {r['poc']}/{r['unit']} - add it (with its "
                        f"offtake unit) to config/bess.yaml; until then charging is not counted")
    return registry


def _read_parts(name):
    files = sorted(glob.glob(f"{PROC}/{name}/*.csv.gz"))
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def _write_parts(name, df):
    os.makedirs(f"{PROC}/{name}", exist_ok=True)
    if df.empty:
        return
    df = df.copy()
    df["_m"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m")
    for m, g in df.groupby("_m"):
        g.drop(columns="_m").sort_values([c for c in ("date", "tp", "bess", "island") if c in g]).to_csv(
            f"{PROC}/{name}/{m}.csv.gz", index=False)


INTERIM_FILE = f"{PROC}/interim_days.json"


def load_interim() -> set:
    try:
        return {pd.Timestamp(x).date() for x in json.load(open(INTERIM_FILE))}
    except (FileNotFoundError, ValueError):
        return set()


def dates_to_run(settings, done: set, start=None, end=None, interim: set = frozenset()):
    yesterday = datetime.now(NZ).date() - timedelta(days=1)
    if start:
        s, e = start, end or yesterday
        return [s + timedelta(days=i) for i in range((e - s).days + 1)]
    s = pd.Timestamp(settings["start_date"]).date()
    allds = [s + timedelta(days=i) for i in range((yesterday - s).days + 1)]
    recent = set(allds[-settings["reprocess_days"]:])
    missing = [d for d in allds if d not in done and d not in recent][: settings["max_new_days_per_run"]]
    return sorted(set(missing) | recent | set(interim))     # days still on interim prices are retried until final


def run(start=None, end=None, skip_displacement=False, publish_only=False):
    settings, registry = load_config()
    skip_displacement = skip_displacement or not settings.get("run_displacement", False)
    if publish_only:   # rebuild dashboard files from the saved half-hourly history, no downloads
        publish(_read_parts("tp"), pd.DataFrame(), registry, settings, [], _read_parts("market"), _events(settings, [], refresh=False))
        return
    warnings: list[str] = []
    fleet = load_fleet()
    registry = autodetect(fleet, registry, warnings)
    fmap = D.fuel_map(fleet, settings)

    tp_all = _read_parts("tp")
    disp_all = _read_parts("displacement")
    done = set(tp_all["date"]) if not tp_all.empty else set()
    interim = load_interim()
    days = dates_to_run(settings, done, start, end, interim)
    print(f"processing {len(days)} day(s): {days[:1]} .. {days[-1:]}")

    market = _read_parts("market")
    have_mkt = set(market["date"]) if not market.empty else set()
    new_tp, new_disp, done_days, new_mkt = [], [], [], []
    for d in days:
        try:
            co = B.cleared_long(d)
            t, w = B.compute_day(d, registry, settings, co)
            warnings += w
            if t.empty:
                continue
            new_tp.append(t)
            done_days.append(d)
            if {("energy_prices", d), ("reserve_prices", d)} & emi.INTERIM_USED:
                interim.add(d)
            else:
                interim.discard(d)
            new_mkt.append(E.market_day(B.energy_prices(d), B.reserve_prices(d), d, settings))
            if not skip_displacement:
                x = D.compute_day(d, co, t, registry, fmap, settings)
                if not x.empty:
                    new_disp.append(x)
            print(f"  {d}: ok ({t['bess'].nunique()} BESS)")
        except Exception as e:  # keep going - one bad file must not stop the night
            warnings.append(f"{d}: {type(e).__name__}: {e}")
            print(f"  {d}: FAILED {e}")
        finally:
            emi.clear_cache()

    os.makedirs(PROC, exist_ok=True)
    with open(INTERIM_FILE, "w") as f:
        json.dump(sorted(str(x) for x in interim), f)
    settings["_interim_days"] = sorted(str(x) for x in interim)

    # island prices for days in the history that don't have them yet (price files only - fast)
    hist_days = sorted(set(tp_all["date"]) if not tp_all.empty else set())
    todo = [d for d in hist_days if d not in have_mkt and d not in done_days]
    if todo:
        print(f"back-filling island prices for {len(todo)} day(s)")
    for d in todo:
        try:
            new_mkt.append(E.market_day(B.energy_prices(d), B.reserve_prices(d), d, settings))
        except Exception as e:
            warnings.append(f"{d}: island prices: {e}")
        finally:
            emi.clear_cache()
    new_mkt = [m for m in new_mkt if not m.empty]
    if new_mkt:
        nm = pd.concat(new_mkt, ignore_index=True)
        nm["date"] = pd.to_datetime(nm["date"]).dt.date
        market = pd.concat([market[~market["date"].isin(set(nm["date"]))] if not market.empty else market, nm], ignore_index=True)
        _write_parts("market", market[market["date"].isin(set(nm["date"]))])

    if new_tp:
        nt = pd.concat(new_tp, ignore_index=True)
        tp_all = pd.concat([tp_all[~tp_all["date"].isin(done_days)] if not tp_all.empty else tp_all, nt], ignore_index=True)
        _write_parts("tp", tp_all[tp_all["date"].isin(done_days)])
    if new_disp:
        nd = pd.concat(new_disp, ignore_index=True)
        disp_all = pd.concat([disp_all[~disp_all["date"].isin(done_days)] if not disp_all.empty else disp_all, nd], ignore_index=True)
        _write_parts("displacement", disp_all[disp_all["date"].isin(done_days)])

    publish(tp_all, disp_all, registry, settings, warnings, market, _events(settings, warnings, refresh=True))


def _events(settings, warnings, refresh=True):
    store = E.update_notices(warnings) if refresh else (pd.read_csv(E.STORE, dtype=str).fillna("") if os.path.exists(E.STORE) else pd.DataFrame())
    return E.active_events(store, pd.Timestamp(settings["start_date"]).date())


def _jsonable(df):
    df = df.copy()
    for c in df.columns:
        if c == "date":
            df[c] = df[c].astype(str)
        elif df[c].dtype.kind == "f":
            df[c] = df[c].round(3)
    return json.loads(df.to_json(orient="records"))


def publish(tp, disp, registry, settings, warnings, market=None, events=None):
    market = market if market is not None else pd.DataFrame()
    events = events or []
    os.makedirs(OUT, exist_ok=True)
    daily = B.daily_rollup(tp, registry) if not tp.empty else pd.DataFrame()

    # revenue by hour of day, per battery per date (compact columnar) - "when in the day is money made"
    tod = {"cols": ["date", "bess", "hour", "arb", "res"], "rows": []}
    if not tp.empty:
        h = tp.assign(hour=((tp["tp"] - 1) // 2).clip(upper=23), res=tp["fir_rev"] + tp["sir_rev"])
        h = h.groupby(["date", "bess", "hour"])[["arbitrage_rev", "res"]].sum().reset_index()
        h = h[(h["arbitrage_rev"].abs() > 0.5) | (h["res"].abs() > 0.5)]
        h["date"] = h["date"].astype(str)
        h[["arbitrage_rev", "res"]] = h[["arbitrage_rev", "res"]].round(0)
        tod["rows"] = h[["date", "bess", "hour", "arbitrage_rev", "res"]].values.tolist()

    # half-hourly detail for the day view: one compact columnar file per month, loaded on demand
    months = []
    if not tp.empty:
        os.makedirs(f"{OUT}/tp", exist_ok=True)
        cols = ["date", "tp", "bess", "discharge_mw", "charge_mw", "fir_mw", "sir_mw", "fir_il_mw", "sir_il_mw",
                "price", "fir_price", "sir_price", "arbitrage_rev", "fir_rev", "sir_rev"]
        x = tp.reindex(columns=cols).copy()
        x["date"] = x["date"].astype(str)
        x["_m"] = x["date"].str[:7]
        for m, g in x.groupby("_m"):
            g = g.drop(columns="_m").sort_values(["date", "bess", "tp"]).fillna(0)
            for c in cols[3:]:
                g[c] = g[c].astype(float).round(2)
            with open(f"{OUT}/tp/{m}.json", "w") as f:
                json.dump({"cols": cols, "rows": g.values.tolist()}, f, separators=(",", ":"), default=str)
            months.append(m)

    # ---- system events: notices, island prices, and revenue earned during tagged half-hours ----
    ev_out = {"events": events, "days": {}, "tagrev": {"cols": ["date", "bess", "tag", "arb", "res"], "rows": []}}
    if not tp.empty:
        tags = E.tag_frame(tp[["date", "tp"]], events, market, settings)
        x = tp.assign(date=tp["date"].astype(str), res=tp["fir_rev"] + tp["sir_rev"]).merge(tags, on=["date", "tp"], how="left")
        for c in ("lrs", "hvdc", "event", "sep"):
            x[c] = x[c].fillna(False).astype(bool)
        x["none"] = ~(x["lrs"] | x["hvdc"] | x["event"] | x["sep"])
        rows = []
        for tag in ("lrs", "hvdc", "event", "sep", "none"):
            g = x[x[tag]].groupby(["date", "bess"])[["arbitrage_rev", "res"]].sum().round(0).reset_index()
            rows += [[r.date, r.bess, tag, r.arbitrage_rev, r.res] for r in g.itertuples()]
        ev_out["tagrev"]["rows"] = rows
        # per-day summary of island separation
        if not market.empty:
            m = market.assign(date=market["date"].astype(str))
            m = m.merge(tags[["date", "tp", "sep"]], on=["date", "tp"], how="left")
            m["gap"] = (m["ni_price"] - m["si_price"])
            for d, g in m.groupby("date"):
                sg = g[g["sep"].fillna(False).astype(bool)]
                if len(sg):
                    i = sg["gap"].abs().idxmax()
                    ev_out["days"][d] = {"sep_tps": int(len(sg)), "max_gap": round(float(sg.loc[i, "gap"]), 0),
                                         "first": int(sg["tp"].min()), "last": int(sg["tp"].max())}
        # island prices for the day view, one file per month
        if not market.empty:
            os.makedirs(f"{OUT}/market", exist_ok=True)
            mm = market.assign(date=market["date"].astype(str)).sort_values(["date", "tp"])
            cols = [c for c in ["date", "tp", "ni_price", "si_price", "ni_fir_price", "ni_sir_price", "si_fir_price", "si_sir_price"] if c in mm]
            for mon, g in mm.groupby(mm["date"].str[:7]):
                g = g[cols].copy()
                for c in cols[2:]:
                    g[c] = g[c].astype(float).round(2)
                with open(f"{OUT}/market/{mon}.json", "w") as f:
                    json.dump({"cols": cols, "rows": g.fillna(0).values.tolist()}, f, separators=(",", ":"), default=str)

    reg = [{k: (str(v) if k == "commissioned" else v) for k, v in b.items()} for b in registry]
    meta = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "first_date": str(tp["date"].min()) if not tp.empty else None,
        "last_date": str(tp["date"].max()) if not tp.empty else None,
        "settings": {k: settings[k] for k in ("tight_market", "reference_nodes")},
        "tp_months": sorted(months),
        "interim_days": settings.get("_interim_days", []),
        "price_separation": settings.get("price_separation", {"min_gap": 50, "min_ratio": 1.5}),
        "warnings": sorted(set(warnings))[-60:],
    }
    files = {"bess.json": reg, "daily.json": _jsonable(daily) if not daily.empty else [],
             "hourly.json": tod, "events.json": ev_out, "meta.json": meta}
    for n, obj in files.items():
        with open(f"{OUT}/{n}", "w") as f:
            json.dump(obj, f, separators=(",", ":"), default=str)
    print(f"published {len(files)} files to {OUT}; {len(warnings)} warning(s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--skip-displacement", action="store_true")
    ap.add_argument("--publish-only", action="store_true")
    a = ap.parse_args()
    run(pd.Timestamp(a.start).date() if a.start else None, pd.Timestamp(a.end).date() if a.end else None,
        a.skip_displacement, a.publish_only)
