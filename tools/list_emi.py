"""List the newest EMI files in the folders the pipeline uses (debug aid)."""
import sys
sys.path.insert(0, ".")
from pipeline import emi
for key, (folder, suffix) in emi.DATASETS.items():
    if key in ("offers", "fleet"):
        continue
    names = [n for n in emi.list_blobs(f"{emi.ROOT}/{folder}/")]
    print(f"\n## {key}  ({len(names)} files under {folder})")
    for n in sorted(names, key=lambda n: n.rsplit('/', 1)[-1])[-6:]:
        print("  ", n)
for extra in ("DispatchAndPricing/DispatchEnergyPrices/2026/", "DispatchAndPricing/FinalEnergyPrices/2026/", "DispatchAndPricing/FinalEnergyPrices/ByMonth/"):
    names = sorted(emi.list_blobs(f"{emi.ROOT}/{extra}"))
    print(f"\n## {extra}: {len(names)} files"); [print("  ", n) for n in names[-5:]]
