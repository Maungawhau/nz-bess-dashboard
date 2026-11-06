"""Print the extracted text of stored notices whose times could not be read (debug aid)."""
import sys, pandas as pd
sys.path.insert(0, ".")
from pipeline.events import fetch_text
s = pd.read_csv("data/events/notices.csv", dtype=str).fillna("")
for _, n in s[s["windows"] == "[]"].head(4).iterrows():
    t = fetch_text(n["url"])
    print(f"\n===== {n['ref']} {n['title']}\n{repr(t[:900])}")
