"""System events from Transpower (System Operator) notices, plus NI/SI price separation.

Sources (public, no login):
  - Customer Advice Notices (CAN):  low residual situations, HVDC outages / reduced capability,
                                    under-frequency event confirmations
  - Excursion Notices (EXN):        frequency excursions and generation trips, with exact times
Both pages list notices as PDF links. There is no API, so we read the list page, download notices
we haven't seen before (cached in data/events/notices.csv) and extract the affected times.

Island price separation comes from EMI final energy prices (OTA2201 vs BEN2201).

Everything here is used to TAG half-hours. A tag means "this was going on at the time",
not "this caused the price or the revenue".
"""
from __future__ import annotations

import io
import json
import os
import re
from datetime import date, datetime, timedelta

import pandas as pd
import requests

UA = {"User-Agent": "nz-bess-dashboard (github.com/Maungawhau/nz-bess-dashboard)"}
PAGES = {
    "CAN": "https://www.transpower.co.nz/system-operator/notices-and-reporting/customer-advice-notices-can",
    "EXN": "https://www.transpower.co.nz/system-operator/notices-and-reporting/excursion-notices",
}
STORE = "data/events/notices.csv"
PARSER_VERSION = "4"          # bump to re-read every stored notice after a parser change
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def classify(title: str) -> str | None:
    t = title.lower()
    if t.startswith("exn"):
        if "frequency" in t:
            return "freq"
        if "generation tripped" in t or "hvdc" in t or "pole" in t:
            return "trip"
        return None                                 # voltage dips from line auto-reclose etc: ignore
    if "residual" in t or "short fall" in t or "shortfall" in t:
        return "lrs_early" if ("potential" in t or "short" in t) else "lrs"
    if "under-frequency" in t or "under frequency" in t:
        return "ufe"
    if "hvdc" in t and ("outage" in t or "reduced" in t or "capability" in t):
        return "hvdc"
    if "grid emergency" in t or "warning notice" in t:
        return "lrs"
    return None


LABEL = {"lrs": "Low residual CAN", "lrs_early": "Potential low residual (week-ahead)", "hvdc": "HVDC outage / reduced capability",
         "ufe": "Under-frequency event", "freq": "Frequency excursion", "trip": "Generation trip"}

# ---------------------------------------------------------------------------
# list pages
# ---------------------------------------------------------------------------

ROW = re.compile(r'<a href="(https://static\.transpower\.co\.nz/[^"]+\.pdf[^"]*)"[^>]*>.*?<span class="description">(.*?)</span>', re.S)


def list_notices() -> list[dict]:
    out = []
    for src, url in PAGES.items():
        r = requests.get(url, headers=UA, timeout=60)
        r.raise_for_status()
        for link, desc in ROW.findall(r.text):
            desc = re.sub(r"\s+", " ", desc).strip()
            m = re.search(r"(\d{6,})\s*-?\s*$", desc)
            ref = m.group(1) if m else re.sub(r"\W+", "", link.split("/")[-1].split("?")[0])[-20:]
            kind = classify(desc)
            if kind:
                out.append({"source": src, "ref": ref, "title": desc, "url": link, "kind": kind})
    if not out:
        raise RuntimeError("no notices parsed - Transpower page layout may have changed")
    return out

# ---------------------------------------------------------------------------
# PDF text -> windows
# ---------------------------------------------------------------------------

def _yr(y: str) -> int:
    y = int(y)
    return y + 2000 if y < 100 else y


def _dt(d: int, mon: str, y, hm: str) -> datetime:
    h, mi = map(int, hm.split(":"))
    return datetime(_yr(y), MONTHS[mon[:3].lower()], d, h % 24, mi)


