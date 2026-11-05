"""Indicative thermal displacement by BESS discharge.

For every trading period in which batteries in an island are discharging we ask:
"if the batteries' cleared energy were not there, which offers would have cleared instead?"

Method (a merit-order shift, not a full SPD/vSPD re-run):
  1. Take the latest energy offers for the TP (EMI Offers dataset), excluding BESS.
  2. Remove each unit's actually-cleared MW from its cheapest tranches -> what remains is uncleared supply.
  3. Walk the uncleared tranches in price order until the batteries' discharge MW is replaced.
  4. Tag each replacement tranche with its fuel. The last tranche's price is the counterfactual marginal price.

Limitations (shown on the dashboard): ignores transmission constraints/losses, HVDC re-optimisation,
energy-reserve co-optimisation, ramp limits and unit commitment (a gas peaker that would need to start
up is treated as available). It is a good indicator of *which* plant sits behind the batteries in the
stack, especially in tight periods, but not a settlement-grade counterfactual.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from . import emi

FUELS = ["gas", "coal/gas", "diesel", "hydro", "geothermal", "wind", "solar", "other"]


def _fuel_label(fuel: str, tech: str = "") -> str:
    f = f"{fuel} {tech}".lower()
    if "bat" in f or "ele" == fuel.lower():
        return "battery"
    if "coal" in f:
        return "coal/gas"
    if "gas" in f or "ocgt" in f or "ccgt" in f or "thermal" in f:
        return "gas"
    if "diesel" in f or "oil" in f or "die" in f:
        return "diesel"
    if "hyd" in f:
        return "hydro"
    if "geo" in f:
        return "geothermal"
    if "wind" in f or "win" in f:
        return "wind"
    if "sol" in f:
        return "solar"
    return "other"


def fuel_map(fleet: pd.DataFrame | None, settings: dict) -> dict:
    """{(POC, UNIT) or POC or 3-letter site: fuel}"""
    m: dict = {}
    if fleet is not None and "poc" in fleet.columns:
        for _, r in fleet.iterrows():
            lab = _fuel_label(str(r.get("fuel", "")), str(r.get("tech", "")))
            poc = str(r["poc"]).upper()
            m[(poc, str(r.get("unit", "")).upper())] = lab
            m.setdefault(poc, lab)
    for pre, lab in settings.get("thermal_prefixes", {}).items():
        m.setdefault(pre.upper(), _fuel_label(lab))
    return m


def lookup_fuel(m: dict, poc: str, unit: str) -> str:
    poc, unit = str(poc).upper(), str(unit).upper()
    return m.get((poc, unit)) or m.get(poc) or m.get(poc[:3]) or "other"


def compute_day(d: date, co: pd.DataFrame, tp_bess: pd.DataFrame, registry: list[dict],
                fmap: dict, settings: dict) -> pd.DataFrame:
    """One row per (tp, island) with BESS discharge > 0."""
    if tp_bess.empty or co is None:
        return pd.DataFrame()
    isl_of = {b["id"]: b.get("island", "NI") for b in registry}
    tb = tp_bess.assign(island=tp_bess["bess"].map(isl_of))
    need = tb.groupby(["tp", "island"]).agg(bess_mw=("discharge_mw", "sum"), ref_price=("ref_price", "first")).reset_index()
    need = need[need["bess_mw"] > 0.5]
    if need.empty:
        return pd.DataFrame()

    off = emi.load("offers", d, ["date", "tp", "poc", "unit", "trader", "product", "tranche",
                                 "mw", "price", "is_latest"])
    if off is None:
        return pd.DataFrame()
    emi.require(off, ["tp", "poc", "mw", "price"], "offers")
    if "date" in off.columns:
        off = off[off["date"] == d]
    if "is_latest" in off.columns:
        off = off[off["is_latest"].astype(str).str.upper().str.startswith("Y")]
    if "product" in off.columns:
        off = off[off["product"].str.upper().str.contains("EN")]
    if "unit" not in off.columns:
        off["unit"] = ""
    bess_pocs = {b["poc"].upper() for b in registry if b.get("poc")}
    off = off[~off["poc"].str.upper().isin(bess_pocs) & (off["mw"] > 0)]

    isl = co.dropna(subset=["island"]).drop_duplicates("poc").set_index("poc")["island"].str.upper().str[:2]
    isl = isl.replace({"NO": "NI", "SO": "SI"})
    off["island"] = off["poc"].map(isl)
    off = off.dropna(subset=["island"])

    cleared = (co[co["kind"] == "energy"].groupby(["tp", "poc", "unit"])["mw"].sum()
               .rename("cleared").reset_index())
    ccleared = cleared.groupby(["tp", "poc"])["cleared"].sum()   # fallback when unit codes differ between files

    cap = settings["tight_market"]["headroom_price_cap"]
    rows = []
    for _, n in need.iterrows():
        s = off[(off["tp"] == n["tp"]) & (off["island"] == n["island"])].copy()
        if s.empty:
            continue
        s = s.sort_values(["poc", "unit", "price"])
        cl = cleared[cleared["tp"] == n["tp"]].set_index(["poc", "unit"])["cleared"]
        rem = []
        for (poc, unit), grp in s.groupby(["poc", "unit"], sort=False):
            c = cl.get((poc, unit), np.nan)
            if np.isnan(c):
                c = ccleared.get((n["tp"], poc), 0.0) if (n["tp"], poc) in ccleared.index else 0.0
            cum = grp["mw"].cumsum()
            left = (cum - c).clip(lower=0)
            rem.append(np.minimum(grp["mw"].values, left.values))
        s["uncleared"] = np.concatenate(rem) if rem else 0
        u = s[s["uncleared"] > 0].sort_values("price")
        u["fuel"] = [lookup_fuel(fmap, p, un) for p, un in zip(u["poc"], u["unit"])]
        need_mw = n["bess_mw"]
        take = np.minimum(u["uncleared"].values, np.maximum(need_mw - (u["uncleared"].cumsum().values - u["uncleared"].values), 0))
        u["take"] = take
        used = u[u["take"] > 0]
        headroom = float(u.loc[u["price"] < cap, "uncleared"].sum())
        row = {"date": d, "tp": int(n["tp"]), "island": n["island"], "bess_mw": need_mw,
               "ref_price": n["ref_price"], "headroom_mw": headroom,
               "cf_price": float(used["price"].max()) if not used.empty else np.nan,
               "unmet_mw": max(need_mw - float(used["take"].sum()), 0.0)}
        for f in FUELS:
            row[f"disp_{f}_mw"] = float(used.loc[used["fuel"] == f, "take"].sum())
        top = used.sort_values("take", ascending=False).head(3)
        row["displaced_units"] = ", ".join(f"{p}{('/' + un) if un and un != 'nan' else ''}" for p, un in zip(top["poc"], top["unit"]))
        rows.append(row)
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    t = settings["tight_market"]
    out["tight"] = (out["ref_price"] >= t["price_threshold"]) | (out["headroom_mw"] < t["headroom_threshold_mw"])
    out["price_impact"] = out["cf_price"] - out["ref_price"]
    return out


def daily_rollup(disp: pd.DataFrame) -> pd.DataFrame:
    if disp.empty:
        return disp
    cols = [f"disp_{f}_mw" for f in FUELS]
    d = disp.copy()
    for c in cols:
        d[c.replace("_mw", "_mwh")] = d[c] * 0.5
    d["bess_mwh"] = d["bess_mw"] * 0.5
    d["thermal_mwh"] = d[["disp_gas_mwh", "disp_coal/gas_mwh", "disp_diesel_mwh"]].sum(axis=1)
    agg = {c.replace("_mw", "_mwh"): "sum" for c in cols}
    agg.update(bess_mwh="sum", thermal_mwh="sum", tp="count")
    g = d.groupby(["date", "island", "tight"]).agg(agg).rename(columns={"tp": "tps"}).reset_index()
    return g
