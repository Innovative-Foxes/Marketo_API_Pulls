#!/usr/bin/env python3
"""Monthly form-fill report using ONLY GET requests.

Pulls "Fill Out Form" (activity type 2) activities for a calendar month via the
REST Activities API and writes a CSV with one row per submission, using the same
column layout as form_fill_report.py.

GET-ONLY BY DESIGN
------------------
The Bulk Activity Extract API used by form_fill_report.py requires POST
(export create + enqueue). This script deliberately avoids it and uses only:

    GET /identity/oauth/token          (auth)
    GET /rest/v1/activities/pagingtoken.json
    GET /rest/v1/activities.json

No POST/PUT/DELETE is issued anywhere. Nothing is written back to Marketo.

Differences vs. the bulk endpoint, handled here:
  * `attributes` arrives as a list of {name, value} (bulk returns a JSON string)
  * `campaignId` is not returned by this endpoint, so the column is emitted
    empty to keep the layout identical to the bulk report.

Run:
    python form_fills_month_get.py --year 2026 --month 7
    python form_fills_month_get.py --year 2026 --month 7 --out-dir output
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import time
from pathlib import Path

import phpserialize
import requests

from auth import MarketoAuth, load_config, log
from form_config import order_columns

FILL_OUT_FORM_TYPE_ID = 2
BATCH_SIZE = 300
# Marketo codes meaning "slow down" / "token expired" -> back off and retry.
RETRY_CODES = {"602", "604", "606", "615", "614"}
MAX_RETRIES = 6

def stringify(v) -> str:
    """CSV-friendly value: no None, no float artifacts on whole numbers."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def parse_form_fields(raw: str) -> dict:
    """Explode Marketo's PHP-serialized 'Form Fields' blob into a dict.

    String lengths are byte counts, so parse from UTF-8 bytes to keep multibyte
    values (curly apostrophes etc.) intact.
    """
    if not raw:
        return {}
    try:
        d = phpserialize.loads(str(raw).encode("utf-8"), decode_strings=True)
    except Exception:  # noqa: BLE001 - never lose data on a parse failure
        return {"Form Fields (unparsed)": str(raw)}
    if not isinstance(d, dict):
        return {"Form Fields (unparsed)": str(raw)}
    return {stringify(k): stringify(v) for k, v in d.items()}


def flatten(rec: dict) -> dict:
    """One activity record -> flat dict of named values."""
    flat = {
        "leadId": stringify(rec.get("leadId")),
        "activityDate": stringify(rec.get("activityDate")),
        "formName": stringify(rec.get("primaryAttributeValue")),
        "marketoGUID": stringify(rec.get("marketoGUID")),
        # Not returned by the REST activities endpoint; kept for layout parity.
        "campaignId": "",
    }

    # attributes: list of {"name": ..., "value": ...}
    attrs = {}
    for a in rec.get("attributes") or []:
        if isinstance(a, dict) and "name" in a:
            attrs[a["name"]] = a.get("value")

    for k, v in parse_form_fields(attrs.pop("Form Fields", "")).items():
        flat[k if k not in flat else f"field_{k}"] = v
    for k, v in attrs.items():
        flat[k if k not in flat else f"meta_{k}"] = stringify(v)
    return flat


def _get(auth: MarketoAuth, url: str, params: dict) -> dict:
    """GET with token refresh + backoff on throttle codes. GET requests only."""
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, headers=auth.auth_header(),
                             params=params, timeout=60).json()
        except Exception as e:  # noqa: BLE001 - transient network, retry
            log(f"    request error ({e}); retry {attempt + 1}")
            time.sleep(3 + attempt * 3)
            continue
        if r.get("success"):
            return r
        codes = {str(e.get("code")) for e in r.get("errors", [])}
        if codes & RETRY_CODES:
            wait = 5 + attempt * 5
            log(f"    Marketo {codes}; waiting {wait}s")
            if "602" in codes:
                auth.refresh()
            time.sleep(wait)
            continue
        raise RuntimeError(f"GET {url} failed: {r.get('errors')}")
    raise RuntimeError(f"GET {url} exhausted retries")


def month_bounds(year: int, month: int):
    start = _dt.date(year, month, 1)
    end = _dt.date(year + 1, 1, 1) if month == 12 else _dt.date(year, month + 1, 1)
    return (f"{start.isoformat()}T00:00:00Z", f"{end.isoformat()}T00:00:00Z")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Monthly Marketo form-fill CSV (GET only).")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--month", type=int, required=True)
    ap.add_argument("--out-dir", default="output")
    args = ap.parse_args(argv)

    cfg = load_config()
    auth = MarketoAuth(**cfg)
    base = cfg["rest_base"]

    start_at, end_at = month_bounds(args.year, args.month)
    log(f"[range] {start_at}  ->  {end_at}   (UTC, end exclusive)")

    tok = _get(auth, f"{base}/rest/v1/activities/pagingtoken.json",
               {"sinceDatetime": start_at}).get("nextPageToken")
    if not tok:
        raise RuntimeError("no paging token returned")

    rows, keys = [], set()
    calls, skipped_after_end = 0, 0
    while True:
        r = _get(auth, f"{base}/rest/v1/activities.json", {
            "nextPageToken": tok,
            "activityTypeIds": FILL_OUT_FORM_TYPE_ID,
            "batchSize": BATCH_SIZE,
        })
        calls += 1
        batch = r.get("result", []) or []
        past_end = False
        for rec in batch:
            if stringify(rec.get("activityDate")) >= end_at:
                past_end = True
                skipped_after_end += 1
                continue
            flat = flatten(rec)
            keys.update(flat)
            rows.append(flat)
        log(f"    call {calls}: {len(batch)} returned, kept {len(rows)} total")
        tok = r.get("nextPageToken") or tok
        if past_end or not r.get("moreResult"):
            break

    # Every field seen becomes a column; nothing is dropped.
    cols = order_columns(keys)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"form_fills_{args.year}-{args.month:02d}.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    log(f"\nGET calls issued: {calls + 2}  (0 POST/PUT/DELETE)")
    log(f"Rows: {len(rows):,}  Columns: {len(cols)}")
    log(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
