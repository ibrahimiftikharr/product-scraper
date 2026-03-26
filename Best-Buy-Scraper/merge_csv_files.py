import csv
import glob
import os
import argparse


def merge_csv_files(output_file="bestBuy_products_merged.csv", input_pattern="*.csv"):
    """Merge all CSV files in the current folder into one output CSV."""
    output_abs = os.path.abspath(output_file)

    input_files = []
    for file_path in sorted(glob.glob(input_pattern)):
        if os.path.abspath(file_path) == output_abs:
            continue
        if os.path.isfile(file_path):
            input_files.append(file_path)

    if not input_files:
        raise FileNotFoundError(f"No input CSV files found for pattern: {input_pattern}")

    merged_rows = []
    fieldnames = []
    fieldname_set = set()

    for csv_file in input_files:
        with open(csv_file, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                print(f"merge_csv: skipping empty/headerless file: {csv_file}")
                continue

            for name in reader.fieldnames:
                if name not in fieldname_set:
                    fieldname_set.add(name)
                    fieldnames.append(name)

            row_count = 0
            for row in reader:
                merged_rows.append(row)
                row_count += 1

            print(f"merge_csv: read {row_count} row(s) from {csv_file}")

    if not fieldnames:
        raise ValueError("No valid CSV headers found in input files.")

    with open(output_file, "w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in merged_rows:
            normalized = {name: row.get(name, "") for name in fieldnames}
            writer.writerow(normalized)

    print(f"merge_csv: wrote {len(merged_rows)} total row(s) to {output_file}")
    return output_file


def parse_args():
    parser = argparse.ArgumentParser(description="Merge CSV files in the current folder.")
    parser.add_argument(
        "--output",
        default="bestBuy_products_merged.csv",
        help="Output CSV filename (default: bestBuy_products_merged.csv)",
    )
    parser.add_argument(
        "--pattern",
        default="*.csv",
        help="Input glob pattern (default: *.csv)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    merge_csv_files(output_file=args.output, input_pattern=args.pattern)