RX_DAYRANGE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?\s+(\d{4}),?\s*(?:from\s*)?(\d{1,2}:\d{2})\s*(?:-|–|to)\s*(\d{1,2}:\d{2})")
RX_SPAN = re.compile(r"(\d{1,2}:\d{2})\s+(\d{1,2})/(\d{1,2})/(\d{2,4})\s+(?:to|-|–)\s+(\d{1,2}:\d{2})\s+(\d{1,2})/(\d{1,2})/(\d{2,4})")
RX_SPAN2 = re.compile(r"(\d{1,2})[-\s]([A-Za-z]{3,9})[-\s](\d{2,4})\s+(\d{1,2}:\d{2})\s*(?:to|-|–)\s*(\d{1,2})[-\s]([A-Za-z]{3,9})[-\s](\d{2,4})\s+(\d{1,2}:\d{2})")
RX_TIME = re.compile(r"Time:\s*(\d{1,2})\s*-\s*([A-Za-z]{3,9})\s*-\s*(\d{4})\s+(\d{1,2}:\d{2})")
RX_SENT = re.compile(r"Sent:\s*(\d{1,2})-([A-Za-z]{3,9})-(\d{4})\s+(\d{1,2}:\d{2})")
RX_OCCURRED = re.compile(r"occurred.{0,80}?on\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\s+(\d{4})", re.S)
RX_REVISION = re.compile(r"Revision of:\s*CAN,?\s*(\d{6,})")
RX_REF = re.compile(r"Ref:\s*(\d{6,})")
RX_STARTEND = re.compile(r"Starting:\s*(\d{1,2})-([A-Za-z]{3,9})-(\d{4})\s+(\d{1,2}:\d{2}).{0,40}?Ending:\s*(\d{1,2})-([A-Za-z]{3,9})-(\d{4})\s+(\d{1,2}:\d{2})", re.S)
RX_UNTIL = re.compile(r"until\s+(\d{1,2}:\d{2})[^,.]{0,20},?\s*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\s+(\d{4})")


def parse_text(text: str, kind: str) -> dict:
    """Extract sent time, island, windows [(start, end)], superseded ref and cancellation."""
    t = re.sub(r"[ \t\u00a0\u2009\u202f]+", " ", text)
    info = {"sent": None, "island": "", "windows": [], "approx": False, "revises": "", "cancelled": False, "doc_ref": ""}
    if m := RX_REF.search(t):
        info["doc_ref"] = m.group(1)
    if m := RX_SENT.search(t):
        info["sent"] = _dt(int(m.group(1)), m.group(2), m.group(3), m.group(4))
    if m := RX_REVISION.search(t):
        info["revises"] = m.group(1)
    low = re.sub(r"\s+", " ", t.lower())
    info["cancelled"] = bool(re.search(r"\bcancel(l)?ed\b|has been resolved|no longer (a|an) ", low))
    m = re.search(r"advises? that (?:an? |the )?(north island|south island|national|nz)", low) or \
        re.search(r"(north and south islands?|north island|south island)", low)
    if m:
        w = m.group(1)
        info["island"] = "NZ" if ("national" in w or "nz" == w or " and " in w) else "NI" if w.startswith("north") else "SI"

    wins = []
    if kind in ("freq", "trip"):
        if m := RX_TIME.search(re.sub(r"\s+", " ", t)):
            s = _dt(int(m.group(1)), m.group(2), m.group(3), m.group(4))
            wins.append((s, s + timedelta(minutes=1)))
        elif info["sent"]:                          # no readable event time: excursion notices go out within minutes
            wins.append((info["sent"] - timedelta(minutes=5), info["sent"] - timedelta(minutes=4)))
            info["approx"] = True
    elif (m := RX_STARTEND.search(t)) and m.group(2)[:3].lower() in MONTHS and m.group(6)[:3].lower() in MONTHS:
        # outage notices: one overall window
        wins.append((_dt(int(m.group(1)), m.group(2), m.group(3), m.group(4)), _dt(int(m.group(5)), m.group(6), m.group(7), m.group(8))))
    else:
        if kind == "hvdc" and info["sent"] and (m := RX_UNTIL.search(t)) and m.group(3)[:3].lower() in MONTHS:
            wins.append((info["sent"], _dt(int(m.group(2)), m.group(3), m.group(4), m.group(1))))
        for m in RX_DAYRANGE.finditer(t):
            if m.group(2)[:3].lower() not in MONTHS:
                continue
            s = _dt(int(m.group(1)), m.group(2), m.group(3), m.group(4))
            e = _dt(int(m.group(1)), m.group(2), m.group(3), m.group(5))
            wins.append((s, e if e > s else e + timedelta(days=1)))
        for m in RX_SPAN.finditer(t):
            s = datetime(_yr(m.group(4)), int(m.group(3)), int(m.group(2)), *map(int, m.group(1).split(":")))
            e = datetime(_yr(m.group(8)), int(m.group(7)), int(m.group(6)), *map(int, m.group(5).split(":")))
            wins.append((s, e))
        for m in RX_SPAN2.finditer(t):
            if m.group(2)[:3].lower() in MONTHS and m.group(6)[:3].lower() in MONTHS:
                wins.append((_dt(int(m.group(1)), m.group(2), m.group(3), m.group(4)), _dt(int(m.group(5)), m.group(6), m.group(7), m.group(8))))
        if kind == "ufe" and (m := RX_OCCURRED.search(t)) and m.group(2)[:3].lower() in MONTHS:
            d0 = _dt(int(m.group(1)), m.group(2), m.group(3), "00:00")
            wins.append((d0, d0 + timedelta(days=1)))
            info["approx"] = True                    # date only; the excursion notice has the exact time
        if not wins and kind == "hvdc" and info["sent"]:
            wins.append((info["sent"], info["sent"] + timedelta(hours=6)))   # unplanned outage with no stated end
            info["approx"] = True
    info["windows"] = sorted(set(w for w in wins if w[1] > w[0] and (w[1] - w[0]) <= timedelta(days=21)))
    return info


