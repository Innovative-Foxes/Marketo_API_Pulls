#!/usr/bin/env python3
"""Definitive list of emails SENT this year, with their asset IDs.

Uses the Marketo Bulk Activity Extract API to pull "Send Email" (activity type
6) records for the year, then dedupes by the email's asset ID
(primaryAttributeValueId). This is the clean single source: every send record
carries both the email name (primaryAttributeValue, in Marketo's
"Program.EmailName" form) AND the unambiguous asset ID — so unlike a name-based
join, there is no guessing.

Because a full year of sends exceeds the 500 MB/day bulk-export quota, the year
is pulled in monthly chunks (each <= the 31-day range cap). Each export file is
*streamed* and reduced to distinct (id -> name, send count) on the fly, so we
never hold millions of rows in memory. Partial progress is written after every
chunk, so a quota cut-off still leaves a usable file.

Output: emails_sent_<YEAR>.csv with columns
    email_id,email_name,program,sent_count,operational,fromEmail,domain,url

Run:
    python sent_emails_by_id.py
    python sent_emails_by_id.py --year 2026 --activity sent   # or: delivered
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import tempfile
import time
from pathlib import Path

import requests

from auth import MarketoAuth, load_config, log
from marketo_email_report import fetch_all_emails, build_asset_record, resolve_email_name, _norm

ACTIVITY_IDS = {"sent": 6, "delivered": 7}

POLL_INTERVAL_S = 8
POLL_TIMEOUT_S = 1800  # 30 min per chunk
# Bulk export error codes that mean "you're out of daily quota / too many jobs".
QUOTA_ERROR_CODES = {"1029"}


def _json_headers(auth: MarketoAuth) -> dict:
    return {**auth.auth_header(), "Content-Type": "application/json"}


def month_ranges(year: int, today: _dt.date) -> list[tuple[str, str]]:
    """Month-by-month [startAt, endAt) UTC strings from Jan 1 to today (cap 31d)."""
    ranges = []
    start = _dt.date(year, 1, 1)
    # don't run past the end of the requested year
    final = today if today.year == year else _dt.date(year, 12, 31)
    while start <= final:
        if start.month == 12:
            nxt = _dt.date(year + 1, 1, 1)
        else:
            nxt = _dt.date(year, start.month + 1, 1)
        end = min(nxt, final + _dt.timedelta(days=1))
        ranges.append(
            (f"{start.isoformat()}T00:00:00Z", f"{end.isoformat()}T00:00:00Z")
        )
        start = nxt
    return ranges


def run_chunk(auth: MarketoAuth, base: str, type_id: int, start_at: str, end_at: str,
              acc: dict) -> int:
    """Create/enqueue/poll/stream one monthly export; fold results into acc.

    acc maps email_id -> {"name": label, "count": n}. Returns rows processed.
    """
    body = {
        "filter": {"activityTypeIds": [type_id],
                   "createdAt": {"startAt": start_at, "endAt": end_at}},
        "format": "CSV",
    }
    r = requests.post(f"{base}/bulk/v1/activities/export/create.json",
                      headers=_json_headers(auth), json=body, timeout=60).json()
    if not r.get("success"):
        codes = {str(e.get("code")) for e in r.get("errors", [])}
        if codes & QUOTA_ERROR_CODES:
            raise QuotaExceeded(r.get("errors"))
        raise RuntimeError(f"create failed: {r.get('errors')}")
    xid = r["result"][0]["exportId"]

    eq = requests.post(f"{base}/bulk/v1/activities/export/{xid}/enqueue.json",
                       headers=_json_headers(auth), timeout=60).json()
    if not eq.get("success"):
        codes = {str(e.get("code")) for e in eq.get("errors", [])}
        if codes & QUOTA_ERROR_CODES:
            raise QuotaExceeded(eq.get("errors"))
        raise RuntimeError(f"enqueue failed: {eq.get('errors')}")

    waited = 0
    while waited < POLL_TIMEOUT_S:
        time.sleep(POLL_INTERVAL_S)
        waited += POLL_INTERVAL_S
        st = requests.get(f"{base}/bulk/v1/activities/export/{xid}/status.json",
                          headers=_json_headers(auth), timeout=60).json()
        row = st["result"][0]
        status = row["status"]
        if status == "Completed":
            log(f"    completed: {row.get('numberOfRecords'):,} rows, "
                f"{(row.get('fileSize') or 0)/1e6:.1f} MB")
            break
        if status in ("Failed", "Cancelled"):
            raise RuntimeError(f"export {status}: {row}")
    else:
        raise RuntimeError(f"export timed out after {POLL_TIMEOUT_S}s")

    # Download to a temp file (binary, chunked, with retries) then parse it.
    # The same exportId can be re-downloaded, so retries never re-extract.
    file_url = f"{base}/bulk/v1/activities/export/{xid}/file.json"
    tmp = Path(tempfile.gettempdir()) / f"mkto_export_{xid}.csv"
    _download_with_retries(auth, file_url, tmp)

    processed = 0
    with tmp.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        cols = next(reader, None)
        if not cols:
            tmp.unlink(missing_ok=True)
            return 0
        idx_id = cols.index("primaryAttributeValueId")
        idx_name = cols.index("primaryAttributeValue")
        for parsed in reader:
            processed += 1
            if len(parsed) <= max(idx_id, idx_name):
                continue
            eid = parsed[idx_id].strip()
            if not eid:
                continue
            entry = acc.get(eid)
            if entry is None:
                acc[eid] = {"name": parsed[idx_name].strip(), "count": 1}
            else:
                entry["count"] += 1
    tmp.unlink(missing_ok=True)
    return processed


def _download_with_retries(auth: MarketoAuth, url: str, dest: Path, retries: int = 4) -> None:
    """Stream a bulk export file to `dest` in binary chunks, retrying on errors."""
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, headers=auth.auth_header(),
                              timeout=(30, 180), stream=True) as resp:
                resp.raise_for_status()
                with dest.open("wb") as out:
                    for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MB
                        if chunk:
                            out.write(chunk)
            log(f"    downloaded {dest.stat().st_size/1e6:.1f} MB")
            return
        except (requests.exceptions.RequestException, OSError) as exc:
            if attempt >= retries:
                raise
            wait = 5 * attempt
            log(f"    download attempt {attempt} failed ({exc}); retrying in {wait}s")
            time.sleep(wait)


class QuotaExceeded(Exception):
    pass


def load_state(path: Path) -> tuple[dict, set]:
    """Load resume state: (acc {id->{name,count}}, set of done range-keys)."""
    if not path.is_file():
        return {}, set()
    data = json.loads(path.read_text())
    return data.get("acc", {}), set(data.get("done_ranges", []))


def save_state(path: Path, acc: dict, done_ranges: set) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"acc": acc, "done_ranges": sorted(done_ranges)}))
    os.replace(tmp, path)  # atomic; survives mid-write interruption


def write_output(path: Path, acc: dict, assets_by_id: dict, by_name: dict) -> None:
    """Write the deduped emails-sent list, enriched from the asset map."""
    header = ["email_id", "email_name", "program", "sent_count",
              "operational", "fromEmail", "domain", "url"]
    rows = []
    for eid, info in acc.items():
        asset = assets_by_id.get(eid)
        # program prefix: strip the resolved email name off the activity label
        program, _name, _c = resolve_email_name(info["name"], by_name)
        if asset:
            email_name = asset["name"]
            rows.append({
                "email_id": eid, "email_name": email_name, "program": program,
                "sent_count": info["count"], "operational": asset["operational"],
                "fromEmail": asset["fromEmail"], "domain": asset["domain"],
                "url": asset["url"],
            })
        else:
            # ID not in the asset pull (rare) — fall back to the activity label.
            label = info["name"]
            email_name = label.rsplit(".", 1)[-1] if "." in label else label
            rows.append({
                "email_id": eid, "email_name": email_name,
                "program": label[: -len(email_name) - 1] if "." in label else "",
                "sent_count": info["count"], "operational": "",
                "fromEmail": "", "domain": "", "url": "",
            })
    rows.sort(key=lambda r: r["sent_count"], reverse=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    log(f"[output] wrote {len(rows)} distinct emails -> {path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="List emails sent this year with asset IDs (Bulk Activity Extract).")
    parser.add_argument("--year", type=int, default=_dt.date.today().year)
    parser.add_argument("--activity", choices=list(ACTIVITY_IDS), default="sent",
                        help="Which activity to extract: 'sent' (type 6) or 'delivered' (type 7).")
    parser.add_argument("--out-dir", type=Path, default=Path("output"),
                        help="Output directory. Default: output")
    args = parser.parse_args(argv)

    cfg = load_config(need_app_base=True)
    auth = MarketoAuth(**cfg)
    base = cfg["rest_base"]
    type_id = ACTIVITY_IDS[args.activity]

    # Asset map for enrichment (id -> record, and name -> [records] for program parse).
    log("[assets] pulling email asset metadata for enrichment ...")
    assets = [build_asset_record(e, cfg["app_base"], "A1") for e in fetch_all_emails(auth)]
    assets_by_id = {str(a["id"]): a for a in assets}
    by_name: dict = {}
    for a in assets:
        if a["name"]:
            by_name.setdefault(_norm(a["name"]), []).append(a)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"emails_sent_{args.year}.csv"
    state_path = args.out_dir / f"emails_sent_{args.year}.state.json"

    # Resume: load prior accumulator + which month-ranges already finished.
    acc, done_ranges = load_state(state_path)
    if done_ranges:
        log(f"[resume] {len(acc)} emails carried over; {len(done_ranges)} chunk(s) already done")

    ranges = month_ranges(args.year, _dt.date.today())
    total_rows = 0
    completed = len(done_ranges)
    for i, (start_at, end_at) in enumerate(ranges, 1):
        key = f"{start_at}|{end_at}"
        if key in done_ranges:
            log(f"[chunk {i}/{len(ranges)}] {start_at[:10]} .. {end_at[:10]}  (already done, skipping)")
            continue
        log(f"[chunk {i}/{len(ranges)}] {start_at[:10]} .. {end_at[:10]}")
        try:
            total_rows += run_chunk(auth, base, type_id, start_at, end_at, acc)
            done_ranges.add(key)
            completed += 1
            save_state(state_path, acc, done_ranges)        # checkpoint state (resumable)
            write_output(out_path, acc, assets_by_id, by_name)  # checkpoint CSV
            log(f"    distinct emails so far: {len(acc)}")
        except QuotaExceeded as e:
            log(f"!! daily bulk-export quota hit: {e}. Stopping with partial results.")
            log(f"   Re-run to continue (chunks {i}..{len(ranges)} remain).")
            break

    save_state(state_path, acc, done_ranges)
    write_output(out_path, acc, assets_by_id, by_name)
    log("")
    log("================ SUMMARY ================")
    log(f"  Activity ................. {args.activity} (type {type_id})")
    log(f"  Chunks completed ......... {completed}/{len(ranges)}")
    log(f"  Send records processed ... {total_rows:,}")
    log(f"  Distinct emails sent ..... {len(acc)}")
    log(f"  Output ................... {out_path}")
    log("========================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
