#!/usr/bin/env python3
"""Marketo email "sent this year" report builder.

Produces two CSVs of emails sent since Jan 1 of the target year, split by the
asset's `operational` flag:

    operational_emails_<YEAR>.csv       (operational == true)
    non_operational_emails_<YEAR>.csv   (operational == false)

Each file has the exact header / column order:

    email_name,sent,delivered,url,fromEmail,domain

Data comes from two sources, joined on email name:

  1. Marketo Asset API  -> metadata (id, name, fromEmail, operational + derived
                           domain and deep-link url)
  2. Email Performance Report CSV exported from the Marketo UI (--epr)
                        -> canonical Sent / Delivered numbers

Only emails that appear in the EPR export (i.e. actually have performance data)
are written out; everything else is dropped.

Usage:
    python marketo_email_report.py --epr email_performance.csv
    python marketo_email_report.py --epr epr.csv --year 2024 --out-dir ./out
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

from auth import MarketoAuth, load_config, log

# ----------------------------------------------------------------------------
# Asset API constants
# ----------------------------------------------------------------------------

PAGE_SIZE = 200  # maxReturn cap for the emails endpoint
MAX_RETRIES = 5  # for rate-limit / transient backoff
BASE_BACKOFF_SECONDS = 2.0

# Marketo error codes worth handling explicitly.
TOKEN_ERROR_CODES = {"600", "601", "602", "603"}
RATE_LIMIT_CODES = {"606", "615"}  # 606 = too many calls, 615 = concurrent limit

# Deep-link suffix. Library emails use "A1"; program-LOCAL emails use "A1LA1".
# The Asset API does not reliably flag local vs library, so we default to the
# library ("A1") form and expose the suffix as a CLI override.
#
# TODO: program-local emails resolve to "{APP_BASE}/#EM{id}A1LA1" (the trailing
# "LA1" = Local Asset 1). The sample deep link #EM91259A1LA1 is one such local
# email. There is currently no clean field on the email record to distinguish
# the two, so anything that is actually program-local will get a slightly wrong
# (library-style) link until Marketo exposes that distinction. Override with
# --url-suffix A1LA1 if you know your set is program-local.
DEFAULT_URL_SUFFIX = "A1"


# ----------------------------------------------------------------------------
# Source 1: Asset API
# ----------------------------------------------------------------------------


def fetch_all_emails(auth: MarketoAuth) -> list[dict]:
    """Page through {REST_BASE}/rest/asset/v1/emails.json and return all records.

    Handles the {success, result, errors} envelope, refreshes the token on
    601/602-style errors, and backs off on 606/615 rate-limit errors.
    """
    session = requests.Session()
    url = f"{auth.rest_base}/rest/asset/v1/emails.json"
    emails: list[dict] = []
    offset = 0

    while True:
        batch = _get_email_page(session, auth, url, offset)
        if not batch:
            break
        emails.extend(batch)
        log(f"[assets] fetched {len(batch)} emails (offset {offset}); total {len(emails)}")
        offset += PAGE_SIZE

    log(f"[assets] done: {len(emails)} email assets pulled")
    return emails


def _get_email_page(session, auth, url, offset) -> list[dict]:
    """Fetch a single page, with token-refresh and rate-limit retries."""
    attempt = 0
    while True:
        attempt += 1
        params = {"maxReturn": PAGE_SIZE, "offset": offset}
        headers = auth.auth_header()
        try:
            resp = session.get(url, params=params, headers=headers, timeout=60)
        except requests.RequestException as exc:
            if attempt > MAX_RETRIES:
                raise RuntimeError(f"[assets] network error after {MAX_RETRIES} retries: {exc}")
            _backoff(attempt, f"network error: {exc}")
            continue

        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise RuntimeError(f"[assets] non-JSON response: {resp.text[:200]}")

        if data.get("success"):
            return data.get("result", []) or []

        # Not successful -> inspect the errors envelope.
        errors = data.get("errors", []) or []
        codes = {str(e.get("code")) for e in errors}

        if codes & TOKEN_ERROR_CODES:
            if attempt > MAX_RETRIES:
                raise RuntimeError(f"[assets] token errors persisted: {errors}")
            log(f"[assets] token error {codes}; refreshing token and retrying")
            auth.refresh()
            continue

        if codes & RATE_LIMIT_CODES:
            if attempt > MAX_RETRIES:
                raise RuntimeError(f"[assets] rate limited, gave up after {MAX_RETRIES} retries: {errors}")
            _backoff(attempt, f"rate limit {codes}")
            continue

        # Anything else is fatal — surface it.
        raise RuntimeError(f"[assets] API error at offset {offset}: {errors}")


def _backoff(attempt: int, reason: str) -> None:
    delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
    log(f"[assets] {reason}; backing off {delay:.1f}s (attempt {attempt})")
    time.sleep(delay)


def build_asset_record(email: dict, app_base: str, url_suffix: str) -> dict:
    """Extract the fields we care about and derive domain + deep-link url."""
    from_email = ""
    raw_from = email.get("fromEmail")
    # fromEmail can be a plain string or an object like {"type": "...", "value": "..."}.
    if isinstance(raw_from, dict):
        from_email = (raw_from.get("value") or "").strip()
    elif isinstance(raw_from, str):
        from_email = raw_from.strip()

    domain = from_email.split("@", 1)[1] if "@" in from_email else ""
    email_id = email.get("id")
    url = f"{app_base}/#EM{email_id}{url_suffix}"

    folder = email.get("folder") or {}
    folder_name = (folder.get("folderName") or "").strip()

    return {
        "id": email_id,
        "name": (email.get("name") or "").strip(),
        "fromEmail": from_email,
        "operational": bool(email.get("operational", False)),
        "domain": domain,
        "url": url,
        "folder": folder_name,
        "is_local": folder.get("type") == "Program",
    }


# ----------------------------------------------------------------------------
# Source 2: Email Performance Report CSV
# ----------------------------------------------------------------------------

# Acceptable header spellings (compared lowercased + stripped).
NAME_HEADERS = {"email name", "email", "name"}
SENT_HEADERS = {"sent"}
DELIVERED_HEADERS = {"delivered"}


def _resolve_column(fieldnames: list[str], wanted: set[str], label: str) -> str:
    """Find the actual header matching one of `wanted` (case-insensitive)."""
    lookup = { (fn or "").strip().lower(): fn for fn in fieldnames }
    for cand in wanted:
        if cand in lookup:
            return lookup[cand]
    raise SystemExit(
        f"ERROR: EPR file is missing a '{label}' column. "
        f"Looked for any of {sorted(wanted)}; found columns: {fieldnames}"
    )


def _parse_int(value: str) -> int:
    """Parse a metric cell, tolerating thousands separators and blanks."""
    if value is None:
        return 0
    cleaned = value.replace(",", "").replace('"', "").strip()
    if cleaned in ("", "-"):
        return 0
    try:
        return int(float(cleaned))
    except ValueError:
        return 0


def _read_tabular(path: Path) -> tuple[list[str], list[dict]]:
    """Read a .csv or .xlsx export into (fieldnames, list-of-row-dicts).

    Cell values are returned as strings so downstream parsing is uniform.
    """
    if not path.is_file():
        raise SystemExit(f"ERROR: EPR file not found: {path}")

    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise SystemExit(
                "ERROR: reading .xlsx requires openpyxl. Run: pip install -r requirements.txt"
            )
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        grid = list(ws.iter_rows(values_only=True))
        if not grid:
            raise SystemExit(f"ERROR: EPR file is empty: {path}")
        header = [("" if c is None else str(c)).strip() for c in grid[0]]
        rows = []
        for raw in grid[1:]:
            rows.append(
                {header[i]: ("" if v is None else str(v)) for i, v in enumerate(raw) if i < len(header)}
            )
        return header, rows

    # default: CSV
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit(f"ERROR: EPR file has no header row: {path}")
        return list(reader.fieldnames), list(reader)


def parse_epr(path: Path) -> list[dict]:
    """Parse the EPR export into rows of {label, sent, delivered}.

    `label` is the raw value of the email-name column. In Marketo's Email
    Performance export this is often a nested "Program.EmailName" path rather
    than a bare email name; the actual email name is teased out later, during
    merge, by matching against known asset names (see resolve_email_name).

    Fails loudly (SystemExit) if a required column is missing.
    """
    fieldnames, raw_rows = _read_tabular(path)

    name_col = _resolve_column(fieldnames, NAME_HEADERS, "email name")
    sent_col = _resolve_column(fieldnames, SENT_HEADERS, "Sent")
    delivered_col = _resolve_column(fieldnames, DELIVERED_HEADERS, "Delivered")
    log(f"[epr] using columns -> name='{name_col}', sent='{sent_col}', delivered='{delivered_col}'")

    rows = []
    for raw in raw_rows:
        label = (raw.get(name_col) or "").strip()
        if not label or label.lower() == "total":
            continue  # skip blank rows and the grand-total row
        rows.append(
            {
                "label": label,
                "sent": _parse_int(raw.get(sent_col)),
                "delivered": _parse_int(raw.get(delivered_col)),
            }
        )

    log(f"[epr] parsed {len(rows)} performance rows from {path}")
    return rows


# ----------------------------------------------------------------------------
# Merge
# ----------------------------------------------------------------------------


def _norm(name: str) -> str:
    return name.strip().lower()


def resolve_email_name(label: str, by_name: dict[str, list[dict]]) -> tuple[str, str, list[dict]]:
    """Tease the real email name out of an EPR label.

    Marketo concatenates the report hierarchy as "Program.EmailName", but BOTH
    the program and the email name can themselves contain '.' (program
    "2023.01-Content..." / email "01 Email - 1.15.26"), so a single split is
    unreliable. Instead we pick the LONGEST suffix of the label that is an
    actual asset name. The remaining prefix is treated as the program (used to
    break ties between same-named assets).

    Returns (program, email_name, candidate_assets). If nothing matches,
    email_name is "" and candidates is empty.
    """
    parts = label.split(".")
    # Longest suffix first => smallest split index.
    for i in range(1, len(parts)):
        name = ".".join(parts[i:]).strip()
        cands = by_name.get(_norm(name))
        if cands:
            return ".".join(parts[:i]).strip(), name, cands
    # Whole label is itself an asset name (no program prefix).
    cands = by_name.get(_norm(label))
    if cands:
        return "", label.strip(), cands
    return "", "", []


def _pick(values: list[str]) -> str:
    """Return the common value if all candidates agree, else '' (unknown)."""
    uniq = set(values)
    return uniq.pop() if len(uniq) == 1 else ""


def merge(epr_rows: list[dict], assets: list[dict]) -> dict:
    """Resolve each EPR row to its email asset and split by operational flag.

    Resolution per row:
      1. Find the email name via longest known-asset-name suffix of the label.
      2. If exactly one asset has that name -> use it.
      3. Else use the program prefix to pick the asset whose folder matches.
      4. Else if every same-named asset agrees on `operational`, use that
         (metadata fields are filled only where the candidates also agree).
      5. Otherwise the row is unresolved (recorded, never silently dropped).
    """
    by_name: dict[str, list[dict]] = defaultdict(list)
    for a in assets:
        if a["name"]:
            by_name[_norm(a["name"])].append(a)

    operational: list[dict] = []
    non_operational: list[dict] = []
    unresolved: list[dict] = []
    counts = defaultdict(int)

    def emit(name, row, operational_flag, url, from_email, domain):
        out = {
            "email_name": name,
            "sent": row["sent"],
            "delivered": row["delivered"],
            "url": url,
            "fromEmail": from_email,
            "domain": domain,
        }
        (operational if operational_flag else non_operational).append(out)

    for row in epr_rows:
        program, name, cands = resolve_email_name(row["label"], by_name)

        if not cands:
            counts["no_asset"] += 1
            unresolved.append({**row, "reason": "no asset with this name"})
            log(f"[merge] no asset match for EPR row: {row['label']!r}")
            continue

        if len(cands) == 1:
            counts["unique"] += 1
            a = cands[0]
            emit(name, row, a["operational"], a["url"], a["fromEmail"], a["domain"])
            continue

        # Ambiguous name: try to disambiguate via the program prefix vs folder.
        pl = program.lower()
        folder_hits = [
            a for a in cands
            if a["folder"] and (pl.startswith(a["folder"].lower()) or a["folder"].lower() in pl)
        ]
        if len(folder_hits) == 1:
            counts["program_folder"] += 1
            a = folder_hits[0]
            emit(name, row, a["operational"], a["url"], a["fromEmail"], a["domain"])
            continue

        # Fall back: if all same-named assets agree on operational, trust that.
        ops = {a["operational"] for a in cands}
        if len(ops) == 1:
            counts["name_agree_operational"] += 1
            emit(
                name, row, next(iter(ops)),
                _pick([a["url"] for a in cands]),
                _pick([a["fromEmail"] for a in cands]),
                _pick([a["domain"] for a in cands]),
            )
            continue

        counts["ambiguous"] += 1
        ids = [a["id"] for a in cands]
        unresolved.append({**row, "reason": f"name maps to assets with differing operational flags: {ids}"})
        log(f"[merge] AMBIGUOUS {name!r} (program {program!r}) -> asset ids {ids}; unresolved")

    return {
        "operational": operational,
        "non_operational": non_operational,
        "unresolved": unresolved,
        "counts": dict(counts),
    }


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------

OUTPUT_HEADER = ["email_name", "sent", "delivered", "url", "fromEmail", "domain"]

# Metadata-only mode (no EPR) drops the performance columns.
METADATA_HEADER = ["email_name", "url", "fromEmail", "domain"]


def write_csv(path: Path, rows: list[dict], header: list[str] = OUTPUT_HEADER) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    log(f"[output] wrote {len(rows)} rows -> {path}")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build operational / non-operational Marketo email reports for a year.",
    )
    parser.add_argument(
        "--epr",
        required=False,
        default=None,
        type=Path,
        help=(
            "Path to the Email Performance Report CSV exported from Marketo. "
            "If omitted, runs in metadata-only mode: every email asset is written "
            "(no sent/delivered columns, no performance filtering)."
        ),
    )
    parser.add_argument(
        "--year",
        type=int,
        default=_dt.date.today().year,
        help="Report year (used only for output filenames). Defaults to the current year.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("output"),
        help="Directory to write the CSVs into. Default: output",
    )
    parser.add_argument(
        "--url-suffix",
        default=DEFAULT_URL_SUFFIX,
        help=(
            "Deep-link suffix after #EM{id}. Default 'A1' (library emails). "
            "Use 'A1LA1' for program-local emails."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    cfg = load_config(need_app_base=True)
    auth = MarketoAuth(**cfg)

    # --- pull metadata ---
    raw_emails = fetch_all_emails(auth)
    assets = [build_asset_record(e, cfg["app_base"], args.url_suffix) for e in raw_emails]
    op_total = sum(1 for a in assets if a["operational"])
    nonop_total = len(assets) - op_total

    args.out_dir.mkdir(parents=True, exist_ok=True)
    op_path = args.out_dir / f"operational_emails_{args.year}.csv"
    nonop_path = args.out_dir / f"non_operational_emails_{args.year}.csv"

    if args.epr is None:
        # --- metadata-only mode: write every asset, no performance data ---
        op_rows = [_metadata_row(a) for a in assets if a["operational"]]
        nonop_rows = [_metadata_row(a) for a in assets if not a["operational"]]
        write_csv(op_path, op_rows, header=METADATA_HEADER)
        write_csv(nonop_path, nonop_rows, header=METADATA_HEADER)

        log("")
        log("================ SUMMARY (metadata-only) ================")
        log(f"  Report year ............... {args.year}")
        log(f"  Total assets pulled ....... {len(assets)}")
        log(f"    operational ............. {op_total}")
        log(f"    non-operational ......... {nonop_total}")
        log(f"  Written:")
        log(f"    {op_path}  ({len(op_rows)} rows)")
        log(f"    {nonop_path}  ({len(nonop_rows)} rows)")
        log("  NOTE: no --epr export given, so sent/delivered are omitted and")
        log("        no performance filtering was applied (all assets included).")
        log("========================================================")
        return 0

    # --- parse performance export ---
    epr_rows = parse_epr(args.epr)

    # --- merge ---
    result = merge(epr_rows, assets)
    counts = result["counts"]

    # --- write ---
    write_csv(op_path, result["operational"])
    write_csv(nonop_path, result["non_operational"])
    unresolved = result["unresolved"]
    unresolved_path = args.out_dir / f"unresolved_emails_{args.year}.csv"
    if unresolved:
        write_csv(
            unresolved_path, unresolved,
            header=["label", "sent", "delivered", "reason"],
        )

    resolved = len(result["operational"]) + len(result["non_operational"])
    vol_total = sum(r["sent"] for r in epr_rows)
    vol_unres = sum(r["sent"] for r in unresolved)
    vol_pct = 100 * (vol_total - vol_unres) / vol_total if vol_total else 100.0

    # --- summary ---
    log("")
    log("================ SUMMARY ================")
    log(f"  Report year ............... {args.year}")
    log(f"  Total assets pulled ....... {len(assets)}")
    log(f"    operational ............. {op_total}")
    log(f"    non-operational ......... {nonop_total}")
    log(f"  EPR rows parsed ........... {len(epr_rows)}")
    log(f"    resolved ................ {resolved}")
    log(f"      unique name ............... {counts.get('unique', 0)}")
    log(f"      program/folder match ...... {counts.get('program_folder', 0)}")
    log(f"      name agrees on operational  {counts.get('name_agree_operational', 0)}")
    log(f"    unresolved .............. {len(unresolved)}")
    log(f"      no matching asset ......... {counts.get('no_asset', 0)}")
    log(f"      ambiguous operational ..... {counts.get('ambiguous', 0)}")
    log(f"  Send volume resolved ...... {vol_pct:.1f}%  ({vol_total - vol_unres:,} / {vol_total:,})")
    log(f"  Written:")
    log(f"    {op_path}  ({len(result['operational'])} rows)")
    log(f"    {nonop_path}  ({len(result['non_operational'])} rows)")
    if unresolved:
        log(f"    {unresolved_path}  ({len(unresolved)} rows)")
    log("========================================")
    return 0


def _metadata_row(asset: dict) -> dict:
    return {
        "email_name": asset["name"],
        "url": asset["url"],
        "fromEmail": asset["fromEmail"],
        "domain": asset["domain"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
