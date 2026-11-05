"""Generate EMI-shaped synthetic files so the pipeline can be tested without network access.

    python tests/make_synthetic.py tests/fixtures 2026-01-01 2026-02-28
    EMI_LOCAL_DIR=tests/fixtures python -m pipeline.build --from 2026-01-01 --to 2026-02-28
"""
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

R = "Datasets/Wholesale"
rng = np.random.default_rng(7)

# poc, unit, trader, island, fuel, tranches [(mw, $)]
UNITS = [
    ("WKM2201", "WKM", "MERI", "NI", "Hydro", [(400, 20), (300, 90), (150, 220)]),
    ("WRK2201", "WRK", "CTCT", "NI", "Geothermal", [(150, 0.01)]),
    ("TWH2201", "TWH", "MERI", "NI", "Wind", [(200, 0.01)]),
    ("HLY2201", "HLY5", "GENE", "NI", "Gas", [(250, 160), (130, 210)]),
    ("HLY2201", "HLY1", "GENE", "NI", "Coal/Gas", [(240, 280)]),
    ("SFD2201", "SFD21", "CTCT", "NI", "Gas", [(50, 330), (50, 420)]),
    ("MKE1101", "MKE", "TODD", "NI", "Gas", [(100, 360)]),
    ("BEN2202", "BEN", "MERI", "SI", "Hydro", [(500, 30), (300, 80)]),
    ("GLN0332", "GLN0", "NZSC", "NI", "Gas", [(26, 0.01)]),
]
BESS = [("BRB0331", "RUK0", "MERI", 100, date(2025, 5, 28)), ("GLN0332", "GLB0", "CTCT", 100, date(2026, 1, 20))]
LOADU = {"RUK0": "RUK99", "GLB0": "GLB99"}


def demand_shape(tp):
    h = (tp - 0.5) / 2
    return 1250 + 420 * np.exp(-((h - 8) / 1.6) ** 2) + 600 * np.exp(-((h - 18.5) / 1.8) ** 2) - 250 * np.exp(-((h - 3.5) / 2.5) ** 2)


def ts(d, tp):
    return (pd.Timestamp(d).tz_localize("Pacific/Auckland") + pd.Timedelta(minutes=30 * (tp - 1))).isoformat()


def write(path, df):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)


