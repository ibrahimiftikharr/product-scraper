# EC2 PM2 Scraper Commands (Exact Order)

Run these commands on your Ubuntu EC2 instance, in this exact sequence.

## 1) Go to project and activate venv

```bash
cd ~/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper
source .venv/bin/activate
```

## 2) Verify Python and packages

```bash
python -V
python -c "import playwright,requests,dotenv; print('deps ok')"
```

## 3) Install Playwright browser and Linux dependencies

```bash
python -m playwright install chromium
sudo ./.venv/bin/python -m playwright install-deps chromium
```

## 4) Clean old PM2 state

```bash
pm2 delete all
pm2 flush
```

## 5) Start all scraper instances

```bash
pm2 start /home/ubuntu/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper/.venv/bin/python --name scraper-265-411 --cwd /home/ubuntu/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper --restart-delay 10000 --max-restarts 5 -- bestbuymedical_scraper.py 265 411
pm2 start /home/ubuntu/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper/.venv/bin/python --name scraper-412-559 --cwd /home/ubuntu/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper --restart-delay 10000 --max-restarts 5 -- bestbuymedical_scraper.py 412 559
pm2 start /home/ubuntu/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper/.venv/bin/python --name scraper-560-707 --cwd /home/ubuntu/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper --restart-delay 10000 --max-restarts 5 -- bestbuymedical_scraper.py 560 707
pm2 start /home/ubuntu/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper/.venv/bin/python --name scraper-708-854 --cwd /home/ubuntu/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper --restart-delay 10000 --max-restarts 5 -- bestbuymedical_scraper.py 708 854
```

Default behavior is restart-safe: existing CSV/checkpoint files are kept and resume is attempted from checkpoint.

Only if you intentionally want to restart a range from scratch, add `--reset`:

```bash
pm2 start /home/ubuntu/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper/.venv/bin/python --name scraper-265-411 --cwd /home/ubuntu/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper --restart-delay 10000 --max-restarts 5 -- bestbuymedical_scraper.py 265 411 --reset
```

## 6) Verify and monitor

```bash
pm2 status
pm2 logs --lines 80
watch -n 3 'free -h; echo; uptime; echo; pm2 status'
```

## 7) Save PM2 process list (after stable)

```bash
pm2 save
```

## 8) Manual post-processing after completion

```bash
python merge_csv_files.py --pattern "bestBuy_products_*.csv" --output bestBuy_products_merged.csv
python csv_to_json_transformer.py bestBuy_products_merged.csv
```
