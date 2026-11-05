"""Per-BESS, per-trading-period dispatch and revenue."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from . import emi

TP_HOURS = 0.5


def classify(product: pd.Series, reserve_type: pd.Series | None) -> pd.Series:
    """Map EMI product codes to energy / fir / sir / bid."""
    p = product.fillna("").str.upper()
    r = reserve_type.fillna("").str.upper() if reserve_type is not None else pd.Series("", index=p.index)
    both = p + " " + r
    out = pd.Series("other", index=p.index)
    out[both.str.contains(r"\bSIR\b|SUSTAINED|\bS\b|60S|SIX ?TY")] = "sir"
    out[both.str.contains(r"\bFIR\b|FAST|\bF\b|6S")] = "fir"
    is_res = both.str.contains(r"RESERVE|\bFIR\b|\bSIR\b|\bIL\b|PLSR|TWDR|FAST|SUSTAINED|INTERRUPT")
    out[~is_res] = "other"
    out[~is_res & p.str.contains(r"EN|GEN|OFFER|^$")] = "energy"
    out[~is_res & p.str.contains(r"BID|ENBD|DEMAND|\bDD\b|LOAD|PURCHAS")] = "bid"
    return out


def cleared_long(d: date) -> pd.DataFrame | None:
    """Cleared offers for a day in long form: date, tp, island, poc, unit, trader, kind, mw (avg MW over the TP)."""
    df = emi.load("cleared_offers", d, ["date", "tp", "interval", "island", "poc", "unit", "trader",
                                        "product", "reserve_type", "mw", "mw_energy", "mw_fir", "mw_sir"])
    if df is None:
        return None
    if "tp" not in df.columns or df["tp"].isna().all():
        emi.require(df, ["interval"], "cleared_offers")
        df["tp"] = emi.interval_to_tp(df["interval"], d)
    if "date" not in df.columns:
        df["date"] = d
    for c in ("island", "unit", "trader"):
        if c not in df.columns:
            df[c] = ""
    emi.require(df, ["poc"], "cleared_offers")

    if {"mw_energy", "mw_fir", "mw_sir"} & set(df.columns) and "mw" not in df.columns:
        # wide format: one row per offer, separate cleared columns
        id_cols = [c for c in ("date", "tp", "interval", "island", "poc", "unit", "trader") if c in df.columns]
        m = df.melt(id_vars=id_cols, value_vars=[c for c in ("mw_energy", "mw_fir", "mw_sir") if c in df.columns],
                    var_name="kind", value_name="mw")
        m["kind"] = m["kind"].map({"mw_energy": "energy", "mw_fir": "fir", "mw_sir": "sir"})
        df = m
    else:
        emi.require(df, ["mw", "product"], "cleared_offers")
        df["kind"] = classify(df["product"], df.get("reserve_type"))

    df = df[df["date"] == d]
    # average over dispatch intervals: sum per interval, divide by the number of intervals in that TP market-wide
    if "interval" in df.columns and df["interval"].notna().any():
        n_int = df.groupby("tp")["interval"].nunique().rename("n_int")
        g = df.groupby(["date", "tp", "island", "poc", "unit", "trader", "kind"], dropna=False)["mw"].sum().reset_index()
        g = g.join(n_int, on="tp")
        g["mw"] = g["mw"] / g["n_int"].clip(lower=1)
        return g.drop(columns="n_int")
    return df.groupby(["date", "tp", "island", "poc", "unit", "trader", "kind"], dropna=False)["mw"].sum().reset_index()


def nodal_load(d: date) -> pd.DataFrame | None:
    df = emi.load("nodal_volumes", d, ["date", "tp", "interval", "poc", "load_mw", "gen_mw"])
    if df is None:
        return None
    if "tp" not in df.columns or df["tp"].isna().all():
        df["tp"] = emi.interval_to_tp(df["interval"], d)
    emi.require(df, ["poc", "load_mw"], "nodal_volumes")
    if "interval" in df.columns and df["interval"].notna().any():
        n_int = df.groupby("tp")["interval"].nunique().rename("n_int")
        g = df.groupby(["tp", "poc"])["load_mw"].sum().reset_index().join(n_int, on="tp")
        g["load_mw"] = g["load_mw"] / g["n_int"].clip(lower=1)
        return g.drop(columns="n_int")
    return df.groupby(["tp", "poc"])["load_mw"].mean().reset_index()


def energy_prices(d: date) -> pd.DataFrame | None:
    df = emi.load("energy_prices", d, ["date", "tp", "poc", "price"])
    if df is None:
        return None
    emi.require(df, ["tp", "poc", "price"], "energy_prices")
    if "date" in df.columns:
        df = df[df["date"] == d]
    return df.groupby(["tp", "poc"])["price"].mean().reset_index()


def reserve_prices(d: date) -> pd.DataFrame | None:
    df = emi.load("reserve_prices", d, ["date", "tp", "island", "product", "reserve_type", "price"])
    if df is None:
        return None
    if "date" in df.columns:
        df = df[df["date"] == d]
    emi.require(df, ["tp", "island", "price"], "reserve_prices")
    col = "reserve_type" if "reserve_type" in df.columns else "product"
    t = df[col].str.upper()
    df["kind"] = np.where(t.str.contains("S"), "sir", "fir")
    df.loc[t.str.contains("FIR|FAST|^F"), "kind"] = "fir"
    df["island"] = df["island"].str.upper().str[:2].replace({"NO": "NI", "SO": "SI", "N": "NI", "S": "SI"})
    return df.pivot_table(index=["tp", "island"], columns="kind", values="price", aggfunc="mean").reset_index()


def compute_day(d: date, registry: list[dict], settings: dict) -> tuple[pd.DataFrame, list[str]]:
    """TP-level metrics for every active BESS on trading date d. Returns (frame, warnings)."""
    warn: list[str] = []
    co = cleared_long(d)
    ep = energy_prices(d)
    rp = reserve_prices(d)
    if co is None or ep is None:
        return pd.DataFrame(), [f"{d}: cleared offers or energy prices not yet published"]
    if rp is None:
        warn.append(f"{d}: reserve prices missing - reserve revenue set to 0")
    need_nodal = any(b.get("charge_source") == "nodal_load" and b.get("charge_poc") for b in registry)
    nl = nodal_load(d) if need_nodal else None
    if need_nodal and nl is None:
        warn.append(f"{d}: nodal volumes missing - charging from nodal load unavailable")

    ref = settings["reference_nodes"]
    rows = []
    for b in registry:
        if not b.get("poc") or pd.Timestamp(b["commissioned"]).date() > d:
            continue
        sel = co["poc"].str.upper() == b["poc"].upper()
        if b.get("unit"):
            sel &= co["unit"].str.upper() == str(b["unit"]).upper()
        x = co[sel]
        tps = pd.DataFrame({"tp": range(1, int(co["tp"].max()) + 1)})
        piv = (x.pivot_table(index="tp", columns="kind", values="mw", aggfunc="sum")
               .reindex(columns=["energy", "fir", "sir", "bid"]).fillna(0).reset_index())
        t = tps.merge(piv, on="tp", how="left").fillna(0)
        t = t.rename(columns={"energy": "discharge_mw", "fir": "fir_mw", "sir": "sir_mw", "bid": "bid_mw"})

        # charging
        src = b.get("charge_source", "none")
        t["charge_mw"] = 0.0
        if src == "nodal_load" and nl is not None and b.get("charge_poc"):
            c = nl[nl["poc"].str.upper() == b["charge_poc"].upper()][["tp", "load_mw"]]
            t = t.merge(c, on="tp", how="left")
            t["charge_mw"] = t.pop("load_mw").fillna(0).clip(lower=0)
        elif src == "cleared_bid":
            t["charge_mw"] = t["bid_mw"].abs()
            if t["charge_mw"].sum() == 0 and t["discharge_mw"].sum() > 0:
                warn.append(f"{d} {b['id']}: no cleared charging bids found - arbitrage excludes charging cost")
        t = t.drop(columns="bid_mw")

        # prices
        island = b.get("island", "NI")
        p = ep[ep["poc"].str.upper() == b["poc"].upper()][["tp", "price"]]
        if p.empty:
            p = ep[ep["poc"] == ref[island]][["tp", "price"]]
            warn.append(f"{d} {b['id']}: no price at {b['poc']} - using {ref[island]}")
        t = t.merge(p, on="tp", how="left")
        rpi = ep[ep["poc"] == ref[island]][["tp", "price"]].rename(columns={"price": "ref_price"})
        t = t.merge(rpi, on="tp", how="left")
        if rp is not None:
            r = rp[rp["island"] == island].drop(columns="island").rename(columns={"fir": "fir_price", "sir": "sir_price"})
            t = t.merge(r, on="tp", how="left")
        for c in ("fir_price", "sir_price"):
            if c not in t:
                t[c] = 0.0
        t[["fir_price", "sir_price"]] = t[["fir_price", "sir_price"]].fillna(0)

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
    out["max_discharge_mw"] = g["discharge_mw"].max()
    out["tps_discharging"] = g["discharge_mw"].apply(lambda v: int((v > 0.5).sum()))
    out["tps_charging"] = g["charge_mw"].apply(lambda v: int((v > 0.5).sum()))
    out = out.reset_index()
    out["reserve_rev"] = out["fir_rev"] + out["sir_rev"]
    out["total_rev"] = out["arbitrage_rev"] + out["reserve_rev"]
    return out
