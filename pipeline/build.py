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


def dates_to_run(settings, done: set, start=None, end=None):
    yesterday = datetime.now(NZ).date() - timedelta(days=1)
    if start:
        s, e = start, end or yesterday
        return [s + timedelta(days=i) for i in range((e - s).days + 1)]
    s = pd.Timestamp(settings["start_date"]).date()
    allds = [s + timedelta(days=i) for i in range((yesterday - s).days + 1)]
    recent = set(allds[-settings["reprocess_days"]:])
    missing = [d for d in allds if d not in done and d not in recent][: settings["max_new_days_per_run"]]
    return sorted(set(missing) | recent)


def run(start=None, end=None, skip_displacement=False):
    settings, registry = load_config()
    warnings: list[str] = []
    fleet = load_fleet()
    registry = autodetect(fleet, registry, warnings)
    fmap = D.fuel_map(fleet, settings)

    tp_all = _read_parts("tp")
    disp_all = _read_parts("displacement")
    done = set(tp_all["date"]) if not tp_all.empty else set()
    days = dates_to_run(settings, done, start, end)
    print(f"processing {len(days)} day(s): {days[:1]} .. {days[-1:]}")

    new_tp, new_disp, done_days = [], [], []
    for d in days:
        try:
            co = B.cleared_long(d)
            t, w = B.compute_day(d, registry, settings, co)
            warnings += w
            if t.empty:
                continue
            new_tp.append(t)
            done_days.append(d)
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

    if new_tp:
        nt = pd.concat(new_tp, ignore_index=True)
        tp_all = pd.concat([tp_all[~tp_all["date"].isin(done_days)] if not tp_all.empty else tp_all, nt], ignore_index=True)
        _write_parts("tp", tp_all[tp_all["date"].isin(done_days)])
    if new_disp:
        nd = pd.concat(new_disp, ignore_index=True)
        disp_all = pd.concat([disp_all[~disp_all["date"].isin(done_days)] if not disp_all.empty else disp_all, nd], ignore_index=True)
        _write_parts("displacement", disp_all[disp_all["date"].isin(done_days)])

    publish(tp_all, disp_all, registry, settings, warnings)


def _jsonable(df):
    df = df.copy()
    for c in df.columns:
        if c == "date":
            df[c] = df[c].astype(str)
        elif df[c].dtype.kind == "f":
            df[c] = df[c].round(3)
    return json.loads(df.to_json(orient="records"))


def publish(tp, disp, registry, settings, warnings):
    os.makedirs(OUT, exist_ok=True)
    daily = B.daily_rollup(tp, registry) if not tp.empty else pd.DataFrame()

    # intraday profile: mean MW per trading period, per BESS per month (how the battery is run through the day)
    prof = pd.DataFrame()
    if not tp.empty:
        x = tp.assign(month=pd.to_datetime(tp["date"]).dt.strftime("%Y-%m"))
        prof = x.groupby(["bess", "month", "tp"])[["discharge_mw", "charge_mw", "fir_mw", "sir_mw", "price"]].mean().reset_index()

    dd = D.daily_rollup(disp) if not disp.empty else pd.DataFrame()
    top = pd.DataFrame()
    if not disp.empty:
        top = disp.sort_values(["tight", "disp_gas_mw"], ascending=False)
        top = top[top["tight"]].nlargest(60, "bess_mw") if top["tight"].any() else top.nlargest(30, "bess_mw")

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

    reg = [{k: (str(v) if k == "commissioned" else v) for k, v in b.items()} for b in registry]
    meta = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "first_date": str(tp["date"].min()) if not tp.empty else None,
        "last_date": str(tp["date"].max()) if not tp.empty else None,
        "settings": {k: settings[k] for k in ("tight_market", "reference_nodes")},
        "tp_months": sorted(months),
        "warnings": sorted(set(warnings))[-60:],
    }
    files = {"bess.json": reg, "daily.json": _jsonable(daily) if not daily.empty else [],
             "profile.json": _jsonable(prof) if not prof.empty else [],
             "displacement_daily.json": _jsonable(dd) if not dd.empty else [],
             "tight_periods.json": _jsonable(top) if not top.empty else [], "meta.json": meta}
    for n, obj in files.items():
        with open(f"{OUT}/{n}", "w") as f:
            json.dump(obj, f, separators=(",", ":"), default=str)
    print(f"published {len(files)} files to {OUT}; {len(warnings)} warning(s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--skip-displacement", action="store_true")
    a = ap.parse_args()
    run(pd.Timestamp(a.start).date() if a.start else None, pd.Timestamp(a.end).date() if a.end else None,
        a.skip_displacement)
