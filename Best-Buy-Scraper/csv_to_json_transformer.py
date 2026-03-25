import csv
import json
import os
import shutil
from collections import OrderedDict


def transform_csv_to_json(input_csv: str, output_json: str | None = None, copy_to_downloads: bool = True) -> str:
    """Transform scraper CSV into grouped JSON by SKU.

    Rules:
    - Use only `SKU` and `product_specifications` columns.
    - Skip rows with empty product_specifications.
    - Group by SKU, merging spec key/value pairs for repeated SKU rows.
    """
    if not input_csv or not os.path.exists(input_csv):
        raise FileNotFoundError(f"CSV not found: {input_csv}")

    if output_json is None:
        base_name = os.path.splitext(os.path.basename(input_csv))[0]
        output_json = os.path.join(os.path.dirname(input_csv), f"{base_name}_specifications.json")

    grouped_specs: dict[str, dict[str, str]] = OrderedDict()

    with open(input_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader, start=2):
            sku = (row.get("SKU") or "").strip()
            raw_specs = (row.get("product_specifications") or "").strip()

            # Skip products without specs, as requested.
            if not raw_specs:
                continue

            if not sku:
                print(f"csv_to_json: row {row_idx} has specs but missing SKU, skipping.")
                continue

            try:
                specs_obj = json.loads(raw_specs)
            except Exception as e:
                print(f"csv_to_json: row {row_idx} has invalid specs JSON for SKU={sku}: {e}")
                continue

            if not isinstance(specs_obj, dict):
                print(f"csv_to_json: row {row_idx} specs for SKU={sku} is not an object, skipping.")
                continue

            if sku not in grouped_specs:
                grouped_specs[sku] = {}

            # Merge specs for repeated SKUs.
            for key, value in specs_obj.items():
                if key is None:
                    continue
                clean_key = str(key).strip()
                if not clean_key:
                    continue
                grouped_specs[sku][clean_key] = "" if value is None else str(value).strip()

    output_payload = [
        {
            "sku": sku,
            "specifications": specs,
        }
        for sku, specs in grouped_specs.items()
        if specs
    ]

    with open(output_json, "w", encoding="utf-8") as out:
        json.dump(output_payload, out, ensure_ascii=False, indent=2)

    print(f"csv_to_json: wrote {len(output_payload)} product(s) to {output_json}")

    if copy_to_downloads:
        try:
            downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
            os.makedirs(downloads_dir, exist_ok=True)
            downloads_output = os.path.join(downloads_dir, os.path.basename(output_json))
            shutil.copy2(output_json, downloads_output)
            print(f"csv_to_json: copied JSON to Downloads: {downloads_output}")
        except Exception as e:
            print(f"csv_to_json: failed to copy JSON to Downloads: {e}")

    return output_json


if __name__ == "__main__":
    # Minimal CLI mode for ad-hoc manual runs.
    default_csv = os.path.join(os.path.dirname(__file__), "bestBuy_products.csv")
    try:
        transform_csv_to_json(default_csv)
    except Exception as e:
        print(f"csv_to_json: failed: {e}")