def fetch_text(url: str) -> str:
    from pypdf import PdfReader
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages)


def update_notices(warnings: list[str]) -> pd.DataFrame:
    """Add notices we haven't seen before to the store. Network failures keep the existing store."""
    store = pd.read_csv(STORE, dtype=str).fillna("") if os.path.exists(STORE) else pd.DataFrame()
    if not store.empty and ("v" not in store or (store["v"] != PARSER_VERSION).any()):
        store = store[store.get("v", "") == PARSER_VERSION] if "v" in store else pd.DataFrame()   # re-read with new parser
    known = set(store["ref"]) if not store.empty else set()
    try:
        listed = list_notices()
    except Exception as e:  # keep yesterday's events rather than failing the whole run
        warnings.append(f"Transpower notices not refreshed: {type(e).__name__}: {e}")
        return store
    new = []
    for n in listed:
        if n["ref"] in known:
            continue
        try:
            info = parse_text(fetch_text(n["url"]), n["kind"])
        except Exception as e:
            warnings.append(f"notice {n['ref']} unreadable: {e}")
            continue
        if not info["windows"] and n["kind"] not in ("ufe",):
            warnings.append(f"notice {n['ref']} ({n['title']}): no affected time found")
        new.append({**n, "sent": info["sent"].isoformat() if info["sent"] else "", "island": info["island"],
                    "windows": json.dumps([[a.isoformat(), b.isoformat()] for a, b in info["windows"]]),
                    "approx": str(info["approx"]), "revises": info["revises"], "cancelled": str(info["cancelled"]),
                    "doc_ref": info["doc_ref"], "v": PARSER_VERSION})
    if new:
        store = pd.concat([store, pd.DataFrame(new)], ignore_index=True)
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        store.sort_values("ref").to_csv(STORE, index=False)
        print(f"  {len(new)} new Transpower notice(s)")
    return store