def day(base, d):
    s = d.strftime("%Y%m%d")
    tps = np.arange(1, 49)
    load = demand_shape(tps) * (1 + 0.04 * rng.standard_normal()) + 40 * rng.standard_normal(48)
    co, offers, ep, rp, nv = [], [], [], [], []
    for tp, L in zip(tps, load):
        h = (tp - 0.5) / 2
        # BESS schedule: charge 01:00-05:00 and 11:00-14:00, discharge peaks; reserves when idle
        bess_mw = {}
        for poc, unit, tr, mw, com in BESS:
            if d < com:
                continue
            if 7 <= h < 9 or 17.5 <= h < 20.5:
                bess_mw[poc] = mw * rng.uniform(0.6, 1.0)
            elif 1 <= h < 5 or 11 <= h < 14:
                bess_mw[poc] = -mw * rng.uniform(0.4, 0.8)
            else:
                bess_mw[poc] = 0.0
        resid = L + sum(-v for v in bess_mw.values() if v < 0) - sum(v for v in bess_mw.values() if v > 0)
        # clear NI stack
        stack = sorted([(p, u, tr, f, mw, pr) for p, u, tr, isl, f, tr_ in UNITS if isl == "NI" for (mw, pr) in tr_ for tr in [tr]],
                       key=lambda r: r[5])
        need, price, clr = resid, 0.0, {}
        for p, u, tr, f, mw, pr in stack:
            take = min(mw, max(need, 0))
            if take > 0:
                clr[(p, u, tr)] = clr.get((p, u, tr), 0) + take
                price = pr
            need -= take
        price = price if need <= 0 else 5000
        price *= rng.uniform(0.95, 1.05)
        for (p, u, tr), mw in clr.items():
            co.append([d.isoformat(), tp, ts(d, tp), "Injection", p, u, "North Island", tr, round(mw, 2), None, None])
        for p, u, tr, isl, f, trs in UNITS:
            for i, (mw, pr) in enumerate(trs, 1):
                offers.append([d.isoformat(), tp, p, u, tr, "Energy", "Injection", "", i, mw, pr, "Y"])
        for (poc, unit, tr, mw, com) in BESS:
            if poc not in bess_mw:
                continue
            v = bess_mw[poc]
            if v > 0:
                co.append([d.isoformat(), tp, ts(d, tp), "Injection", poc, unit, "North Island", tr, round(v, 2), None, None])
            elif v == 0:
                co.append([d.isoformat(), tp, ts(d, tp), "Injection", poc, unit, "North Island", tr, None, 40.0, 30.0])
            else:  # charging: interruptible load reserve from the offtake unit
                co.append([d.isoformat(), tp, ts(d, tp), "Offtake", poc, LOADU[unit], "North Island", tr, None, round(-v * 0.5, 2), round(-v * 0.4, 2)])
            nv.append([d.isoformat(), tp, ts(d, tp), poc, LOADU[unit], round(max(-v, 0), 2), round(max(v, 0) * 0, 2), price])
            nv.append([d.isoformat(), tp, ts(d, tp), poc, unit, 0.0, round(max(v, 0), 2), price])
            offers.append([d.isoformat(), tp, poc, unit, tr, "Energy", "Injection", "", 1, mw, 150, "Y"])
        for poc in ("OTA2201", "BRB0331", "GLN0332", "HLY2201"):
            ep.append([d.isoformat(), tp, poc, round(price * (1.03 if poc == "BRB0331" else 1.0), 2)])
        ep.append([d.isoformat(), tp, "BEN2201", round(price * 0.9, 2)])
        for isl in ("NI", "SI"):
            rp.append([d.isoformat(), tp, isl, round(rng.uniform(0.5, 15), 2), round(rng.uniform(0.2, 6), 2)])
        nv.append([d.isoformat(), tp, ts(d, tp), "OTA2201", "", round(L * 0.3, 1), 0, price])
        nv.append([d.isoformat(), tp, ts(d, tp), "GLN0332", "", 41.0, 0, price])   # steel load at shared POC
    y = d.year
    write(f"{base}/{R}/DispatchAndPricing/ClearedOffers/{y}/{s}_ClearedOffers.csv",
          pd.DataFrame(co, columns=["TradingDate", "TradingPeriodNumber", "IntervalDateTime", "ProductClass", "PointOfConnectionCode",
                                    "UnitCode", "Island", "ParticipantCode", "ClearedGenerationMegawatts", "ClearedFIRMegawatts", "ClearedSIRMegawatts"]))
    write(f"{base}/{R}/BidsAndOffers/Offers/{y}/{s}_Offers.csv",
          pd.DataFrame(offers, columns=["TradingDate", "TradingPeriod", "PointOfConnection", "Unit", "ParticipantCode",
                                        "ProductType", "ProductClass", "ReserveType", "Tranche", "Megawatts", "DollarsPerMegawattHour", "IsLatestYesNo"]))
    write(f"{base}/{R}/DispatchAndPricing/FinalEnergyPrices/{s}_FinalEnergyPrices.csv",
          pd.DataFrame(ep, columns=["TradingDate", "TradingPeriod", "PointOfConnection", "DollarsPerMegawattHour"]))
    write(f"{base}/{R}/DispatchAndPricing/FinalReservePrices/{s}_FinalReservePrices.csv",
          pd.DataFrame(rp, columns=["TradingDate", "TradingPeriod", "Island", "FIRDollarsPerMegawattHour", "SIRDollarsPerMegawattHour"]))
    write(f"{base}/{R}/DispatchAndPricing/NodalPricesAndVolumes/{y}/{s}_DispatchNodalPricesAndVolumes.csv",
          pd.DataFrame(nv, columns=["TradingDate", "TradingPeriodNumber", "IntervalDateTime", "PointOfConnectionCode", "UnitCode",
                                    "LoadMegawatts", "GenerationMegawatts", "DollarsPerMegawattHour"]))


if __name__ == "__main__":
    base, a, b = sys.argv[1], date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3])
    fleet = [[n, p, u, f[:3].upper(), f, f, sum(m for m, _ in t), None] for p, u, tr, isl, f, t in UNITS for n in [p[:3]]]
    fleet += [["Ruakaka Energy Park", "BRB0331", "RUK0", "BAT", "Chemical battery energy", "Electrical energy", 100, None],
              ["Glenbrook-Ohurua Battery", "GLN0332", "GLB0", "BAT", "Chemical battery energy", "Electrical energy", 100, None],
              ["Huntly Battery", "HLY2201", "HLY11", "BAT", "Chemical battery energy", "Electrical energy", 100, None],
              ["Southdown Battery", "SWN2201", "SWN1", "BAT", "Chemical battery energy", "Electrical energy", 1, "23/05/2022"],
              ["New Battery", "NEW0331", "NEW0", "BAT", "Chemical battery energy", "Electrical energy", 50, None]]
    write(f"{base}/{R}/Generation/GenerationFleet/Existing/20260702_DispatchedGenerationPlant.csv",
          pd.DataFrame(fleet, columns=["PlantName", "PointOfConnectionCode", "UnitCode", "TechnologyCode", "Technology", "Fuel",
                                       "NameplateMegawatts", "DateDecommissioned"]))
    d = a
    while d <= b:
        day(base, d)
        d += timedelta(days=1)
    print("ok")
