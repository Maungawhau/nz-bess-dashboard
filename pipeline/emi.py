"""EMI dataset access.

EMI wholesale datasets are public CSV files served from the Azure blob
container `emidatasets/publicdata`. No API key is needed. File locations
are discovered with the public blob list API (prefix search), so folder
re-organisations on EMI's side (daily -> by-year -> by-month) don't break
the pipeline. If listing is unavailable we fall back to known path patterns.

Column names have changed over the years, so every dataset is read through
a resolver that maps several candidate header spellings onto one canonical
name. The probe workflow prints the real headers so the candidates can be
tightened.
"""
from __future__ import annotations

import io
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date
from functools import lru_cache

import pandas as pd
import requests

BLOB = "https://emidatasets.blob.core.windows.net/publicdata"
WEB = "https://www.emi.ea.govt.nz"
ROOT = "Datasets/Wholesale"

CACHE_DIR = os.environ.get("EMI_CACHE", ".cache/emi")
LOCAL_DIR = os.environ.get("EMI_LOCAL_DIR")  # tests: read files from a folder instead of the web

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "nz-bess-dashboard (github actions)"

# dataset key -> (folder under Datasets/Wholesale, filename suffix)
DATASETS = {
    "cleared_offers": ("DispatchAndPricing/ClearedOffers", "_ClearedOffers.csv"),
    "energy_prices": ("DispatchAndPricing/FinalEnergyPrices", "_FinalEnergyPrices.csv"),
    "reserve_prices": ("DispatchAndPricing/FinalReservePrices", "_FinalReservePrices.csv"),
    "nodal_volumes": ("DispatchAndPricing/NodalPricesAndVolumes", "_DispatchNodalPricesAndVolumes.csv"),
    "offers": ("BidsAndOffers/Offers", "_Offers.csv"),
    "fleet": ("Generation/GenerationFleet/Existing", "_DispatchedGenerationPlant.csv"),
}

