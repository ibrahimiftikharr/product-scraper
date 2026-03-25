import csv
import time
import os
import re
import hashlib
import json
import shutil
import sys
import glob
import platform
from datetime import datetime
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import requests
from email_notifier import notify_admin
from format_validator import check_page_format_deviation, send_format_deviation_alert, validate_price_parsing
from csv_to_json_transformer import transform_csv_to_json

from dotenv import load_dotenv
load_dotenv()   # loads .env into os.environ

# -----------------------------------------------------------------------------
# MODULE OVERVIEW
# -----------------------------------------------------------------------------
# This script automates the following end-to-end workflow:
# 1. Login to BestBuyMedical ordering portal.
# 2. Navigate to order search results.
# 3. Trigger product search with no keyword filter.
# 4. Iterate through listing pages and extract product-level fields.
# 5. Optionally open detail pages (More info) to extract specifications.
# 6. Normalize values and write rows into CSV.
# 7. Send notifications and copy the output file locally.
#
# The design intentionally keeps extraction steps split into small helpers so
# selector updates are easier when the supplier UI changes.


class TeeStream:
    """Write output to both original stream (console) and a file stream."""

    def __init__(self, original_stream, file_stream):
        self.original_stream = original_stream
        self.file_stream = file_stream

    def write(self, data):
        self.original_stream.write(data)
        self.file_stream.write(data)

    def flush(self):
        self.original_stream.flush()
        self.file_stream.flush()

    def isatty(self):
        try:
            return self.original_stream.isatty()
        except Exception:
            return False


def start_console_file_logging(log_dir=None):
    """Mirror stdout/stderr to a single fixed log file and return stream state."""
    if log_dir is None:
        log_dir = os.getcwd()

    os.makedirs(log_dir, exist_ok=True)

    # Keep only one actively used log filename for all runs.
    log_path = os.path.join(log_dir, "bestbuy_scraper.log")

    # Cleanup legacy timestamped logs from previous implementation.
    for old_log in glob.glob(os.path.join(log_dir, "bestbuy_scraper_run_*.log")):
        try:
            os.remove(old_log)
        except Exception:
            pass

    # Line buffering keeps logs visible in near-real-time in both console and file.
    log_file = open(log_path, "w", encoding="utf-8", buffering=1)

    original_stdout = sys.stdout
    original_stderr = sys.stderr

    sys.stdout = TeeStream(original_stdout, log_file)
    sys.stderr = TeeStream(original_stderr, log_file)

    print(f"📝 Logging to file: {log_path}")
    return log_path, log_file, original_stdout, original_stderr


def stop_console_file_logging(log_file, original_stdout, original_stderr):
    """Restore original streams and close active log file stream."""
    sys.stdout = original_stdout
    sys.stderr = original_stderr
    try:
        log_file.close()
    except Exception:
        pass

API_ENDPOINT = "https://stockupdater.behope.com/api/upload?source=bestbuy"

def send_csv_to_api(filepath):
    """Upload the generated CSV to the upstream API endpoint.

    This function is currently not called in `run_scraper` because upload is
    temporarily disabled there, but it is kept intact for easy re-enabling.
    """
    # Open CSV in binary mode because requests multipart upload expects bytes.
    with open(filepath, "rb") as f:
        # API expects a form field named "file".
        files = {"file": (os.path.basename(filepath), f, "text/csv")}
        try:
            # Keep timeout finite so network issues do not hang the run forever.
            resp = requests.post(API_ENDPOINT, files=files, timeout=60)
            if resp.status_code == 200:
                try:
                    # API contract is JSON { success: bool, jobId?: str, ... }.
                    data = resp.json()
                    if data.get("success"):
                        print(f"✅ CSV uploaded successfully. Job ID: {data.get('jobId')}")
                        return True
                    else:
                        # Explicit API failure payload (200 but success=false).
                        msg = f"⚠️ API returned failure: {data}"
                        print(msg)
                        notify_admin("Scraper: CSV upload failed", msg)
                        return False
                except Exception as e:
                    # Response not JSON or schema changed unexpectedly.
                    msg = f"⚠️ Could not parse JSON response: {e}\nRaw response: {resp.text}"
                    print(msg)
                    notify_admin("Scraper: Invalid API response", msg)
                    return False
            else:
                # Non-200 status code indicates transport or server-side failure.
                msg = f"⚠️ Failed to upload CSV.\nStatus: {resp.status_code}\nResponse: {resp.text}"
                print(msg)
                notify_admin("Scraper: CSV upload failed", msg)
                return False
        except Exception as e:
            # Covers DNS failures, connectivity issues, SSL errors, etc.
            msg = f"❌ Error uploading CSV: {e}"
            print(msg)
            notify_admin("Scraper: API connection error", msg)
            return False


# ---------- CONFIG ----------
LOGIN_URL = "https://orders.bestbuymedical.ca/index.html"
USERNAME = os.environ.get("BBM_USERNAME", "62317")
PASSWORD = os.environ.get("BBM_PASSWORD", "secret123!")
BROWSER_HEADLESS = os.environ.get("BBM_HEADLESS", "true").strip().lower() in {"1", "true", "yes", "y"}

# Linux EC2 often needs these flags (especially as root or in constrained VMs).
PLAYWRIGHT_LAUNCH_ARGS = []
if platform.system().lower() == "linux":
    PLAYWRIGHT_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

