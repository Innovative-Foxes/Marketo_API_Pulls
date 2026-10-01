#!/usr/bin/env python3
"""Static-list membership report for reply lists (list-name prefix match).

Some tools (chat/conversation platforms, reply-tracking integrations) drop people
into static lists whose names share a prefix, e.g. "Replied".
This tool finds every such list, counts its members (= people added, since these
reply lists are append-only), and attributes each to its parent program.

Outputs two CSVs:
  * reply_lists_detail.csv      one row per Replied list:
        program, list_name, list_id, member_count, workspace, createdAt, updatedAt
  * reply_lists_by_program.csv  one row per program:
        program, num_replied_lists, total_members

"Member count" is the current membership. Because repliers are only ever added
(never removed), this equals the all-time number of people added — and unlike
activity history, it isn't limited by Marketo's activity-retention window.

Run:
    python reply_list_report.py
    python reply_list_report.py --prefix Replied --out-dir output
"""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

import requests

from auth import MarketoAuth, load_config, log

# Marketo throttle / transient error codes worth a short backoff + retry.
RETRY_CODES = {"602", "606", "615", "604"}  # token exp, rate, concurrency, timeout
# "No leads found" style responses -> treat as an empty (0-member) list.
EMPTY_CODES = {"1003", "1004", "1077"}


def _get(auth: MarketoAuth, url: str, params: dict) -> dict:
    """GET with auto-refreshed auth header and basic throttle/backoff retry."""
    for attempt in range(6):
        headers = auth.auth_header()  # re-issued each call; refreshes if expired
        try:
            r = requests.get(url, headers=headers, params=params, timeout=60).json()
        except Exception as e:  # noqa: BLE001 - network blip, retry
            log(f"    request error ({e}); retry {attempt + 1}")
            time.sleep(2 + attempt * 2)
            continue
        if r.get("success"):
            return r
        codes = {str(e.get("code")) for e in r.get("errors", [])}
        if codes & EMPTY_CODES:
            return {"success": True, "result": []}
        if codes & RETRY_CODES:
            wait = 5 + attempt * 5
            log(f"    throttled/transient {codes}; waiting {wait}s")
            if "602" in codes:
                auth.refresh()
            time.sleep(wait)
            continue
        raise RuntimeError(f"GET {url} failed: {r.get('errors')}")
    raise RuntimeError(f"GET {url} exhausted retries")


def fetch_all_lists(auth: MarketoAuth, base: str) -> list[dict]:
    """Page through every static list (moreResult is unreliable; page till empty)."""
    out: list[dict] = []
    tok = None
    while True:
        params = {"batchSize": 300}
        if tok:
            params["nextPageToken"] = tok
        r = _get(auth, f"{base}/rest/v1/lists.json", params)
        res = r.get("result", [])
        out += res
        tok = r.get("nextPageToken")
        if not res or not tok:
            break
    return out


def count_members(auth: MarketoAuth, base: str, list_id) -> int:
    """Count members of a list by paging its leads (id field only, for speed)."""
    total = 0
    tok = None
    while True:
        params = {"batchSize": 300, "fields": "id"}
        if tok:
            params["nextPageToken"] = tok
        r = _get(auth, f"{base}/rest/v1/lists/{list_id}/leads.json", params)
        res = r.get("result", [])
        total += len(res)
        tok = r.get("nextPageToken")
        if not res or not tok:
            break
    return total


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reply-list membership report.")
    ap.add_argument("--prefix", default="Replied",
                    help="List-name prefix to match (case-insensitive). Default: Replied")
    ap.add_argument("--out-dir", default="output", help="Output directory.")
    args = ap.parse_args(argv)

    cfg = load_config()
    auth = MarketoAuth(**cfg)
    base = cfg["rest_base"]

    log("[lists] fetching all static lists ...")
    all_lists = fetch_all_lists(auth, base)
    pref = args.prefix.lower()
    replied = [l for l in all_lists if l.get("name", "").lower().startswith(pref)]
    log(f"[lists] {len(all_lists):,} total; {len(replied):,} start with '{args.prefix}'")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for i, l in enumerate(replied, 1):
        n = count_members(auth, base, l["id"])
        rows.append({
            "program": l.get("programName", "") or "(no program)",
            "list_name": l.get("name", ""),
            "list_id": l.get("id", ""),
            "member_count": n,
            "workspace": l.get("workspaceName", ""),
            "createdAt": l.get("createdAt", ""),
            "updatedAt": l.get("updatedAt", ""),
        })
        if i % 25 == 0 or i == len(replied):
            log(f"    counted {i}/{len(replied)} lists")

    # Detail: sort by program then most members first.
    rows.sort(key=lambda r: (r["program"].lower(), -r["member_count"]))
    detail_path = out_dir / "reply_lists_detail.csv"
    cols = ["program", "list_name", "list_id", "member_count",
            "workspace", "createdAt", "updatedAt"]
    with open(detail_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # Rollup by program.
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["program"], {"program": r["program"],
                                          "num_replied_lists": 0, "total_members": 0})
        a["num_replied_lists"] += 1
        a["total_members"] += r["member_count"]
    prog_rows = sorted(agg.values(), key=lambda a: -a["total_members"])
    prog_path = out_dir / "reply_lists_by_program.csv"
    with open(prog_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["program", "num_replied_lists", "total_members"])
        w.writeheader()
        w.writerows(prog_rows)

    grand = sum(r["member_count"] for r in rows)
    log(f"\nDone. {len(rows):,} Replied lists across {len(prog_rows):,} programs; "
        f"{grand:,} total members.")
    log(f"  {detail_path}")
    log(f"  {prog_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
