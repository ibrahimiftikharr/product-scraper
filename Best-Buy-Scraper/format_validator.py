
import re
import os
from dotenv import load_dotenv
from email_notifier import notify_admin

# -----------------------------------------------------------------------------
# MODULE OVERVIEW
# -----------------------------------------------------------------------------
# Validation helpers in this file check whether scraped pages still match
# assumptions used by selectors/parsers. Instead of stopping the run, they
# collect deviations and trigger a single alert summary.

# Load environment variables
load_dotenv()

EXPECTED_PRICE_PATTERN = r'^\$\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?$'  # $123.45 or $1,234.56

def check_page_format_deviation(page, page_num, products, current_url):
    """
    Check for deviations in page format and send alerts if detected.
    Focuses on: Price format, Stock Status HTML elements, and SKU presence.
    Records any anomaly found (no percentage thresholds).
    Returns dict with deviation details.
    """
    # Unified structure used by downstream alert formatter.
    # `issues` contains human-readable lines that can be dropped directly
    # into email content without additional formatting.
    deviations = {
        "has_deviation": False,
        "issues": []
    }
    
    # Check 1: Verify H5 elements are present (used for item/price headers).
    # If this fails, major card structure may have changed.
    try:
        has_h5_elements = page.evaluate("""() => {
            const h5s = document.querySelectorAll('h5');
            return h5s.length > 0;
        }""")
        
        if not has_h5_elements:
            deviations["issues"].append(f"Page {page_num}: Missing H5 elements (used for SKU/price headers)")
            deviations["has_deviation"] = True
    except Exception as e:
        deviations["issues"].append(f"Page {page_num}: Error checking H5 elements: {str(e)}")
        deviations["has_deviation"] = True
    
    # Check 2: Verify stock status elements are present (span.language-english).
    # Missing status spans often indicates CSS/DOM refactor in listing cards.
    try:
        has_status_elements = page.evaluate("""() => {
            const spans = document.querySelectorAll('span.language-english');
            return spans.length > 0;
        }""")
        
        if not has_status_elements:
            deviations["issues"].append(f"Page {page_num}: Missing span.language-english elements (used for stock status)")
            deviations["has_deviation"] = True
    except Exception as e:
        deviations["issues"].append(f"Page {page_num}: Error checking status elements: {str(e)}")
        deviations["has_deviation"] = True
    
    # Check 3: Product-level data presence checks.
    # Missing SKU/Price rows are actionable because these fields drive output.
    if products:
        missing_sku_products = [p for p in products if not p.get("sku")]
        missing_price_products = [p for p in products if not p.get("price")]
        
        if missing_sku_products:
            count = len(missing_sku_products)
            # Show first 5 examples with index/price info to speed debugging.
            examples = missing_sku_products[:5]
            example_str = ", ".join([f"Product {i+1} (Price: {p.get('price', 'N/A')})" for i, p in enumerate(examples)])
            deviations["issues"].append(
                f"Page {page_num}: Found {count} product(s) with missing SKU. Examples: {example_str}"
            )
            deviations["has_deviation"] = True
        
        if missing_price_products:
            count = len(missing_price_products)
            # Show first 5 examples with SKU info to speed debugging.
            examples = missing_price_products[:5]
            example_str = ", ".join([f"SKU: {p.get('sku', 'N/A')}" for p in examples])
            deviations["issues"].append(
                f"Page {page_num}: Found {count} product(s) with missing Price. SKUs: {example_str}"
            )
            deviations["has_deviation"] = True
        
        # Check 4: Validate price format consistency for extracted values.
        # This catches subtle extraction drift where values are present but malformed.
        invalid_prices = []
        for i, p in enumerate(products):
            price = p.get("price")
            if price:
                # Check if price matches expected format
                if not re.match(EXPECTED_PRICE_PATTERN, price.strip()):
                    invalid_prices.append({
                        "sku": p.get("sku", "Unknown"),
                        "price": price,
                        "index": i
                    })
        
        if invalid_prices:
            sample = invalid_prices[:5]  # Show first 5 examples
            sample_str = ", ".join([f"'{p['price']}' (SKU: {p['sku']})" for p in sample])
            deviations["issues"].append(
                f"Page {page_num}: Found {len(invalid_prices)} product(s) with unexpected price format. Examples: {sample_str}"
            )
            deviations["has_deviation"] = True
    else:
        deviations["issues"].append(f"Page {page_num}: No products extracted")
        deviations["has_deviation"] = True
    
    return deviations


def send_format_deviation_alert(deviations_list, total_pages):
    """Send a consolidated alert summarizing all format deviations found."""
    if not deviations_list:
        return
    
    # Subject is intentionally stable to simplify mailbox filtering/rules.
    subject = "⚠️ Best Buy Scraper: Page Format Deviations Detected"
    
    message_lines = [
        "The Best Buy scraper has detected deviations from the expected page format.",
        f"Total pages scraped: {total_pages}",
        f"Pages with issues: {len(deviations_list)}",
        "",
        "DETECTED ISSUES:",
        "=" * 60,
        ""
    ]
    
    # Flatten all issues into one readable message body.
    # Keeping one email per run avoids alert fatigue.
    for deviation in deviations_list:
        for issue in deviation["issues"]:
            message_lines.append(f"• {issue}")
    
    message_lines.extend([
        "",
        "=" * 60,
        "",
        "RECOMMENDED ACTIONS:",
        "1. Check if Best Buy website UI has changed",
        "2. Review the HTML structure of product pages",
        "3. Update scraper selectors if needed",
        "4. Verify data extraction logic",
        "",
        "The scraper continued running but data quality may be affected.",
    ])
    
    message = "\n".join(message_lines)
    
    try:
        notify_admin(subject, message)
    except Exception as e:
        # Alert failures should never break scraper completion path.
        pass


def validate_price_parsing(products_sample, parse_price_func):
    """
    Validate that prices can be parsed correctly.
    Returns list of issues found.
    """
    issues = []
    
    # Lightweight sanity check on a small sample for runtime safety.
    # This is a fast early signal, not a full validation pass.
    for prod in products_sample[:10]:  # Check first 10 products
        price_str = prod.get("price")
        if price_str:
            parsed = parse_price_func(price_str)
            if parsed is None:
                issues.append(f"Failed to parse price '{price_str}' for SKU: {prod.get('sku', 'Unknown')}")
            elif parsed <= 0:
                issues.append(f"Invalid price value {parsed} for SKU: {prod.get('sku', 'Unknown')}")
    
    return issues