# canonical column -> accepted header spellings (compared after lower-casing and stripping non-alphanumerics)
COLUMNS = {
    "date": ["TradingDate", "Date", "Trading_date"],
    "tp": ["TradingPeriod", "Trading_period", "TP", "TradingPeriodNumber"],
    "interval": ["DispatchInterval", "IntervalDateTime", "DispatchIntervalDateTime", "IntervalStartTime",
                 "RunDateTime", "CaseTime", "DateTime", "PublishDateTime"],
    "island": ["Island", "IslandCode"],
    "poc": ["PointOfConnection", "PointOfConnectionCode", "PNode", "Node", "POC", "NodeCode", "GridPointOfConnection"],
    "unit": ["Unit", "UnitCode", "GenerationUnit"],
    "trader": ["Trader", "ParticipantCode", "PlantOperatorCode", "TraderCode", "Participant"],
    "product": ["ProductType", "Type", "OfferType", "ClearedType", "Product"],
    "reserve_type": ["ReserveType", "ProductClass", "ReserveClass", "ProductDescription"],
    "mw": ["ClearedMW", "ClearedMegawatts", "Megawatts", "MW", "Cleared", "ClearedQuantity"],
    "mw_energy": ["ClearedGenerationMegawatts", "ClearedEnergy", "ClearedEnergyMW", "ClearedEnergyMegawatts", "Energy"],
    "mw_fir": ["ClearedFIRMegawatts", "ClearedFIR", "ClearedFIRMW", "FIR"],
    "mw_sir": ["ClearedSIRMegawatts", "ClearedSIR", "ClearedSIRMW", "SIR"],
    "flow": ["ProductClass", "FlowDirection", "Direction"],
    "fir_price": ["FIRDollarsPerMegawattHour", "FIRPrice"],
    "sir_price": ["SIRDollarsPerMegawattHour", "SIRPrice"],
    "price": ["DollarsPerMegawattHour", "Price", "PriceDollarsPerMegawattHour", "DollarsPerMWh", "Dollars"],
    "load_mw": ["LoadMegawatts", "LoadMW", "Load", "LoadMegawatts", "DispatchedLoad"],
    "gen_mw": ["GenerationMegawatts", "GenerationMW", "Generation", "GenerationMegawatts", "DispatchedGeneration"],
    "max_output": ["MaximumOutputMegawatts"],
    "forecast": ["ForecastOfGenerationPotentialMegawatts"],
    "tranche": ["Tranche", "Band", "TrancheNumber"],
    "is_latest": ["IsLatestYesNo", "IsLatest"],
    "fuel": ["FuelType", "Fuel", "FuelCode", "Fuel_Code", "FuelCode", "PrimaryFuel"],
    "tech": ["TechnologyCode", "Technology", "Tech_Code", "TechCode", "GenerationType", "PlantType"],
    "name": ["PlantName", "StationName", "Name", "PlantName", "Station", "GeneratingPlantName"],
    "decommissioned": ["DateDecommissioned"],
    "capacity": ["NameplateMegawatts", "NameplateCapacityMW", "Capacity", "MaxOutputMW", "NameplateMW", "CapacityMW"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def resolve(df: pd.DataFrame, wanted: list[str], dataset: str = "") -> pd.DataFrame:
    """Rename df columns to canonical names. Missing optional columns are simply absent."""
    lookup = {_norm(c): c for c in df.columns}
    rename = {}
    for canon in wanted:
        for cand in COLUMNS[canon]:
            if _norm(cand) in lookup and lookup[_norm(cand)] not in rename:
                rename[lookup[_norm(cand)]] = canon
                break
    out = df.rename(columns=rename)
    return out


def require(df: pd.DataFrame, cols: list[str], dataset: str):
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise KeyError(f"{dataset}: could not find columns {missing}. Headers were: {list(df.columns)[:40]}. "
                       f"Add the right spelling to pipeline/emi.py COLUMNS.")


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _get(url: str, **kw) -> requests.Response:
    for attempt in range(4):
        try:
            r = SESSION.get(url, timeout=120, **kw)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return r
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)
    raise RuntimeError(f"failed to fetch {url}")


@lru_cache(maxsize=64)
def list_blobs(prefix: str) -> tuple[str, ...]:
    """All blob names under prefix (public container list API)."""
    if LOCAL_DIR:
        base = os.path.join(LOCAL_DIR, prefix)
        out = []
        for root, _, files in os.walk(base):
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), LOCAL_DIR).replace(os.sep, "/"))
        return tuple(sorted(out))
    names, marker = [], ""
    while True:
        url = f"{BLOB}?restype=container&comp=list&prefix={prefix}&maxresults=5000" + (f"&marker={marker}" if marker else "")
        r = _get(url)
        if r.status_code != 200:
            return tuple()
        root = ET.fromstring(r.content)
        names += [b.findtext("Name") for b in root.iter("Blob")]
        marker = root.findtext("NextMarker") or ""
        if not marker:
            break
    return tuple(names)


INTERIM_USED: set = set()     # (key, date) pairs read from an interim (_I) file in this run


def find_file(key: str, d: date) -> str | None:
    """Blob path of the daily file for dataset `key` and trading date d (any sub-folder).

    Price files are first published as interim (`..._I.csv`) and renamed when declared final
    (usually within 2 business days). Final is preferred; interim is used when that's all there is.
    """
    folder, suffix = DATASETS[key]
    stamp = d.strftime("%Y%m%d")
    for want, interim in ((suffix, False), (suffix.replace(".csv", "_I.csv"), True)):
        for prefix in (f"{ROOT}/{folder}/{d.year}/", f"{ROOT}/{folder}/"):
            for n in list_blobs(prefix):
                fn = n.rsplit("/", 1)[-1]
                if fn.startswith(stamp) and fn.endswith(want):
                    if interim:
                        INTERIM_USED.add((key, d))
                    else:
                        INTERIM_USED.discard((key, d))
                    return n
    # listing failed / empty -> try the conventional locations directly
    for cand in (f"{ROOT}/{folder}/{d.year}/{stamp}{suffix}", f"{ROOT}/{folder}/{stamp}{suffix}"):
        if _exists(cand):
            return cand
    return None