def active_events(store: pd.DataFrame, start: date) -> list[dict]:
    """One record per window, dropping notices that were later revised or cancelled."""
    if store.empty:
        return []
    revised = set(store["revises"].replace("", None).dropna())
    out = []
    for _, n in store.iterrows():
        if n["ref"] in revised or (n.get("doc_ref") or "-") in revised or n.get("cancelled") == "True":
            continue
        for a, b in json.loads(n["windows"] or "[]"):
            if b[:10] < str(start):
                continue
            out.append({"kind": n["kind"], "label": LABEL.get(n["kind"], n["kind"]), "title": re.sub(r"^(CAN|EXN)\s*-?\s*|\s*\d{6,}\s*-?$", "", n["title"]),
                        "start": a[:16], "end": b[:16], "island": n["island"], "approx": n["approx"] == "True", "url": n["url"], "ref": n["ref"]})
    return sorted(out, key=lambda e: e["start"])

# ---------------------------------------------------------------------------
# half-hour tagging
# ---------------------------------------------------------------------------

def tp_bounds(d: str, tp: int) -> tuple[datetime, datetime]:
    s = datetime.fromisoformat(d) + timedelta(minutes=30 * (tp - 1))
    return s, s + timedelta(minutes=30)


def tag_frame(keys: pd.DataFrame, events: list[dict], market: pd.DataFrame, settings: dict) -> pd.DataFrame:
    """keys: date (str), tp. Returns keys + boolean columns lrs, hvdc, event (freq/trip/ufe), sep."""
    out = keys[["date", "tp"]].drop_duplicates().copy()
    out["date"] = out["date"].astype(str)
    start = pd.to_datetime(out["date"]) + pd.to_timedelta((out["tp"] - 1) * 30, unit="m")
    end = start + pd.Timedelta(minutes=30)
    for col, kinds in (("lrs", {"lrs"}), ("hvdc", {"hvdc"}), ("event", {"freq", "trip", "ufe"})):
        flag = pd.Series(False, index=out.index)
        for e in events:
            if e["kind"] in kinds and not (e["kind"] == "ufe" and e["approx"]):
                a, b = pd.Timestamp(e["start"]), pd.Timestamp(e["end"])
                flag |= (start < b) & (end > a)
        out[col] = flag
    sep = settings.get("price_separation", {"min_gap": 50, "min_ratio": 1.5})
    if not market.empty:
        m = market.assign(date=market["date"].astype(str))[["date", "tp", "ni_price", "si_price"]]
        out = out.merge(m, on=["date", "tp"], how="left")
        gap = (out["ni_price"] - out["si_price"]).abs()
        lo = out[["ni_price", "si_price"]].min(axis=1).clip(lower=1)
        hi = out[["ni_price", "si_price"]].max(axis=1)
        out["sep"] = (gap >= sep["min_gap"]) & (hi / lo >= sep["min_ratio"])
        out = out.drop(columns=["ni_price", "si_price"])
    else:
        out["sep"] = False
    return out


def market_day(ep: pd.DataFrame | None, rp: pd.DataFrame | None, d: date, settings: dict) -> pd.DataFrame:
    """NI/SI reference prices and reserve prices per TP for one day."""
    if ep is None:
        return pd.DataFrame()
    ref = settings["reference_nodes"]
    ni = ep[ep["poc"] == ref["NI"]].set_index("tp")["price"].rename("ni_price")
    si = ep[ep["poc"] == ref["SI"]].set_index("tp")["price"].rename("si_price")
    m = pd.concat([ni, si], axis=1).reset_index()
    if rp is not None:
        for isl in ("NI", "SI"):
            r = rp[rp["island"] == isl].set_index("tp")[["fir_price", "sir_price"]].add_prefix(f"{isl.lower()}_")
            m = m.merge(r, left_on="tp", right_index=True, how="left")
    m["date"] = str(d)
    return m
