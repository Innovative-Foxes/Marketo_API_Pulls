#!/usr/bin/env python3
"""Read-only audit of email From addresses (no writes to Marketo).

Pulls every email asset in the instance and classifies each by its From
address, e.g. in preparation for moving literal From addresses onto a single
sending domain (--target-domain):

    token     -> From uses a {{token}}              -> leave as-is
    target    -> From is already on the target domain -> no change needed
    empty     -> From blank/unset                   -> flagged (would fail approval)
    candidate -> literal From on some other domain  -> would need updating

Writes (to --out-dir, default output/):
    from_address_candidates_<year>.csv   (status == candidate)
    from_address_audit_<year>.csv        (every asset, all statuses)

and prints a summary with the candidate-domain breakdown. Makes ZERO changes.

Run:
    python from_address_audit.py --target-domain mail.example.com
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
from collections import Counter
from pathlib import Path

from auth import MarketoAuth, load_config, log
from marketo_email_report import fetch_all_emails, build_asset_record


def is_token(from_email: str) -> bool:
    """A token-based From address comes back as the literal token, e.g. {{my.x}}."""
    return "{{" in from_email and "}}" in from_email


def classify(asset: dict, target_domain: str) -> str:
    raw = (asset.get("fromEmail") or "").strip()
    if not raw:
        return "empty"
    if is_token(raw):
        return "token"
    if (asset.get("domain") or "").strip().lower() == target_domain.lower():
        return "target"
    return "candidate"


AUDIT_HEADER = [
    "id", "email_name", "operational", "status",
    "current_fromEmail", "current_domain", "folder", "is_local", "url",
]


def _row(asset: dict, status: str) -> dict:
    return {
        "id": asset.get("id"),
        "email_name": asset.get("name", ""),
        "operational": asset.get("operational", False),
        "status": status,
        "current_fromEmail": asset.get("fromEmail", ""),
        "current_domain": asset.get("domain", ""),
        "folder": asset.get("folder", ""),
        "is_local": asset.get("is_local", False),
        "url": asset.get("url", ""),
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=AUDIT_HEADER, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log(f"[output] wrote {len(rows)} rows -> {path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Audit email From addresses (read-only).")
    parser.add_argument("--target-domain", required=True, metavar="DOMAIN",
                        help="Sending domain considered already-correct, e.g. mail.example.com.")
    parser.add_argument("--year", type=int, default=_dt.date.today().year,
                        help="Label used in output filenames. Defaults to current year.")
    parser.add_argument("--out-dir", type=Path, default=Path("output"),
                        help="Output directory. Default: output")
    args = parser.parse_args(argv)

    cfg = load_config(need_app_base=True)
    auth = MarketoAuth(**cfg)

    assets = [build_asset_record(e, cfg["app_base"], "A1") for e in fetch_all_emails(auth)]

    all_rows: list[dict] = []
    candidates: list[dict] = []
    counts: Counter = Counter()
    candidate_domains: Counter = Counter()

    for a in assets:
        status = classify(a, args.target_domain)
        counts[status] += 1
        row = _row(a, status)
        all_rows.append(row)
        if status == "candidate":
            candidates.append(row)
            candidate_domains[(a.get("domain") or "(none)")] += 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    cand_path = args.out_dir / f"from_address_candidates_{args.year}.csv"
    audit_path = args.out_dir / f"from_address_audit_{args.year}.csv"
    write_csv(cand_path, candidates)
    write_csv(audit_path, all_rows)

    op_cand = sum(1 for r in candidates if r["operational"])
    log("")
    log("================ FROM-ADDRESS AUDIT (read-only) ================")
    log(f"  Target domain ............. @{args.target_domain}")
    log(f"  Total email assets ........ {len(assets)}")
    log(f"    candidate (would update)  {counts['candidate']}")
    log(f"        operational ......... {op_cand}")
    log(f"        non-operational ..... {counts['candidate'] - op_cand}")
    log(f"    token (left as-is) ....... {counts['token']}")
    log(f"    already on target ........ {counts['target']}")
    log(f"    empty/unset (skipped) .... {counts['empty']}")
    log("")
    log("  Candidate From domains (top 15):")
    for dom, n in candidate_domains.most_common(15):
        log(f"      {n:6,}  @{dom}")
    log("")
    log(f"  Candidate list -> {cand_path}")
    log(f"  Full audit ----> {audit_path}")
    log("  NOTE: read-only. No emails were modified or approved.")
    log("===============================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