PRODUCT_WAIT_TIMEOUT = 30    # seconds to wait for products to appear after navigation/click
POLL_INTERVAL = 1.0          # seconds between checks while waiting
EXCLUDED_SPEC_KEYS = {"manufacturer name", "manufacturer number", "reference number"}
MAX_PRODUCTS_TO_SAVE = None  # no row cap: scrape all available products
MAX_PAGE_NAV_RETRIES = 3
CHECKPOINT_FILE = "bestbuy_scraper_checkpoint.json"


def resolve_artifact_copy_dir(script_dir):
    """Resolve where run artifacts should be copied (CSV/JSON/log).

    Priority:
    1. BBM_OUTPUT_DIR env var (explicit override)
    2. ~/Downloads if it already exists
    3. <repo>/output (created automatically)
    """
    configured_dir = os.environ.get("BBM_OUTPUT_DIR", "").strip()
    if configured_dir:
        os.makedirs(configured_dir, exist_ok=True)
        return configured_dir

    downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
    if os.path.isdir(downloads_dir):
        return downloads_dir

    fallback_dir = os.path.join(script_dir, "output")
    os.makedirs(fallback_dir, exist_ok=True)
    return fallback_dir


def dismiss_cookie_banner(page):
    """Try to close or remove cookie/consent banners (best-effort)."""
    try:
        # Strategy A: click common button texts.
        candidates = ["Accept", "I Agree", "Agree", "OK", "Ok", "Close", "Dismiss",
                      "Got it", "Accept Cookies", "Accept All", "Accept all cookies"]
        for txt in candidates:
            try:
                locator = page.locator(f"button:has-text('{txt}')")
                if locator.count() > 0:
                    locator.first.click(timeout=2000)
                    return True
            except Exception:
                pass
        # Strategy B: same text list but for <a> links.
        for txt in candidates:
            try:
                locator = page.locator(f"a:has-text('{txt}')")
                if locator.count() > 0:
                    locator.first.click(timeout=2000)
                    return True
            except Exception:
                pass
        # Strategy C: click likely cookie/consent selectors.
        selectors = [
            "[id*='cookie']", "[class*='cookie']",
            "[id*='consent']", "[class*='consent']",
            "[id*='gdpr']", "[class*='gdpr']",
            ".cc-btn", ".cookie-accept", ".cookie-btn", ".cookie-banner button",
            ".qc-cmp2-summary-buttons .qc-cmp2-btn", ".eu-cookie-compliance"
        ]
        for sel in selectors:
            try:
                if page.locator(sel).count() > 0:
                    try:
                        page.locator(sel).first.click(timeout=2000)
                        return True
                    except Exception:
                        return True
            except Exception:
                pass
        try:
            # Strategy D (last resort): remove matching overlay nodes via JS.
            removed = page.evaluate("""() => {
                const nodes = Array.from(document.querySelectorAll('[id*=cookie],[class*=cookie],[id*=consent],[class*=consent],[id*=gdpr],[class*=gdpr]'));
                for (const n of nodes) n.remove();
                return nodes.length;
            }""")
            return bool(removed)
        except Exception:
            pass
    except Exception:
        pass
    return False


def load_checkpoint(checkpoint_path):
    """Load resume checkpoint from disk, if present and valid."""
    if not os.path.exists(checkpoint_path):
        return None
    try:
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception as e:
        print(f"Warning: could not read checkpoint {checkpoint_path}: {e}")
        return None


