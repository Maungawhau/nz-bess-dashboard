"""Print the structure of Transpower's notice pages and sample notice text (run on GitHub Actions)."""
import io, re, collections, requests
from pypdf import PdfReader

H = {"User-Agent": "nz-bess-dashboard (github.com/Maungawhau/nz-bess-dashboard)"}
PAGES = {
    "can": "https://www.transpower.co.nz/system-operator/notices-and-reporting/customer-advice-notices-can",
    "excursion": "https://www.transpower.co.nz/system-operator/notices-and-reporting/excursion-notices",
}
for name, url in PAGES.items():
    r = requests.get(url, headers=H, timeout=60)
    html = r.text
    print(f"\n######## {name}: HTTP {r.status_code}, {len(html):,} chars, final url {r.url}")
    links = re.findall(r'href="([^"]+\.pdf[^"]*)"', html)
    print("pdf links:", len(links))
    for l in links[:5]:
        print("  ", l[:200])
    # show the HTML around the first link, to learn the row/date markup
    if links:
        i = html.find(links[0])
        print("---- context around first link ----")
        print(re.sub(r"\s+", " ", html[max(0, i - 900): i + 700]))
    print("---- year selector / forms ----")
    for m in re.finditer(r"<select[^>]*>.*?</select>", html, re.S):
        print(re.sub(r"\s+", " ", m.group(0))[:600])
    for m in re.finditer(r'<form[^>]*>', html):
        print(m.group(0)[:300])
    print("query-string links mentioning year:", sorted(set(re.findall(r'href="([^"]*year[^"]*)"', html)))[:10])
    titles = [re.sub(r"%20|\+", " ", l.split("/")[-1].split(".pdf")[0]) for l in links]
    kinds = collections.Counter(re.sub(r"\s*\d{6,}.*$", "", t) for t in titles)
    print("types:", kinds.most_common(30))
    seen = set()
    for l, t in zip(links, titles):
        k = re.sub(r"\s*\d{6,}.*$", "", t)
        want = any(w in k.lower() for w in ["residual", "hvdc", "frequency", "excursion", "island", "emergency", "warning", "pole"]) or name == "excursion"
        if k in seen or not want or len(seen) >= 9:
            continue
        seen.add(k)
        u = l if l.startswith("http") else "https://www.transpower.co.nz" + l
        try:
            pr = requests.get(u, headers=H, timeout=60)
            txt = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pr.content)).pages)
            print(f"\n======== SAMPLE [{k}] {u[:160]}\n{txt[:1800]}")
        except Exception as e:
            print("failed", u, e)
