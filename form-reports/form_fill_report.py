#!/usr/bin/env python3
"""Marketo form-fill report (bulk export).

Pulls every "Fill Out Form" activity for one or more years from the Marketo
Bulk Activity Extract API and writes one CSV per year, with one row per form
submission and a clean labeled header. Each Fill Out Form activity carries an
`attributes` JSON blob holding the values the visitor submitted plus form
metadata (form id, the page the form was filled out on, the referrer, etc.).
We flatten the union of all attribute keys seen into columns, so every field
across every form is represented (blank where a given form didn't have it).

Output columns (per year), in this order:
    leadId, activityDate, formName,
    <page-URL fields: your FORM_PAGE_URL_FIELDS, then Webpage URL, Referrer URL>,
    <every other submitted field / attribute, alphabetical>,
    <activity metadata>, campaignId, marketoGUID

Run (defaults to the current year):
    python form_fill_report.py
    python form_fill_report.py --year 2025
    python form_fill_report.py --year 2025 --year 2026 --out-dir ./output
    python form_fill_report.py --probe            # tiny pull; print discovered columns, write nothing

Quota + resume: a year is pulled in monthly chunks. State is checkpointed to
output/.work/form_fills_<year>.state.json after every chunk and flattened rows
are streamed to output/.work/form_fills_<year>.jsonl. If the daily bulk-export
quota is hit (error 1029), the run stops cleanly with partial progress saved --
just re-run the same command after the quota resets and it continues.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import sys
import tempfile
import time
from pathlib import Path

import phpserialize
import requests

from auth import MarketoAuth, load_config, log
from form_config import order_columns

# "Fill Out Form" is activity type 2 on every Marketo instance, but we still
# look it up by name at runtime and fall back to this if the lookup fails.
FILL_OUT_FORM_FALLBACK_ID = 2
FILL_OUT_FORM_NAME = "Fill Out Form"

POLL_INTERVAL_S = 8
POLL_TIMEOUT_S = 1800  # 30 min per chunk
QUOTA_ERROR_CODES = {"1029"}  # daily bulk-export quota / too many jobs

# How many days the probe pull spans.
PROBE_DAYS = 2


class QuotaExceeded(RuntimeError):
    """Raised when Marketo reports the daily bulk-export quota is exhausted."""


def _json_headers(auth: MarketoAuth) -> dict:
    return {**auth.auth_header(), "Content-Type": "application/json"}


def resolve_activity_type_id(auth: MarketoAuth, base: str) -> int:
    """Look up the numeric id for the 'Fill Out Form' activity type."""
    try:
        r = requests.get(
            f"{base}/rest/v1/activities/types.json",
            headers=auth.auth_header(),
            timeout=60,
        ).json()
        if r.get("success"):
            for t in r.get("result", []):
                if t.get("name", "").strip().lower() == FILL_OUT_FORM_NAME.lower():
                    log(f"[types] '{FILL_OUT_FORM_NAME}' = activity type {t['id']}")
                    return int(t["id"])
        log(f"[types] '{FILL_OUT_FORM_NAME}' not found in types list; "
            f"using fallback id {FILL_OUT_FORM_FALLBACK_ID}")
    except Exception as e:  # noqa: BLE001 - non-fatal, we have a fallback
        log(f"[types] lookup failed ({e}); using fallback id {FILL_OUT_FORM_FALLBACK_ID}")
    return FILL_OUT_FORM_FALLBACK_ID


def month_ranges(year: int, today: _dt.date) -> list[tuple[str, str, str]]:
    """Monthly [startAt, endAt) UTC windows for the year (each <= 31 days).

    Returns (key, start_iso, end_iso) where key is e.g. '2025-03'. The window
    never extends past `today` for the current year.
    """
    ranges: list[tuple[str, str, str]] = []
    start = _dt.date(year, 1, 1)
    final = today if today.year == year else _dt.date(year, 12, 31)
    if start > final:
        return ranges
    while start <= final:
        nxt = _dt.date(year + 1, 1, 1) if start.month == 12 else _dt.date(year, start.month + 1, 1)
        end = min(nxt, final + _dt.timedelta(days=1))
        ranges.append(
            (f"{year}-{start.month:02d}",
             f"{start.isoformat()}T00:00:00Z",
             f"{end.isoformat()}T00:00:00Z")
        )
        start = nxt
    return ranges


def _create_export(auth: MarketoAuth, base: str, type_id: int,
                   start_at: str, end_at: str) -> str:
    body = {
        "filter": {
            "activityTypeIds": [type_id],
            "createdAt": {"startAt": start_at, "endAt": end_at},
        },
        "format": "CSV",
    }
    r = requests.post(
        f"{base}/bulk/v1/activities/export/create.json",
        headers=_json_headers(auth), json=body, timeout=60,
    ).json()
    if not r.get("success"):
        codes = {str(e.get("code")) for e in r.get("errors", [])}
        if codes & QUOTA_ERROR_CODES:
            raise QuotaExceeded(r.get("errors"))
        raise RuntimeError(f"create failed: {r.get('errors')}")
    return r["result"][0]["exportId"]


def _enqueue(auth: MarketoAuth, base: str, xid: str) -> None:
    eq = requests.post(
        f"{base}/bulk/v1/activities/export/{xid}/enqueue.json",
        headers=_json_headers(auth), timeout=60,
    ).json()
    if not eq.get("success"):
        codes = {str(e.get("code")) for e in eq.get("errors", [])}
        if codes & QUOTA_ERROR_CODES:
            raise QuotaExceeded(eq.get("errors"))
        raise RuntimeError(f"enqueue failed: {eq.get('errors')}")


def _wait_for_completion(auth: MarketoAuth, base: str, xid: str) -> None:
    waited = 0
    while waited < POLL_TIMEOUT_S:
        time.sleep(POLL_INTERVAL_S)
        waited += POLL_INTERVAL_S
        st = requests.get(
            f"{base}/bulk/v1/activities/export/{xid}/status.json",
            headers=_json_headers(auth), timeout=60,
        ).json()
        row = st["result"][0]
        status = row["status"]
        if status == "Completed":
            n = row.get("numberOfRecords") or 0
            mb = (row.get("fileSize") or 0) / 1e6
            log(f"    completed: {n:,} rows, {mb:.1f} MB")
            return
        if status in ("Failed", "Cancelled"):
            raise RuntimeError(f"export {status}: {row}")
    raise RuntimeError(f"export timed out after {POLL_TIMEOUT_S}s")


def _download(auth: MarketoAuth, base: str, xid: str) -> Path:
    """Stream the completed export to a temp file (binary, chunked, retried)."""
    file_url = f"{base}/bulk/v1/activities/export/{xid}/file.json"
    tmp = Path(tempfile.gettempdir()) / f"marketo_formfill_{xid}.csv"
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with requests.get(file_url, headers=auth.auth_header(),
                              stream=True, timeout=300) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        if chunk:
                            fh.write(chunk)
            return tmp
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"    download attempt {attempt + 1} failed ({e}); retrying")
            time.sleep(5)
    raise RuntimeError(f"download failed after retries: {last_err}")


def _stringify(v) -> str:
    """Coerce a parsed value to a CSV-friendly string (nested -> JSON)."""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def _parse_form_fields(raw: str) -> dict:
    """Explode Marketo's PHP-serialized 'Form Fields' attribute into a dict.

    Marketo packs every value the visitor submitted (FirstName, Email,
    formid, checksum, ...) into a
    single PHP-serialized array string, e.g.
        a:26:{s:9:"FirstName";s:12:"Pablo Andres";...}
    String lengths are *byte* counts, so we parse from UTF-8 bytes to handle
    multibyte values (e.g. curly apostrophes) correctly.
    """
    if not raw:
        return {}
    try:
        d = phpserialize.loads(raw.encode("utf-8"), decode_strings=True)
    except Exception:  # noqa: BLE001 - keep the raw blob rather than lose data
        return {"Form Fields (unparsed)": raw}
    if not isinstance(d, dict):
        return {"Form Fields (unparsed)": raw}
    return {(_stringify(k)): _stringify(v) for k, v in d.items()}


def _flatten_row(rec: dict) -> dict:
    """Turn one raw activity-export row into a flat dict of named values.

    The activity export's fixed columns are:
        marketoGUID, leadId, activityDate, activityTypeId, campaignId,
        primaryAttributeValue, primaryAttributeValueId, attributes
    For Fill Out Form: primaryAttributeValue = form name and `attributes` is a
    JSON object holding the PHP-serialized 'Form Fields' blob plus activity
    metadata (Referrer URL, Webpage ID, Client IP Address, User Agent, ...).
    """
    camp = rec.get("campaignId", "")
    flat: dict[str, str] = {
        "leadId": rec.get("leadId", ""),
        "activityDate": rec.get("activityDate", ""),
        "formName": rec.get("primaryAttributeValue", ""),
        "campaignId": "" if camp in ("null", None) else camp,
        "marketoGUID": rec.get("marketoGUID", ""),
    }

    attrs: dict = {}
    raw_attrs = rec.get("attributes", "")
    if raw_attrs:
        try:
            parsed = json.loads(raw_attrs)
            if isinstance(parsed, dict):
                attrs = parsed
        except (ValueError, TypeError):
            flat["attributes_raw"] = str(raw_attrs)

    # Explode the submitted form fields into individual columns (the core data).
    for k, v in _parse_form_fields(attrs.pop("Form Fields", "")).items():
        flat[k if k not in flat else f"field_{k}"] = v

    # Keep the remaining activity metadata (Referrer URL, Webpage ID, IP, etc.).
    for k, v in attrs.items():
        flat[k if k not in flat else f"meta_{k}"] = _stringify(v)

    return flat


def run_chunk(auth: MarketoAuth, base: str, type_id: int,
              start_at: str, end_at: str, jsonl_fh, keys: set) -> int:
    """Create/enqueue/poll/stream one monthly export; append flat rows to jsonl.

    Updates `keys` with every column seen. Returns the number of rows written.
    """
    xid = _create_export(auth, base, type_id, start_at, end_at)
    log(f"    exportId {xid}")
    _enqueue(auth, base, xid)
    _wait_for_completion(auth, base, xid)
    tmp = _download(auth, base, xid)

    n = 0
    try:
        with open(tmp, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for rec in reader:
                flat = _flatten_row(rec)
                keys.update(flat.keys())
                jsonl_fh.write(json.dumps(flat, ensure_ascii=False) + "\n")
                n += 1
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    jsonl_fh.flush()
    return n


def _write_year_csv(jsonl_path: Path, keys: set, out_path: Path) -> int:
    """Read the per-year jsonl and write the final labeled CSV. Returns rows."""
    cols = order_columns(keys)
    n = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as out, \
            open(jsonl_path, encoding="utf-8") as src:
        writer = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for line in src:
            line = line.strip()
            if not line:
                continue
            writer.writerow(json.loads(line))
            n += 1
    return n


def process_year(auth: MarketoAuth, base: str, type_id: int, year: int,
                 today: _dt.date, out_dir: Path, work_dir: Path) -> Path | None:
    """Pull a full year (resumable) and write output/form_fills_<year>.csv."""
    ranges = month_ranges(year, today)
    if not ranges:
        log(f"[{year}] no date range to pull (year is in the future); skipping")
        return None

    state_path = work_dir / f"form_fills_{year}.state.json"
    jsonl_path = work_dir / f"form_fills_{year}.jsonl"
    state = {"completed": [], "keys": []}
    if state_path.is_file():
        state = json.loads(state_path.read_text())
        log(f"[{year}] resuming; {len(state['completed'])} month(s) already done")

    completed = set(state.get("completed", []))
    keys = set(state.get("keys", []))

    # Append mode so resumed runs keep prior rows; truncate only on a clean start.
    mode = "a" if completed else "w"
    with open(jsonl_path, mode, encoding="utf-8") as jsonl_fh:
        for key, start_at, end_at in ranges:
            if key in completed:
                continue
            log(f"[{year}] {key}: {start_at} -> {end_at}")
            try:
                wrote = run_chunk(auth, base, type_id, start_at, end_at, jsonl_fh, keys)
                log(f"[{year}] {key}: {wrote:,} rows")
            except QuotaExceeded as q:
                log(f"[{year}] daily bulk-export quota hit ({q}). "
                    f"Progress saved -- re-run the same command after it resets.")
                state["completed"] = sorted(completed)
                state["keys"] = sorted(keys)
                state_path.write_text(json.dumps(state, indent=2))
                raise SystemExit(3)
            completed.add(key)
            state["completed"] = sorted(completed)
            state["keys"] = sorted(keys)
            state_path.write_text(json.dumps(state, indent=2))

    out_path = out_dir / f"form_fills_{year}.csv"
    n = _write_year_csv(jsonl_path, keys, out_path)
    log(f"[{year}] wrote {n:,} rows, {len(order_columns(keys))} columns -> {out_path}")
    return out_path


def probe(auth: MarketoAuth, base: str, type_id: int, today: _dt.date) -> int:
    """Pull a tiny window and print discovered columns + a sample row."""
    end = today
    start = today - _dt.timedelta(days=PROBE_DAYS)
    start_at = f"{start.isoformat()}T00:00:00Z"
    end_at = f"{end.isoformat()}T00:00:00Z"
    log(f"[probe] pulling {start_at} -> {end_at}")
    keys: set = set()
    tmp_jsonl = Path(tempfile.gettempdir()) / "marketo_formfill_probe.jsonl"
    with open(tmp_jsonl, "w", encoding="utf-8") as fh:
        n = run_chunk(auth, base, type_id, start_at, end_at, fh, keys)
    log(f"[probe] {n:,} rows in the last {PROBE_DAYS} day(s)")
    cols = order_columns(keys)
    print("Discovered columns ({}):".format(len(cols)))
    for c in cols:
        print(f"  - {c}")
    # show the first row, if any
    with open(tmp_jsonl, encoding="utf-8") as fh:
        first = fh.readline().strip()
    if first:
        print("\nSample row:")
        print(json.dumps(json.loads(first), indent=2, ensure_ascii=False))
    tmp_jsonl.unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Marketo form-fill report (bulk export).")
    parser.add_argument("--year", type=int, action="append",
                        help="Year to pull (repeatable). Default: the current year.")
    parser.add_argument("--out-dir", default="output", help="Output directory.")
    parser.add_argument("--probe", action="store_true",
                        help="Pull a tiny window and print discovered columns; write nothing.")
    parser.add_argument("--today", help="Override 'today' (YYYY-MM-DD); the current year is pulled up to this date.")
    args = parser.parse_args(argv)

    cfg = load_config()
    auth = MarketoAuth(**cfg)
    base = cfg["rest_base"]

    today = _dt.date.fromisoformat(args.today) if args.today else _dt.date.today()
    type_id = resolve_activity_type_id(auth, base)

    if args.probe:
        return probe(auth, base, type_id, today)

    years = args.year or [today.year]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / ".work"
    work_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for year in years:
        p = process_year(auth, base, type_id, year, today, out_dir, work_dir)
        if p:
            written.append(p)

    log("\nDone. Wrote:")
    for p in written:
        log(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
