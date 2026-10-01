#!/usr/bin/env python3
"""Combine monthly form-fill CSVs into one date-range file.

The monthly reports from form_fills_month_get.py each carry a different set of
columns, because every month contains different form fields. Concatenating them
naively would misalign rows, so this takes the union of columns in the standard
report order and leaves a cell blank where a month never saw that field.

Reads nothing from Marketo — pure local file work.

Run:
    python combine_form_fills.py --months 2026-06 2026-07 2026-08
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from form_config import order_columns


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--months", nargs="+", required=True, help="e.g. 2026-06 2026-07 2026-08")
    ap.add_argument("--out-dir", default="output")
    ap.add_argument("--out-name", help="Defaults to form_fills_<first>_to_<last>.csv")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    paths = [out_dir / f"form_fills_{m}.csv" for m in args.months]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        raise SystemExit("missing input file(s): " + ", ".join(str(p) for p in missing))

    rows: list[dict] = []
    seen_cols: list[str] = []
    per_month: list[tuple[str, int]] = []

    for month, path in zip(args.months, paths):
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for col in reader.fieldnames or []:
                if col not in seen_cols:
                    seen_cols.append(col)
            month_rows = list(reader)
        for row in month_rows:
            row["source_month"] = month
        rows.extend(month_rows)
        per_month.append((month, len(month_rows)))
        print(f"  {path.name}: {len(month_rows):,} rows, {len(reader.fieldnames or [])} cols")

    # Standard report order, then the provenance column.
    cols = order_columns(seen_cols) + ["source_month"]

    rows.sort(key=lambda r: r.get("activityDate") or "")

    name = args.out_name or f"form_fills_{args.months[0]}_to_{args.months[-1]}.csv"
    out_path = out_dir / name
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    total = sum(n for _m, n in per_month)
    print(f"\nCombined {len(per_month)} months, {total:,} rows, {len(cols)} columns")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