def save_checkpoint(checkpoint_path, next_page, total_rows_saved, pages_scraped):
    """Persist progress so reruns can resume from next page instead of page 1."""
    payload = {
        "next_page": int(next_page),
        "total_rows_saved": int(total_rows_saved),
        "pages_scraped": int(pages_scraped),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Warning: failed to save checkpoint: {e}")


def clear_checkpoint(checkpoint_path):
    """Remove checkpoint after successful completion."""
    try:
        if os.path.exists(checkpoint_path):
            os.remove(checkpoint_path)
    except Exception as e:
        print(f"Warning: failed to clear checkpoint {checkpoint_path}: {e}")


def count_existing_csv_rows(output_csv):
    """Count already-saved data rows in an existing CSV file."""
    if not os.path.exists(output_csv):
        return 0
    try:
        with open(output_csv, "r", encoding="utf-8", newline="") as f:
            return sum(1 for _ in csv.DictReader(f))
    except Exception as e:
        print(f"Warning: could not count rows in {output_csv}: {e}")
        return 0


def load_seen_product_ids_from_csv(output_csv):
    """Build dedupe set from existing CSV so resumed runs do not rewrite products."""
    seen = set()
    if not os.path.exists(output_csv):
        return seen

    try:
        with open(output_csv, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                sku = (row.get("SKU") or "").strip()
                if sku:
                    seen.add(sku)
    except Exception as e:
        print(f"Warning: could not load seen SKUs from CSV: {e}")
    return seen


def jump_to_page(page, target_page, timeout=30000):
    """Navigate listing results to a target page number using site JS pagination."""
    if target_page <= 1:
        return True

    print(f"Resuming scrape at page {target_page}...")
    for attempt in range(1, MAX_PAGE_NAV_RETRIES + 1):
        try:
            page.evaluate(
                """(p) => {
                    if (typeof SearchItems === 'function') {
                        SearchItems('Paging', p);
                    } else {
                        throw new Error('SearchItems function not available');
                    }
                }""",
                target_page,
            )

            start = time.time()
            while time.time() - start < (timeout / 1000.0):
                active = get_active_page_number(page)
                if active == target_page:
                    return True
                time.sleep(0.5)

            print(f"Warning: jump attempt {attempt} timed out waiting for page {target_page}.")
        except Exception as e:
            print(f"Warning: jump attempt {attempt} to page {target_page} failed: {e}")

    return False


def extract_products_from_page(page):
    """Extract product rows from the listing page using in-page JavaScript.

    Each returned dict contains the fields needed for CSV export plus the
    optional `more_info_href` for detail-page specification scraping.
    """
    # Keeping DOM extraction in one JS evaluation is significantly faster than
    # multiple Playwright locator round-trips per field.
    js = r"""
    () => {
        const results = [];
        const rows = Array.from(document.querySelectorAll('div.row')).filter(el => {
            const cn = el.className || '';
            return cn.indexOf('my-2') !== -1 && cn.indexOf('py-2') !== -1;
        });

        const statusRegex = /specialty product|mfg bo|in stock|out of stock|discontinued|in stock date/i;

        function isBadgeElement(el) {
            if (!el) return false;
            try {
                if ((el.className || '').toString().toLowerCase().includes('badge')) return true;
                if (el.closest) {
                    const b = el.closest('.badge, .badge-warning, .badge-danger, .badge-warning');
                    if (b) return true;
                }
                const t = (el.innerText || el.textContent || '').trim();
                if (t && /discontinued/i.test(t) && t.length < 40) return true;
            } catch (e) {}
            return false;
        }

        for (const container of rows) {
            // SKU
            let sku = null;
            const h5s = Array.from(container.querySelectorAll('h5'));
            let itemHeader = h5s.find(h => (h.innerText || '').toLowerCase().includes('item'));
            if (itemHeader) {
                let sib = itemHeader.nextElementSibling;
                if (sib && sib.tagName && sib.tagName.toLowerCase() === 'p') {
                    sku = (sib.innerText || '').trim();
                } else {
                    const p = container.querySelector('p');
                    if (p) sku = (p.innerText || '').trim();
                }
            } else {
                const p = container.querySelector('p');
                if (p) sku = (p.innerText || '').trim();
            }

            // Price
            let price = null;
            const priceH5 = Array.from(container.querySelectorAll('h5')).find(h => (h.innerText || '').toLowerCase().includes('price'));
            if (priceH5) {
                const strongs = priceH5.querySelectorAll('strong');
                if (strongs && strongs.length > 1) price = (strongs[strongs.length - 1].innerText || '').trim();
                else price = (priceH5.innerText || '').trim();
            }
            if (!price) {
                const txt = container.innerText || '';
                const idx = txt.indexOf('$');
                if (idx !== -1) {
                    const frag = txt.substring(idx, Math.min(txt.length, idx + 40));
                    const re = new RegExp("\\$\\s*\\d[\\d,]*(?:\\.\\d{1,2})?");
                    const m = re.exec(frag);
                    if (m) price = m[0].trim();
                }
            }

            // Discontinued badge detection
            let discontinued = false;
            try {
                const disc = container.querySelector('.badge.badge-warning, .badge');
                if (disc && (disc.innerText || '').toLowerCase().includes('discontinued')) {
                    discontinued = true;
                }
            } catch (e) {}

            // Status extraction - find the rightmost column (price column) WITHIN this product row
            let bestbuy_status = null;
            let priceColumn = null;
            
            // Find price column within THIS container only
            const columns = container.querySelectorAll(':scope > div[class*="col-xl-3"], :scope > div[class*="col-lg-3"]');
            if (columns.length > 0) {
                priceColumn = columns[columns.length - 1]; // Take last matching column in THIS row
            } else if (priceH5) {
                // Navigate up from price element
                priceColumn = priceH5.parentElement;
                for (let i = 0; i < 3 && priceColumn; i++) {
                    priceColumn = priceColumn.parentElement;
                }
            }
            
            // Extract status from price column
            if (priceColumn) {
                // 1. Try colored status divs (most reliable)
                const statusDivs = priceColumn.querySelectorAll('div.text-success, div.text-danger, div.text-warning');
                for (const div of statusDivs) {
                    const span = div.querySelector('span.language-english');
                    if (span) {
                        const text = (span.innerText || span.textContent || '').trim();
                        if (text && statusRegex.test(text)) {
                            bestbuy_status = text;
                            break;
                        }
                    }
                }
                
                // 2. Try any status span in price column
                if (!bestbuy_status) {
                    const spans = priceColumn.querySelectorAll('span.language-english');
                    for (const span of spans) {
                        if (isBadgeElement(span)) continue;
                        const text = (span.innerText || span.textContent || '').trim();
                        if (text && statusRegex.test(text)) {
                            bestbuy_status = text;
                            break;
                        }
                    }
                }
                
                // 3. Fallback: parse price column text
                if (!bestbuy_status) {
                    const text = priceColumn.innerText || '';
                    if (/in stock/i.test(text) && !/out of stock/i.test(text)) {
                        bestbuy_status = 'In Stock';
                    } else if (/out of stock/i.test(text)) {
                        bestbuy_status = 'Out of Stock';
                    } else if (/specialty product/i.test(text)) {
                        bestbuy_status = 'Specialty Product - Order Created Upon Submission';
                    } else if (/mfg bo/i.test(text)) {
                        bestbuy_status = 'MFG BO';
                    }
                }
            }

            // Clean up if status is just "discontinued" text
            if (bestbuy_status && /^discontinued$/i.test(bestbuy_status.trim())) {
                bestbuy_status = null;
            }

            // HEAVY ITEM detection: look for the text "Heavy Item" in element text or inside <i> or a nearby span
            let heavy_item = false;
            try {
                const inner = (container.innerText || '').toLowerCase();
                if (inner.indexOf('heavy item') !== -1) heavy_item = true;
                // look for <i> elements containing Heavy Item
                const iEls = Array.from(container.querySelectorAll('i'));
                for (const iEl of iEls) {
                    if ((iEl.innerText || '').toLowerCase().indexOf('heavy item') !== -1) {
                        heavy_item = true;
                        break;
                    }
                }
            } catch (e) {
                heavy_item = false;
            }

            // More info link (if present) to open product details page
            let more_info_href = null;
            try {
                const anchors = Array.from(container.querySelectorAll('a[href]'));
                for (const a of anchors) {
                    const txt = (a.innerText || a.textContent || '').toLowerCase().trim();
                    const href = (a.getAttribute('href') || '').trim();
                    if (!href) continue;
                    if (txt.includes('more info') || txt.includes("plus d'info")) {
                        more_info_href = href;
                        break;
                    }
                }
            } catch (e) {}

            results.push({
                sku: sku || null,
                price: price || null,
                bestbuy_status: bestbuy_status || null,
                discontinued: !!discontinued,
                heavy_item: !!heavy_item,
                more_info_href: more_info_href || null
            });
        }

        return results;
    }
    """
    # Returned list is page-local snapshot; caller handles cross-page dedupe.
    return page.evaluate(js)


def map_behope_status(bestbuy_status, discontinued=False):
    """Map site-specific stock text to the normalized status used in output CSV."""
    if not bestbuy_status:
        return "Out of Stock"

    s = bestbuy_status.lower()

    if discontinued and "in stock" in s and "mfg bo" not in s:
        return "In Stock"

    if discontinued and "specialty product" in s:
        return "Out of Stock"

    if "specialty product" in s:
        return "Long Lead Time"

    if "mfg bo" in s:
        return "Out of Stock"

    if "in stock" in s and "mfg bo" not in s:
        return "In Stock"

    if discontinued:
        return "Out of Stock"

    return "Out of Stock"


def extract_product_specifications_json(detail_page, listing_url, more_info_href, product_label=""):
    """Open product detail page and return filtered specifications as JSON text.

    Steps:
    1. Build an absolute detail URL from the listing URL + href.
    2. Open detail page and activate Product Specifications tab.
    3. Read table rows as key/value pairs.
    4. Drop excluded keys and serialize remaining fields to JSON.
    """
    if not more_info_href:
        return ""

    # Convert relative href (e.g., ./product.html?InventoryID=...) to absolute URL.
    detail_url = urljoin(listing_url, more_info_href)
    try:
        if product_label:
            print(f"{product_label} ..clicking more info")

        # Navigate dedicated detail tab/page so listing page state remains intact.
        detail_page.goto(detail_url, timeout=30000, wait_until="domcontentloaded")
        if product_label:
            print(f"{product_label} ..product details page opened: {detail_url}")
        dismiss_cookie_banner(detail_page)

        # Open product specifications tab when present.
        if product_label:
            print(f"{product_label} ..opening Product Specifications tab")
        try:
            detail_page.locator("#Specifications-tab").first.click(timeout=5000)
        except Exception:
            try:
                detail_page.locator("a.nav-link:has-text('Product Specifications')").first.click(timeout=5000)
            except Exception:
                pass

        try:
            detail_page.wait_for_selector("#Specifications table tbody tr", timeout=5000)
        except Exception:
            pass

        if product_label:
            print(f"{product_label} ..reading specification key/value rows")

        # Pull raw table rows from Product Specifications tab.
        specs = detail_page.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('#Specifications table tbody tr'));
            return rows.map((row) => {
                const tds = row.querySelectorAll('td');
                if (!tds || tds.length < 2) return null;
                return {
                    key: (tds[0].innerText || tds[0].textContent || '').trim(),
                    value: (tds[1].innerText || tds[1].textContent || '').trim()
                };
            }).filter(Boolean);
        }""")

        # Normalize + filter keys, then build final key/value dict.
        filtered = {}
        for pair in specs or []:
            key = (pair.get("key") or "").strip()
            value = (pair.get("value") or "").strip()
            if not key or not value:
                continue
            if key.lower() in EXCLUDED_SPEC_KEYS:
                continue
            filtered[key] = value

        if not filtered:
            if product_label:
                print(f"{product_label} ..making specs json")
                print(f"{product_label} ..json made: <empty after filtering>")
            return ""

        if product_label:
            print(f"{product_label} ..making specs json")
        specs_json = json.dumps(filtered, ensure_ascii=False)
        if product_label:
            print(f"{product_label} ..json made:")
            print(specs_json)
        return specs_json
    except Exception as e:
        if product_label:
            print(f"{product_label} ..ERROR while extracting specs from {detail_url}: {e}")
        else:
            print(f"Warning: failed to fetch specifications for {detail_url}: {e}")
        return ""


def format_price_string(value):
    """Render float prices in currency format used by downstream consumers."""
    try:
        return "${:,.2f}".format(value)
    except Exception:
        return str(value)


def parse_price_to_float(price_str):
    """Convert raw scraped price text into float for math/normalization."""
    if not price_str:
        return None
    try:
        # remove currency symbols, spaces, parentheses, etc
        cleaned = re.sub(r"[^\d\.\-]", "", price_str)
        if cleaned == "" or cleaned == "-" or cleaned == ".":
            return None
        return float(cleaned)
    except Exception:
        return None


def write_rows_to_csv(rows, output_csv, write_header=False):
    """Append transformed rows to CSV and optionally write header first."""
    keys = ["SKU", "Price", "Supplier", "Discontinued", "Stock Status", "product_specifications"]
    # Append mode is used across pages; first write may include header.
    mode = "a" if os.path.exists(output_csv) else "w"
    with open(output_csv, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        if write_header:
            writer.writeheader()
        for r in rows:
            writer.writerow({
                "SKU": r.get("sku"),
                "Price": r.get("price"),
                "Supplier": "Best Buy",
                "Discontinued": "Yes" if r.get("discontinued") else "No",
                "Stock Status": r.get("behope_status"),
                "product_specifications": r.get("product_specifications", ""),
            })

def count_product_rows(page):
    """Count listing result cards currently visible on the page."""
    try:
        return page.evaluate("""() => Array.from(document.querySelectorAll('div.row')).filter(el => {
            const cn = el.className || '';
            return cn.indexOf('my-2') !== -1 && cn.indexOf('py-2') !== -1;
        }).length
        """)
    except Exception:
        return 0


def wait_for_product_count(page, min_count=1, timeout=PRODUCT_WAIT_TIMEOUT, poll=POLL_INTERVAL):
    """Poll until listing cards appear (or timeout) to avoid scraping too early."""
    start = time.time()
    while True:
        count = count_product_rows(page)
        if count >= min_count:
            return True, count
        if time.time() - start >= timeout:
            return False, count
        time.sleep(poll)


def get_active_page_number(page):
    """Read active page number from paginator UI (returns None on failure)."""
    try:
        num = page.evaluate("""() => {
            const lis = Array.from(document.querySelectorAll('nav[aria-label="Search Result Pages"] li'));
            for (const li of lis) {
                if (li.classList.contains('active')) {
                    const a = li.querySelector('a.page-link');
                    if (a) {
                        const t = (a.innerText || '').trim();
                        const n = parseInt(t, 10);
                        if (!isNaN(n)) return n;
                    }
                }
            }
            return null;
        }""")
        return int(num) if num else None
    except Exception:
        return None


def click_next(page):
    """Click the next page button and wait for navigation to complete."""
    try:
        old_page_num = get_active_page_number(page)
        
        # Method 1: Try JavaScript function call first (most reliable)
        if old_page_num:
            next_page = old_page_num + 1
            try:
                res = page.evaluate(f"""() => {{ 
                    if (typeof SearchItems === 'function') {{ 
                        SearchItems('Paging', {next_page}); 
                        return true; 
                    }} 
                    return false; 
                }}""")
                if res:
                    # Wait for page number to actually change
                    time.sleep(1.0)
                    for _ in range(10):  # Try up to 5 seconds
                        new_page_num = get_active_page_number(page)
                        if new_page_num and new_page_num > old_page_num:
                            return True
                        time.sleep(0.5)
            except Exception:
                pass
        
        # Method 2: Try visible Next button in paginator.
        try:
            page.locator("nav[aria-label='Search Result Pages'] a.page-link:has-text('Next')").first.click(timeout=5000)
            time.sleep(1.0)
            for _ in range(10):
                new_page_num = get_active_page_number(page)
                if new_page_num and old_page_num and new_page_num > old_page_num:
                    return True
                time.sleep(0.5)
            return True  # Assume success if no error
        except Exception:
            pass
        
        # Method 3: Walk active <li> and click next sibling.
        try:
            result = page.evaluate("""() => {
                const lis = Array.from(document.querySelectorAll('nav[aria-label="Search Result Pages"] li'));
                for (let i=0;i<lis.length;i++){
                    const li = lis[i];
                    if (li.classList.contains('active')) {
                        const nextLi = lis[i+1];
                        if (nextLi && !nextLi.classList.contains('disabled')) {
                            const a = nextLi.querySelector('a.page-link');
                            if (a) { 
                                a.click(); 
                                return true; 
                            }
                        }
                    }
                }
                return false;
            }""")
            if result:
                time.sleep(1.0)
                for _ in range(10):
                    new_page_num = get_active_page_number(page)
                    if new_page_num and old_page_num and new_page_num > old_page_num:
                        return True
                    time.sleep(0.5)
                return True
        except Exception:
            pass
            
    except Exception as e:
        print(f"Click next error: {e}")
        pass
    
    return False


def has_next_page(page):
    """Return True when paginator indicates there is a next listing page."""
    try:
        return page.evaluate("""() => {
            const nav = document.querySelector('nav[aria-label="Search Result Pages"]');
            if (!nav) return false;
            const anchors = Array.from(nav.querySelectorAll('a.page-link'));
            const nextAnchor = anchors.find(a => (a.innerText || '').trim() === 'Next');
            if (nextAnchor) {
                const li = nextAnchor.closest('li');
                if (!li) return true;
                return !li.classList.contains('disabled');
            }
            const lis = Array.from(nav.querySelectorAll('li'));
            for (let i=0;i<lis.length;i++){
                if (lis[i].classList.contains('active')) {
                    const next = lis[i+1];
                    if (next && !next.classList.contains('disabled')) return true;
                }
            }
            return false;
        }""")
    except Exception:
        return False

def make_product_id(prod):
    """Build stable per-product ID for deduplication across paginated results."""
    raw_sku = prod.get("sku")
    
    if raw_sku:
        # Deduplicate by SKU only - it's the unique product identifier
        return str(raw_sku).strip()
    
    # Fallback for products without SKU: use all fields
    price = str(prod.get("price") or "")
    status = str(prod.get("bestbuy_status") or "")
    discontinued = "D" if prod.get("discontinued") else "ND"
    heavy = "H" if prod.get("heavy_item") else "NH"
    key = "|".join([price, status, discontinued, heavy])
    return "HASH:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def main(output_csv):
    """Run the full scrape: login, search, paginate, enrich, validate, write CSV."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    checkpoint_path = os.path.join(script_dir, CHECKPOINT_FILE)
    checkpoint = load_checkpoint(checkpoint_path)

    resume_mode = bool(checkpoint and os.path.exists(output_csv))
    if resume_mode:
        print(f"🔁 Resume mode enabled using checkpoint: {checkpoint_path}")
    else:
        # Fresh run: clear old checkpoint and reset output CSV.
        clear_checkpoint(checkpoint_path)
        if os.path.exists(output_csv):
            os.remove(output_csv)

    with sync_playwright() as p:
        try:
            # Headful mode is currently used due to environment-specific
            # headless-shell launch issues observed during testing.
            browser = p.chromium.launch(headless=BROWSER_HEADLESS, args=PLAYWRIGHT_LAUNCH_ARGS)
            context = browser.new_context()
            page = context.new_page()
        except Exception as e:
            notify_admin("Scraper: browser start failed", f"Failed to launch browser: {e}")
            raise

        # Step 1: Authenticate into the supplier portal.
        print("Opening login page...")
        try:
            page.goto(LOGIN_URL, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            msg = f"Failed to open login page ({LOGIN_URL}): {e}"
            print(msg)
            notify_admin("Scraper: Failed to open login page", msg)
            browser.close()
            return None

        # Cookie banner can block input fields; clear it early if present.
        dismiss_cookie_banner(page)

        print("Filling credentials...")
        try:
            page.fill("input#login-username", USERNAME)
            page.fill("input#login-password", PASSWORD)
        except Exception as e:
            msg = f"Failed to fill credentials: {e}"
            print(msg)
            notify_admin("Scraper: Failed to fill credentials", msg)
            browser.close()
            return None

        print("Clicking Login...")
        try:
            page.click("button[onclick*='validateLogin']", timeout=7000)
        except PlaywrightTimeout:
            try:
                page.click("button.btn.btn-primary.btn-block", timeout=7000)
            except Exception:
                pass
        except Exception as e:
            print("Click login error:", e)

        # After login attempt, check if login succeeded; if not, email and bail out
        login_ok = False
        try:
            page.wait_for_selector("text=Create orders and have them delivered directly to your patients.", timeout=15000)
            login_ok = True
            print("Login appears successful (Home delivery orders found).")
        except PlaywrightTimeout:
            try:
                page.wait_for_url("**/index.html", timeout=15000)
                login_ok = True
                print("Login seemed successful (navigated to index.html).")
            except PlaywrightTimeout:
                login_ok = False

        if not login_ok:
            msg = f"Login did not complete (no expected selector or URL). Current page URL: {page.url}"
            print("Warning:", msg)
            notify_admin("Scraper: Login failed or site down", msg)
            browser.close()
            return None
        
        # Step 2: Navigate from landing page into drop-ship order flow.
        try:
            # Click the DropShipOrder Continue button specifically
            page.click("button[onclick*=\"checkForCartSession('DropShipOrder')\"]", timeout=10000)
            time.sleep(0.8)
        except Exception as e:
            print(f"❌ Could not click Continue button: {e}")
        
        try:
            page.click("button[onclick*=\"goToOrder('DropShipOrder','New',0)\"]", timeout=10000)
        except PlaywrightTimeout:
            try:
                page.locator("text=Create a new order").first.click()
            except Exception:
                print("Warning: couldn't click 'Create a new order' automatically.")
                try:
                    page.evaluate("() => { if (typeof goToOrder === 'function') goToOrder('DropShipOrder','New',0); }")
                except Exception:
                    pass

        try:
            page.wait_for_url("**/order.html", timeout=15000)
            print("Reached order.html page.")
        except PlaywrightTimeout:
            print("Warning: order.html may not have loaded; continuing anyway.")

        # Clear banners again after navigation because page templates differ.
        dismiss_cookie_banner(page)

        # Step 3: Ensure keyword filter is empty so all products are scraped.
        print("Clearing search keyword (no filter)...")
        try:
            page.fill("#search-keyword", "")
        except Exception:
            try:
                page.locator("input#search-keyword").first.fill("")
            except Exception as e:
                print(f"Warning: could not clear search keyword: {e}")

        try:
            page.locator("button[onclick*=\"SearchItems('New',1\"]").first.click()
        except Exception:
            try:
                page.locator("button.btn.btn-search").first.click()
            except Exception:
                try:
                    page.click("text=Search")
                except Exception:
                    try:
                        page.evaluate("""() => {
                            const input = document.querySelector('#search-keyword');
                            if (input) input.value = '';
                            if (typeof SearchItems === 'function') {
                                SearchItems('New', 1, 'ItemAsc', 'order.html');
                                return;
                            }
                            if (typeof GoToProducts === 'function') GoToProducts();
                        }""")
                    except Exception:
                        print("Warning: couldn't trigger Search via click; results might already be visible.")

        # Wait for initial results after keyword search before entering loop.
        ok, found = wait_for_product_count(page, min_count=1, timeout=PRODUCT_WAIT_TIMEOUT, poll=POLL_INTERVAL)
        if ok:
            print(f"Products appear: found {found} product block(s).")
            print("Products Found and Scraping started")
        else:
            print(f"Timed out waiting for products (found {found})")
            print("Products Found and Scraping started")

        # Step 4: Initialize counters, helper page for product details, and state.
        total_rows_saved = count_existing_csv_rows(output_csv) if resume_mode else 0
        pages_scraped = int((checkpoint or {}).get("pages_scraped", 0)) if resume_mode else 0
        write_header_first_time = not os.path.exists(output_csv) or os.path.getsize(output_csv) == 0
        page_num = int((checkpoint or {}).get("next_page", 1)) if resume_mode else 1
        # Reuse one detail page for all More Info navigation to reduce overhead.
        detail_page = context.new_page()

        # Dedupe set (across the entire run) and counters
        seen_product_ids = load_seen_product_ids_from_csv(output_csv) if resume_mode else set()
        duplicates_skipped = 0
        all_deviations = []
        hit_product_limit = False

        if resume_mode and page_num > 1:
            jumped = jump_to_page(page, page_num)
            if not jumped:
                msg = f"Failed to resume at page {page_num}; stopping to avoid bad data alignment."
                print(msg)
                notify_admin("Scraper: Resume failed", msg)
                try:
                    detail_page.close()
                except Exception:
                    pass
                browser.close()
                return None

        # Step 5: Process result pages until no next page or temporary row limit hit.
        while True:
            print(f"\n-------------Page#{page_num}----------------")
            dismiss_cookie_banner(page)

            ok, found = wait_for_product_count(page, min_count=1, timeout=PRODUCT_WAIT_TIMEOUT, poll=POLL_INTERVAL)
            if ok:
                print(f"Proceeding: detected {found} product block(s) on page {page_num}.")
            else:
                print(f"No products detected after waiting (page {page_num})")

            try:
                products = extract_products_from_page(page)
            except Exception as e:
                print("Extraction error:", e)
                products = []
            
            # Check for format deviations (no logs)
            deviation_check = check_page_format_deviation(page, page_num, products, page.url)
            if deviation_check["has_deviation"]:
                all_deviations.append(deviation_check)
            
            # Validate price parsing on first page (no logs)
            if page_num == 1 and products:
                price_issues = validate_price_parsing(products, parse_price_to_float)
                if price_issues:
                    all_deviations.append({"has_deviation": True, "issues": price_issues})

            rows_to_write = []

            # Step 6: Transform each product into normalized CSV row format.
            for product_idx, prod in enumerate(products, start=1):
                if MAX_PRODUCTS_TO_SAVE and (total_rows_saved + len(rows_to_write)) >= MAX_PRODUCTS_TO_SAVE:
                    hit_product_limit = True
                    break

                sku_display = (prod.get("sku") or "N/A").strip() if isinstance(prod.get("sku"), str) else (prod.get("sku") or "N/A")
                product_label = f"[Page {page_num} | Product {product_idx} | SKU={sku_display}]"
                print(f"====== PRODUCT # {product_idx} --- SKU={sku_display} ======")

                # Deduplicate using raw SKU when available, or a fallback hash
                pid = make_product_id(prod)
                if pid in seen_product_ids:
                    duplicates_skipped += 1
                    print(f"{product_label} ..duplicate detected, skipping (id={pid})")
                    print()
                    print()
                    continue
                seen_product_ids.add(pid)

                bestbuy_status = prod.get("bestbuy_status")
                discontinued = bool(prod.get("discontinued"))
                heavy_item = bool(prod.get("heavy_item"))
                behope_status = map_behope_status(bestbuy_status, discontinued)
                product_specifications = ""
                print(
                    f"{product_label} ..listing data => "
                    f"price={prod.get('price')}, status={bestbuy_status}, "
                    f"discontinued={discontinued}, heavy_item={heavy_item}"
                )

                # If a product exposes "More info...", extract detailed specs JSON.
                more_info_href = prod.get("more_info_href")
                if more_info_href:
                    product_specifications = extract_product_specifications_json(
                        detail_page=detail_page,
                        listing_url=page.url,
                        more_info_href=more_info_href,
                        product_label=product_label,
                    )
                else:
                    print(f"{product_label} ..no more info link on card; specs left empty")

                # Price handling: parse numeric, add $4 for heavy items, otherwise keep original.
                orig_price_str = prod.get("price")
                parsed = parse_price_to_float(orig_price_str)
                if parsed is not None:
                    if heavy_item:
                        updated_price_str = format_price_string(parsed + 4.0)
                    else:
                        updated_price_str = format_price_string(parsed)
                else:
                    updated_price_str = orig_price_str
                print(f"{product_label} ..normalized price={updated_price_str}, mapped stock status={behope_status}")

                row = {
                    "sku": prod.get("sku"),
                    "price": updated_price_str,
                    "discontinued": discontinued,
                    "behope_status": behope_status,
                    "product_specifications": product_specifications,
                }
                rows_to_write.append(row)
                print(f"{product_label} ..row prepared for CSV")
                print()
                print()

            # Persist page batch to disk once all rows are transformed.
            if rows_to_write:
                write_header = False
                if write_header_first_time:
                    write_header = True
                    write_header_first_time = False
                write_rows_to_csv(rows_to_write, output_csv, write_header=write_header)
                total_rows_saved += len(rows_to_write)
                pages_scraped += 1
                print(f"Saved {len(rows_to_write)} rows from page {page_num} (total saved: {total_rows_saved}).")

                # Save checkpoint after each successful page write.
                save_checkpoint(
                    checkpoint_path=checkpoint_path,
                    next_page=page_num + 1,
                    total_rows_saved=total_rows_saved,
                    pages_scraped=pages_scraped,
                )
            else:
                print(f"Found 0 products on page {page_num}; nothing written.")

            if hit_product_limit:
                print(f"Reached test product limit ({MAX_PRODUCTS_TO_SAVE}). Stopping scrape.")
                break

            # (Optional) additional page-limit switch for quick debugging.
            # if page_num >= 5:
            #     print("🧪 Test mode: stopping after 5 pages.")
            #     break
            # Stop naturally when no next page is available.
            nxt = has_next_page(page)
            if not nxt:
                print("No more pages (Next disabled or not found). Stopping.")
                break

            # Attempt to paginate; guard against infinite loop on click failure.
            clicked = False
            for nav_attempt in range(1, MAX_PAGE_NAV_RETRIES + 1):
                clicked = click_next(page)
                if clicked:
                    break

                print(f"Warning: failed to move to next page (attempt {nav_attempt}/{MAX_PAGE_NAV_RETRIES}).")
                try:
                    # Retry by explicitly calling paging function to target next page.
                    page.evaluate(
                        """(p) => {
                            if (typeof SearchItems === 'function') SearchItems('Paging', p);
                        }""",
                        page_num + 1,
                    )
                    time.sleep(1.5)
                except Exception:
                    pass

            if not clicked:
                msg = f"Failed to move past page {page_num} after {MAX_PAGE_NAV_RETRIES} attempts. Progress saved; rerun will resume."
                print(msg)
                notify_admin("Scraper: Pagination stalled", msg)
                save_checkpoint(
                    checkpoint_path=checkpoint_path,
                    next_page=page_num,
                    total_rows_saved=total_rows_saved,
                    pages_scraped=pages_scraped,
                )
                break

            # Wait for the active page number or product count to change to avoid extracting overlapping nodes
            old_active = get_active_page_number(page) or page_num
            old_count = count_product_rows(page)
            wait_start = time.time()
            while time.time() - wait_start < PRODUCT_WAIT_TIMEOUT:
                time.sleep(0.5)
                new_active = get_active_page_number(page)
                new_count = count_product_rows(page)
                # If active page number incremented, likely navigation succeeded
                try:
                    if new_active and int(new_active) > int(old_active):
                        break
                except Exception:
                    pass
                # If product count changed (and is non-zero), assume new content
                if new_count != old_count and new_count > 0:
                    break
            time.sleep(0.5)
            page_num += 1

        # Step 7: Final summary and quality alerts.
        print(f"\nFinished. Pages scraped: {pages_scraped}, total rows saved: {total_rows_saved}")
        print(f"Duplicates skipped across run: {duplicates_skipped}")

        # If run reached natural end, clear checkpoint to mark completion.
        if not has_next_page(page):
            clear_checkpoint(checkpoint_path)
        
        # Send alert if format deviations were detected (no logs)
        if all_deviations:
            send_format_deviation_alert(all_deviations, pages_scraped)

        try:
            detail_page.close()
        except Exception:
            pass
        
        browser.close()
        return output_csv

def run_scraper():
    """Wrapper for one full run, plus post-run notifications and local copy."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    artifact_copy_dir = resolve_artifact_copy_dir(script_dir)

    log_path, log_file, original_stdout, original_stderr = start_console_file_logging(log_dir=script_dir)

    output_csv = f"bestBuy_products.csv"
    try:
        # Run main scraping routine and get produced CSV path.
        scraped_file = main(output_csv)

        if scraped_file and os.path.exists(scraped_file):
            # API upload is temporarily disabled for now.
            # success = send_csv_to_api(scraped_file)
            # if not success:
            #     print("⚠️ CSV upload to API failed.")
            #     notify_admin(
            #         subject="Scraper: CSV upload failed",
            #         body="The scraper completed but failed to upload the CSV to the API."
            #     )

            # Save an extra local copy to artifact directory (Linux/Windows safe).
            try:
                local_copy_path = os.path.join(artifact_copy_dir, os.path.basename(scraped_file))
                shutil.copy2(scraped_file, local_copy_path)
                print(f"✅ Local CSV copy saved to: {local_copy_path}")
            except Exception as e:
                print(f"⚠️ Could not save local CSV copy to artifact directory: {e}")

            # Auto-run CSV -> JSON transformation after CSV is generated.
            transformed_json_path = None
            try:
                transformed_json_path = transform_csv_to_json(scraped_file, copy_to_downloads=False)
                print(f"✅ Product specifications JSON generated: {transformed_json_path}")

                json_copy_path = os.path.join(artifact_copy_dir, os.path.basename(transformed_json_path))
                shutil.copy2(transformed_json_path, json_copy_path)
                print(f"✅ JSON copy saved to: {json_copy_path}")
            except Exception as e:
                print(f"⚠️ CSV->JSON transformation failed: {e}")

            notify_admin(
                subject="Scraper Finished",
                body="Here’s the CSV from today’s run.",
                attachments=[scraped_file]   # attach the generated CSV
            )
            print("✅ Email sent with CSV attached.")
        else:
            print("⚠️ No scraped file produced, skipping email.")
    finally:
        stop_console_file_logging(log_file, original_stdout, original_stderr)
        try:
            log_copy_path = os.path.join(artifact_copy_dir, os.path.basename(log_path))
            shutil.copy2(log_path, log_copy_path)
            print(f"✅ Run log saved: {log_path}")
            print(f"✅ Run log copied to artifact directory: {log_copy_path}")
        except Exception as e:
            print(f"⚠️ Could not copy run log to artifact directory: {e}")


if __name__ == "__main__":
    run_scraper()