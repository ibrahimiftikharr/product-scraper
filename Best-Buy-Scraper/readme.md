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

## Notes

- Keep credentials out of source control.
- Adjust timeouts/constants in the script if needed.
