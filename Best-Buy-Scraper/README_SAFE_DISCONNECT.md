# Safe Disconnect Checklist (PM2 Scrapers on EC2)

Use this checklist before disconnecting SSH or shutting down your local PC.

## 1) Confirm scrapers are running in PM2

```bash
pm2 status
```

## 2) Confirm progress files are updating

```bash
cd ~/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper
watch -n 10 'ls -lh bestBuy_products_*.csv bestbuy_scraper_checkpoint_*.json 2>/dev/null'
```

Let it run for 20-30 seconds and verify file sizes/timestamps are changing, then press `Ctrl+C`.

## 3) Save PM2 process list

```bash
pm2 save
```

## 4) Enable PM2 auto-start on reboot (one-time setup)

```bash
pm2 startup
```

Run the exact `sudo ...` command shown by PM2, then run:

```bash
pm2 save
```

## 5) Optional safety backup before logout

```bash
mkdir -p ~/scraper_backups
tar -czf ~/scraper_backups/scrape_backup_$(date +%F_%H%M).tar.gz ~/parallel-scraper-bestbuy/product-scraper/Best-Buy-Scraper
```

## 6) Optional: monitor in tmux session

```bash
sudo apt-get install -y tmux
tmux new -s monitor
```

Inside tmux:

```bash
watch -n 5 'pm2 status; echo; free -h'
```

Detach from tmux (leave it running):
- Press `Ctrl+B`, then `D`

## 7) Disconnect safely

```bash
exit
```

Now you can shut down your local PC.

## 8) After reconnect, verify everything continued

```bash
pm2 status
pm2 logs --lines 80
```

## Notes

- Do not run `pm2 delete all` while scrapers are in progress.
- Do not restart scraper processes unless necessary.
- SSH disconnect alone should not stop PM2-managed processes.
