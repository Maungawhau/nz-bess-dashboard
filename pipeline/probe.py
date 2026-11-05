"""Print the real EMI file layouts and the battery codes they contain.

Run by the "Probe EMI schemas" workflow. Its output (probe_report.md, attached to the run)
is what we use to confirm the column names and POC/unit codes in config/bess.yaml.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

from . import emi

lines: list[str] = []


def say(s=""):
    print(s)
    lines.append(s)


def main():
    cfg = yaml.safe_load(open("config/bess.yaml"))
    pocs = [b["poc"] for b in cfg["bess"] if b.get("poc")]
    d = datetime.now(ZoneInfo("Pacific/Auckland")).date() - timedelta(days=3)
    say(f"# EMI probe - sample trading date {d}\n")
    for key in emi.DATASETS:
        say(f"## {key}")
        path = emi.latest_file("fleet") if key == "fleet" else emi.find_file(key, d)
        if not path:
            say("**not found** - check the folder in pipeline/emi.py DATASETS\n")
            continue
        say(f"file: `{path}`")
        df = emi.read_csv(path)
        say(f"rows: {len(df):,}\n\ncolumns: `{list(df.columns)}`\n")
        say("```\n" + df.head(4).to_string()[:3000] + "\n```")
        txt = df.astype(str).agg(" ".join, axis=1)
        if key == "fleet":
            hit = df[txt.str.upper().str.contains(r"\bBAT|BATTERY|BESS")]
            say(f"\nbattery rows ({len(hit)}):\n```\n{hit.to_string()[:4000]}\n```")
        else:
            for p in pocs + ["HLY", "GLN", "BRB"]:
                h = df[txt.str.contains(p, regex=False)]
                if len(h):
                    say(f"\n{p}: {len(h)} rows, e.g.\n```\n{h.head(6).to_string()[:2500]}\n```")
        say("")
    open("probe_report.md", "w").write("\n".join(lines))


if __name__ == "__main__":
    main()
