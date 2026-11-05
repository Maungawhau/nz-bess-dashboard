# NZ BESS dispatch & revenue dashboard

This dashboard shows how New Zealand's grid-scale batteries are dispatched and what they earn. It is built automatically every night from the Electricity Authority's public EMI datasets, with no API key needed.

- **Revenue** per battery and in total: energy arbitrage plus FIR and SIR reserves, viewable by day, week, month or year
- **Volumes and prices**: MWh charged and discharged, average buy and sell prices, spread, cycles and round-trip efficiency
- **How each battery is run**: its average daily dispatch profile by trading period
- **What the batteries displace**: the plant (gas, Huntly coal/gas, hydro and so on) that would have cleared next in the island's offer stack, with a separate view of tight periods

## How it works

```
GitHub Actions (02:30 NZT nightly)
   └─ pipeline/build.py   downloads yesterday's EMI files, re-does the last 4 days (interim → final prices)
        ├─ pipeline/bess.py          discharge / charge / reserves / revenue per battery per half-hour
        └─ pipeline/displacement.py  merit-order shift on the offer stack
   └─ commits data/processed/ (half-hourly history) and docs/data/ (dashboard JSON)
GitHub Pages serves docs/ → the dashboard
```

| EMI dataset | Used for |
|---|---|
| DispatchAndPricing/ClearedOffers | Discharge MW (injection unit), FIR/SIR cleared MW (both units) |
| DispatchAndPricing/FinalEnergyPrices | Node price for each half-hour |
| DispatchAndPricing/FinalReservePrices | FIR/SIR price for each island |
| DispatchAndPricing/NodalPricesAndVolumes | Charging (dispatched load on the offtake unit) |
| BidsAndOffers/Offers | Offer stack for the displacement analysis |
| Generation/GenerationFleet/Existing | Fuel tags and automatic detection of new batteries |

## Adding a new battery

Edit `config/bess.yaml`, copy a block, fill in the connection point (POC), the injection and offtake unit codes, and the commission date, then commit. The next nightly run back-fills it from `start_date`. Batteries that EMI's plant list tags `BAT` but that aren't in the config are added automatically, show as *auto-detected* on the dashboard, and are listed in the data notes.

## One-time setup (about 15 minutes)

1. **Create a GitHub account** at github.com (the free plan is fine).
2. **Create a repository**: click **+ → New repository**, name it `nz-bess-dashboard`, set it to *Public* (Pages is free for public repos; a private repo needs a paid plan for Pages) and leave everything else unticked.
3. **Put this code in it**: either let Claude push it (connect GitHub in Claude and share the repo name), or use **Add file → Upload files** and drag in the whole folder contents, including the hidden `.github` folder.
4. **Let the job push data**: go to **Settings → Actions → General → Workflow permissions**, choose *Read and write permissions* and save.
5. **Check the data layout**: go to **Actions → Probe EMI schemas → Run workflow**. When it finishes, open the run. The summary shows the real column names and battery codes. Fix any `# verify` codes in `config/bess.yaml` (or ask Claude to, with the report).
6. **Back-fill**: go to **Actions → Nightly BESS update → Run workflow**. One run back-fills the whole history from 1 January 2026 (about 15 seconds per day).
7. **Publish the dashboard**: go to **Settings → Pages → Build and deployment**, choose *Deploy from a branch*, select `main` and `/docs`, and save. The dashboard appears at `https://<your-username>.github.io/nz-bess-dashboard/`.

## Running locally

```bash
pip install -r requirements.txt
python -m pipeline.build                     # incremental
python -m pipeline.build --from 2026-03-01 --to 2026-03-31
cd docs && python -m http.server             # open http://localhost:8000

# offline test with synthetic EMI-shaped files
python tests/make_synthetic.py tests/fixtures 2026-01-01 2026-03-31
EMI_LOCAL_DIR=tests/fixtures BESS_CONFIG=tests/bess_test.yaml python -m pipeline.build --from 2026-01-01 --to 2026-03-31
```

## Caveats

- **Gross spot value, not profit.** Hedges, frequency keeping, constrained-on payments, transmission and reserve cost allocation, and losses are excluded.
- **Charging.** Each battery has an injection unit and an offtake unit in EMI (e.g. Ruakākā RUK0/RUK99), and charging is the dispatched load on the offtake unit. The EA's decision moves batteries to a single bid/offer form from 31 March 2028; when that happens the unit mapping will need updating.
- **Displacement is indicative.** It is a merit-order shift that ignores transmission constraints, the HVDC link, energy/reserve co-optimisation, ramping and unit commitment.

## Next iteration ideas

- Overlays for context: Transpower customer advice notices and low residual situation notices, weather (wind and solar lulls, cold snaps), and HVDC transfer
- Battery position in the offer stack: the price of the battery's highest cleared tranche against the marginal price
- A vSPD re-run of tight periods without the batteries, for a constraint-aware counterfactual
