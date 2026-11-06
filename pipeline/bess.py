"""Per-BESS, per-trading-period dispatch and revenue.

EMI data model (confirmed Nov 2026):
  ClearedOffers  - one row per RTD interval per unit: ClearedGenerationMegawatts / ClearedFIRMegawatts /
                   ClearedSIRMegawatts, ProductClass Injection|Offtake.
  NodalPricesAndVolumes - one row per RTD interval per POC/unit: LoadMegawatts, GenerationMegawatts.
  Each battery = injection unit (discharge) + offtake unit (charging); both can clear reserves.
Interval values are averaged to half-hours: sum over the intervals in the TP / number of RTD intervals
in that TP (market-wide), because a unit has no row in intervals where nothing cleared.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from . import emi

TP_HOURS = 0.5
KEYS = ["date", "tp", "island", "poc", "unit", "trader"]


def _to_tp(df: pd.DataFrame, d: date) -> pd.DataFrame:
    if "tp" not in df.columns or df["tp"].isna().all():
        emi.require(df, ["interval"], "interval data")
        df["tp"] = emi.interval_to_tp(df["interval"], d)
    if "date" in df.columns:
        df = df[df["date"] == d]
    else:
        df["date"] = d
    return df


def _tp_mean(df: pd.DataFrame, keys: list[str], vals: list[str]) -> pd.DataFrame:
    """Sum per interval, divide by number of intervals in the TP market-wide."""
    if "interval" in df.columns and df["interval"].notna().any():
        n = df.groupby("tp")["interval"].nunique().rename("_n")
        g = df.groupby(keys, dropna=False)[vals].sum().reset_index().join(n, on="tp")
        g[vals] = g[vals].div(g["_n"].clip(lower=1), axis=0)
        return g.drop(columns="_n")
    return df.groupby(keys, dropna=False)[vals].mean().reset_index()


def cleared_long(d: date) -> pd.DataFrame | None:
    """Cleared offers for a day, long form: date, tp, island, poc, unit, trader, flow, kind (energy|fir|sir), mw."""
    df = emi.load("cleared_offers", d, ["date", "tp", "interval", "island", "poc", "unit", "trader", "flow",
                                        "mw_energy", "mw_fir", "mw_sir"])
    if df is None:
        return None
    df = _to_tp(df, d)
    for c in ("island", "unit", "trader", "flow"):
        if c not in df.columns:
            df[c] = ""
    emi.require(df, ["poc", "mw_energy"], "cleared_offers")
    for c in ("mw_energy", "mw_fir", "mw_sir"):
        if c not in df.columns:
            df[c] = 0.0
        df[c] = df[c].fillna(0)
    g = _tp_mean(df, KEYS + ["flow"], ["mw_energy", "mw_fir", "mw_sir"])
    m = g.melt(id_vars=KEYS + ["flow"], value_vars=["mw_energy", "mw_fir", "mw_sir"], var_name="kind", value_name="mw")
    m["kind"] = m["kind"].str.replace("mw_", "", regex=False)
    return m[m["mw"] != 0]


def nodal_volumes(d: date) -> pd.DataFrame | None:
    df = emi.load("nodal_volumes", d, ["date", "tp", "interval", "poc", "unit", "load_mw", "gen_mw"])
    if df is None:
        return None
    df = _to_tp(df, d)
    if "unit" not in df.columns:
        df["unit"] = ""
    emi.require(df, ["poc", "load_mw"], "nodal_volumes")
    if "gen_mw" not in df.columns:
        df["gen_mw"] = 0.0
    df[["load_mw", "gen_mw"]] = df[["load_mw", "gen_mw"]].fillna(0)
    return _tp_mean(df, ["tp", "poc", "unit"], ["load_mw", "gen_mw"])


def energy_prices(d: date) -> pd.DataFrame | None:
    df = emi.load("energy_prices", d, ["date", "tp", "poc", "price"])
    if df is None:
        return None
    emi.require(df, ["tp", "poc", "price"], "energy_prices")
    if "date" in df.columns:
        df = df[df["date"] == d]
    return df.groupby(["tp", "poc"])["price"].mean().reset_index()


def reserve_prices(d: date) -> pd.DataFrame | None:
    """tp, island, fir_price, sir_price"""
    df = emi.load("reserve_prices", d, ["date", "tp", "island", "fir_price", "sir_price",
                                        "product", "reserve_type", "price"])
    if df is None:
        return None
    if "date" in df.columns:
        df = df[df["date"] == d]
    emi.require(df, ["tp", "island"], "reserve_prices")
    if {"fir_price", "sir_price"} <= set(df.columns):           # wide (current) format
        return df.groupby(["tp", "island"])[["fir_price", "sir_price"]].mean().reset_index()
    col = "reserve_type" if "reserve_type" in df.columns else "product"   # long (older) format
    df["kind"] = np.where(df[col].str.upper().str.contains("S"), "sir_price", "fir_price")
    return df.pivot_table(index=["tp", "island"], columns="kind", values="price", aggfunc="mean").reset_index()


def _is(series: pd.Series, value) -> pd.Series:
    return series.str.upper() == str(value or "").upper()


def compute_day(d: date, registry: list[dict], settings: dict, co: pd.DataFrame | None = None
                ) -> tuple[pd.DataFrame, list[str]]:
    """TP-level metrics for every active BESS on trading date d. Returns (frame, warnings)."""
    warn: list[str] = []
    co = cleared_long(d) if co is None else co
    ep = energy_prices(d)
    if co is None or ep is None:
        return pd.DataFrame(), [f"{d}: cleared offers or energy prices not yet published"]
    rp = reserve_prices(d)
    if rp is None:
        warn.append(f"{d}: reserve prices missing - reserve revenue set to 0")
    nv = nodal_volumes(d)
    if nv is None:
        warn.append(f"{d}: nodal volumes missing - charging unavailable")

    ref = settings["reference_nodes"]
    n_tp = int(max(co["tp"].max(), ep["tp"].max()))
    rows = []
    for b in registry:
        if not b.get("poc") or pd.Timestamp(b["commissioned"]).date() > d:
            continue
        at_poc = co[_is(co["poc"], b["poc"])]
        units = [u for u in (b.get("gen_unit"), b.get("load_unit")) if u]
        mine = at_poc[at_poc["unit"].str.upper().isin([u.upper() for u in units])] if units else at_poc
        t = pd.DataFrame({"tp": range(1, n_tp + 1)})
        gen = mine[_is(mine["unit"], b.get("gen_unit")) & (mine["kind"] == "energy")] if b.get("gen_unit") else \
            mine[mine["kind"] == "energy"]
        t = t.merge(gen.groupby("tp")["mw"].sum().rename("discharge_mw"), on="tp", how="left")
        for k in ("fir", "sir"):
            t = t.merge(mine[mine["kind"] == k].groupby("tp")["mw"].sum().rename(f"{k}_mw"), on="tp", how="left")
            t = t.merge(mine[(mine["kind"] == k) & mine["flow"].str.upper().str.startswith("OFF")]
                        .groupby("tp")["mw"].sum().rename(f"{k}_il_mw"), on="tp", how="left")

        t["charge_mw"] = 0.0
        if nv is not None and b.get("load_unit"):
            c = nv[_is(nv["poc"], b["poc"]) & _is(nv["unit"], b["load_unit"])].groupby("tp")["load_mw"].sum()
            t = t.merge(c.rename("_c"), on="tp", how="left")
            t["charge_mw"] = t.pop("_c").fillna(0).clip(lower=0)
        elif not b.get("load_unit"):
            warn.append(f"{d} {b['id']}: no load_unit configured - charging not counted")
        t = t.fillna(0)

        island = b.get("island", "NI")
        p = ep[_is(ep["poc"], b["poc"])][["tp", "price"]]
        if p.empty:
            p = ep[ep["poc"] == ref[island]][["tp", "price"]]
            warn.append(f"{d} {b['id']}: no price at {b['poc']} - using {ref[island]}")
        t = t.merge(p, on="tp", how="left")
        t = t.merge(ep[ep["poc"] == ref[island]][["tp", "price"]].rename(columns={"price": "ref_price"}), on="tp", how="left")
        if rp is not None:
            t = t.merge(rp[rp["island"] == island].drop(columns="island"), on="tp", how="left")
        for c in ("fir_price", "sir_price"):
            t[c] = t[c].fillna(0) if c in t else 0.0

        t["discharge_mwh"] = t["discharge_mw"] * TP_HOURS
        t["charge_mwh"] = t["charge_mw"] * TP_HOURS
        t["discharge_rev"] = t["discharge_mwh"] * t["price"]
        t["charge_cost"] = t["charge_mwh"] * t["price"]
        t["arbitrage_rev"] = t["discharge_rev"] - t["charge_cost"]
        t["fir_rev"] = t["fir_mw"] * t["fir_price"] * TP_HOURS
        t["sir_rev"] = t["sir_mw"] * t["sir_price"] * TP_HOURS
        t["date"] = d
        t["bess"] = b["id"]
        rows.append(t)
    if not rows:
        return pd.DataFrame(), warn
    return pd.concat(rows, ignore_index=True), warn


def daily_rollup(tp: pd.DataFrame, registry: list[dict]) -> pd.DataFrame:
    """Daily per-BESS sums - the dashboard aggregates these to week/month/year in the browser."""
    if tp.empty:
        return tp
    s = ["discharge_mwh", "charge_mwh", "discharge_rev", "charge_cost", "arbitrage_rev", "fir_rev", "sir_rev"]
    g = tp.groupby(["date", "bess"])
    out = g[s].sum()
    out["fir_mwh"] = g["fir_mw"].sum() * TP_HOURS      # MW-hours of reserve cleared
    out["sir_mwh"] = g["sir_mw"].sum() * TP_HOURS
    out["ref_price_avg"] = g["ref_price"].mean()
    out["node_price_avg"] = g["price"].mean()
    out["node_price_max"] = g["price"].max()
    out["max_discharge_mw"] = g["discharge_mw"].max()
    out["tps_discharging"] = g["discharge_mw"].apply(lambda v: int((v > 0.5).sum()))
    out["tps_charging"] = g["charge_mw"].apply(lambda v: int((v > 0.5).sum()))
    out = out.reset_index()
    out["reserve_rev"] = out["fir_rev"] + out["sir_rev"]
    out["total_rev"] = out["arbitrage_rev"] + out["reserve_rev"]
    return out
