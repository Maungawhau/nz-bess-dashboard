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
]
BESS = [("BRB0331", "BRB", "MERI", 100, date(2025, 5, 28)), ("GLN0332", "GLN", "CTCT", 100, date(2026, 1, 20))]


def demand_shape(tp):
    h = (tp - 0.5) / 2
    return 1250 + 420 * np.exp(-((h - 8) / 1.6) ** 2) + 600 * np.exp(-((h - 18.5) / 1.8) ** 2) - 250 * np.exp(-((h - 3.5) / 2.5) ** 2)


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
            co.append([d.isoformat(), tp, "NI", p, u, tr, "Energy", "", round(mw, 2)])
        for p, u, tr, isl, f, trs in UNITS:
            for i, (mw, pr) in enumerate(trs, 1):
                offers.append([d.isoformat(), tp, p, u, tr, "Energy", "", i, mw, pr, "Y"])
        for (poc, unit, tr, mw, com) in BESS:
            if poc not in bess_mw:
                continue
            v = bess_mw[poc]
            if v > 0:
                co.append([d.isoformat(), tp, "NI", poc, unit, tr, "Energy", "", round(v, 2)])
            else:
                co.append([d.isoformat(), tp, "NI", poc, unit, tr, "Reserve", "FIR", round(min(60, mw + v), 2)])
                co.append([d.isoformat(), tp, "NI", poc, unit, tr, "Reserve", "SIR", round(min(40, mw + v), 2)])
            nv.append([d.isoformat(), tp, poc, round(max(-v, 0), 2), round(max(v, 0), 2), price])
            offers.append([d.isoformat(), tp, poc, unit, tr, "Energy", "", 1, mw, 150, "Y"])
        for poc in ("OTA2201", "BRB0331", "GLN0332", "HLY2201"):
            ep.append([d.isoformat(), tp, poc, round(price * (1.03 if poc == "BRB0331" else 1.0), 2)])
        ep.append([d.isoformat(), tp, "BEN2201", round(price * 0.9, 2)])
        for isl in ("NI", "SI"):
            rp.append([d.isoformat(), tp, isl, "FIR", round(rng.uniform(0.5, 15), 2)])
            rp.append([d.isoformat(), tp, isl, "SIR", round(rng.uniform(0.2, 6), 2)])
        nv.append([d.isoformat(), tp, "OTA2201", round(L * 0.3, 1), 0, price])
    y = d.year
    write(f"{base}/{R}/DispatchAndPricing/ClearedOffers/{y}/{s}_ClearedOffers.csv",
          pd.DataFrame(co, columns=["TradingDate", "TradingPeriod", "Island", "PointOfConnection", "Unit", "Trader",
                                    "ProductType", "ReserveType", "ClearedMW"]))
    write(f"{base}/{R}/BidsAndOffers/Offers/{y}/{s}_Offers.csv",
          pd.DataFrame(offers, columns=["TradingDate", "TradingPeriod", "PointOfConnection", "Unit", "ParticipantCode",
                                        "ProductType", "ReserveType", "Tranche", "Megawatts", "DollarsPerMegawattHour", "IsLatestYesNo"]))
    write(f"{base}/{R}/DispatchAndPricing/FinalEnergyPrices/{s}_FinalEnergyPrices.csv",
          pd.DataFrame(ep, columns=["TradingDate", "TradingPeriod", "PointOfConnection", "DollarsPerMegawattHour"]))
    write(f"{base}/{R}/DispatchAndPricing/FinalReservePrices/{s}_FinalReservePrices.csv",
          pd.DataFrame(rp, columns=["TradingDate", "TradingPeriod", "Island", "ReserveType", "DollarsPerMegawattHour"]))
    write(f"{base}/{R}/DispatchAndPricing/NodalPricesAndVolumes/{y}/{s}_DispatchNodalPricesAndVolumes.csv",
          pd.DataFrame(nv, columns=["TradingDate", "TradingPeriod", "PointOfConnection", "LoadMW", "GenerationMW", "DollarsPerMegawattHour"]))


if __name__ == "__main__":
    base, a, b = sys.argv[1], date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3])
    fleet = [[p, u, f, f, n, sum(m for m, _ in t)] for p, u, tr, isl, f, t in UNITS for n in [p[:3]]]
    fleet += [["BRB0331", "BRB", "ELE", "BAT", "Ruakaka BESS", 100], ["GLN0332", "GLN", "ELE", "BAT", "Glenbrook BESS", 100],
              ["HLY2202", "HLYB", "ELE", "BAT", "Huntly BESS", 100]]
    write(f"{base}/{R}/Generation/GenerationFleet/Existing/20261001_DispatchedGenerationPlant.csv",
          pd.DataFrame(fleet, columns=["PointOfConnection", "Unit", "FuelType", "Technology", "StationName", "NameplateCapacityMW"]))
    d = a
    while d <= b:
        day(base, d)
        d += timedelta(days=1)
    print("ok")
