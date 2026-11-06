"""Print extracted text of stored notices (debug aid). Usage: python tools/print_notices.py <kind> [approx_only]"""
import sys, pandas as pd
sys.path.insert(0, ".")
from pipeline.events import fetch_text
kind = sys.argv[1] if len(sys.argv) > 1 else "hvdc"
s = pd.read_csv("data/events/notices.csv", dtype=str).fillna("")
s = s[(s["kind"] == kind) & (s["approx"] == "True")]
seen = set()
for _, n in s.iterrows():
    k = n["title"].split(" 8000")[0]
    if k in seen or len(seen) >= 5:
        continue
    seen.add(k)
    print(f"\n===== {n['ref']} {n['title']}\n{fetch_text(n['url'])[:1400]}")