def latest_file(key: str) -> str | None:
    folder, suffix = DATASETS[key]
    names = [n for n in list_blobs(f"{ROOT}/{folder}/") if n.endswith(suffix)]
    return sorted(names, key=lambda n: n.rsplit("/", 1)[-1])[-1] if names else None


def _exists(path: str) -> bool:
    if LOCAL_DIR:
        return os.path.exists(os.path.join(LOCAL_DIR, path))
    try:
        return SESSION.head(f"{BLOB}/{path}", timeout=30).status_code == 200
    except requests.RequestException:
        return False


def usecols_for(wanted: list[str]):
    keep = {_norm(c) for w in wanted for c in COLUMNS[w]}
    return lambda col: _norm(col) in keep


def read_csv(path: str, **kw) -> pd.DataFrame:
    """Read an EMI CSV (cached on disk for the duration of a run)."""
    if LOCAL_DIR:
        return pd.read_csv(os.path.join(LOCAL_DIR, path), low_memory=False, **kw)
    local = os.path.join(CACHE_DIR, path)
    if not os.path.exists(local):
        os.makedirs(os.path.dirname(local), exist_ok=True)
        r = _get(f"{BLOB}/{path}")
        if r.status_code != 200:
            r = _get(f"{WEB}/{path.replace('Datasets/Wholesale', 'Wholesale/Datasets')}")
        r.raise_for_status()
        with open(local, "wb") as f:
            f.write(r.content)
    return pd.read_csv(local, low_memory=False, **kw)


def load(key: str, d: date, wanted: list[str]) -> pd.DataFrame | None:
    path = find_file(key, d)
    if path is None:
        return None
    df = resolve(read_csv(path, usecols=usecols_for(wanted)), wanted, key)
    if "date" in df.columns:
        df["date"] = parse_dates(df["date"])
    if "tp" in df.columns:
        df["tp"] = pd.to_numeric(df["tp"], errors="coerce").astype("Int64")
    for c in ("mw", "mw_energy", "mw_fir", "mw_sir", "price", "load_mw", "gen_mw", "capacity", "max_output", "forecast"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ("poc", "unit", "trader", "island", "product", "reserve_type", "flow", "name"):
        if c in df.columns:
            df[c] = df[c].fillna("").astype(str).str.strip()
    if "island" in df.columns:  # "North Island" / "NI" / "N" -> NI|SI
        u = df["island"].str.upper()
        df["island"] = u.where(~u.str.startswith(("N", "S")), u.str[0] + "I")
    return df


def parse_dates(s: pd.Series) -> pd.Series:
    """EMI uses both ISO (2026-01-31) and NZ (31/01/2026) date formats."""
    s = s.astype(str).str.slice(0, 10)
    iso = s.str.match(r"^\d{4}-\d{2}-\d{2}")
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    out[iso] = pd.to_datetime(s[iso], format="%Y-%m-%d", errors="coerce")
    out[~iso] = pd.to_datetime(s[~iso], dayfirst=True, errors="coerce")
    return out.dt.date


def interval_to_tp(ts: pd.Series, d: date) -> pd.Series:
    """Trading period from a dispatch-interval timestamp (NZ local), DST-safe."""
    t = pd.to_datetime(ts, errors="coerce")
    if t.dt.tz is None:
        t = t.dt.tz_localize("Pacific/Auckland", ambiguous="NaT", nonexistent="shift_forward")
    midnight = pd.Timestamp(d).tz_localize("Pacific/Auckland")
    mins = (t - midnight).dt.total_seconds() / 60
    return (mins // 30 + 1).astype("Int64")


def clear_cache():
    import shutil
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
