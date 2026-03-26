# BestBuyMedical Scraper

Playwright scraper for BestBuyMedical orders page (logged-in). Logs in, scrapes product result pages, extracts SKU/price/status/discontinued/heavy flags, deduplicates across the run, check for deviations and writes CSV outputs.


## What this does (quick summary)

* Logs into BestBuyMedical using `BBM_USERNAME` / `BBM_PASSWORD` env vars.

* Clicks through the order/search flow, runs a page-by-page extraction.

* Extracts each product block (sku, price, BestBuy status, discontinued flag, heavy item flag).

* Creates a deduplication ID (SKU) so changes in any of those fields will be considered different.

* Writes:

  * `bestBuy_products.csv` — main product output (SKU, Price, Supplier, Discontinued, Stock Status).

---

## Features

* Logged-in scraping (handles login flow).

* Optional page-range scraping via positional CLI args (`start_page end_page`).

* Per-instance artifact files (CSV, checkpoint, and log) for parallel runs.

* Robust DOM extraction inside a single `page.evaluate(...)`.

* Deduplication across the entire run using a combined hash key.

* Price parsing and `+ $4` surcharge for heavy items (recorded in price updates CSV).

* Saves debug snapshots for some failure cases.

* Small audit files for skipped/updated items.

---

## Requirements

- Python 3.9+
- Packages: `playwright`, `requests`
- Playwright browsers installed (Chromium)
- `email_notifier.notify_admin` available or remove its calls

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install playwright requests
python -m playwright install chromium
```

## Environment variables

- `BBM_USERNAME` — BestBuyMedical username
- `BBM_PASSWORD` — BestBuyMedical password
- `API_ENDPOINT` — optional endpoint for CSV upload (if used)

## Run

```bash
python bestbuymedical_scraper.py
```

### Run with page ranges (parallel instances)

```bash
python bestbuymedical_scraper.py 1 212
python bestbuymedical_scraper.py 213 425
python bestbuymedical_scraper.py 426 637
python bestbuymedical_scraper.py 638 850
```

PM2 examples:

```bash
pm2 start bestbuymedical_scraper.py --name "scraper-1" --interpreter python -- 1 212
pm2 start bestbuymedical_scraper.py --name "scraper-2" --interpreter python -- 213 425
```

Each range run produces independent files, for example:

* `bestBuy_products_1_212.csv`
* `bestbuy_scraper_checkpoint_1_212.json`
* `bestbuy_scraper_1_212.log`

Each scraper instance sends a completion email with its CSV attached.

## Manual Post-Processing

Merge all CSV files in the current folder:

```bash
python merge_csv_files.py
```

Optional parameters:

```bash
python merge_csv_files.py --output bestBuy_products_merged.csv --pattern "bestBuy_products_*.csv"
```

Convert CSV to JSON manually:

```bash
python csv_to_json_transformer.py bestBuy_products_merged.csv
```

## Notes

- Keep credentials out of source control.
- Adjust timeouts/constants in the script if needed.
