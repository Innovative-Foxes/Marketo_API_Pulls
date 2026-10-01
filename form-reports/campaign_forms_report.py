#!/usr/bin/env python3
"""Forms in/under a campaign folder (one row per form).

Reports every distinct form that is, under the target folder (--folder):
  * STORED in the folder subtree (program-local form asset), and/or
  * EMBEDDED in a landing page belonging to a program in the subtree.

NOTE: a third sense — "referenced by a smart campaign's Filled Out Form
trigger" — is NOT obtainable: the Marketo REST API does not expose smart
campaign / smart list rule logic (only the captive smart list's metadata).
Those campaign-trigger forms (typically your website contact or demo-request forms)
cannot be resolved to a form id via API. This report covers the two sources
that ARE retrievable.

Output (one row per form): form_pages columns
    form_id, form_name, status, sources, stored_in, embedded_lp_count,
    embedded_landing_pages   (name [link] ; ...)

Run:
    python campaign_forms_report.py --folder 1234
"""

from __future__ import annotations

import argparse
import collections
import csv
import requests
from pathlib import Path
from urllib.parse import urlsplit

from auth import MarketoAuth, load_config, log
from form_config import lp_link
from form_pages_report import asset_page, scoped_forms


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", type=int, required=True,
                    help="Marketo Design Studio / Marketing Activities folder ID that holds the forms (see README).")
    ap.add_argument("--out-dir", default="output")
    args = ap.parse_args(argv)

    cfg = load_config()
    auth = MarketoAuth(**cfg)
    base = cfg["rest_base"]
    so_id = urlsplit(base).netloc.split(".")[0]

    # subtree (folders + programs)
    fol = asset_page(auth, base, "/rest/asset/v1/folders.json", {"maxDepth": 20})
    kids = collections.defaultdict(list)
    for f in fol:
        kids[(f.get("parent") or {}).get("id")].append(f)
    sub, stack = {args.folder}, [args.folder]
    while stack:
        for c in kids.get(stack.pop(), []):
            sub.add(c["id"])
            stack.append(c["id"])
    progs = asset_page(auth, base, "/rest/asset/v1/programs.json")
    prog_ids = {p["id"] for p in progs if (p.get("folder") or {}).get("value") in sub}
    scope = sub | prog_ids

    all_forms = {f["id"]: f for f in asset_page(auth, base, "/rest/asset/v1/forms.json")}
    stored = {f["id"]: f for f in scoped_forms(auth, base, args.folder)}

    # embedded forms across in-scope LPs
    lps = asset_page(auth, base, "/rest/asset/v1/landingPages.json")
    in_lps = [lp for lp in lps if (lp.get("folder") or {}).get("value") in scope]
    log(f"[scope] {len(sub)} folders, {len(prog_ids)} programs, {len(in_lps)} landing pages")
    embed = collections.defaultdict(list)  # form_id -> [(lp_id, lp_name)]
    for lp in in_lps:
        rc = requests.get(base + f"/rest/asset/v1/landingPage/{lp['id']}/content.json",
                          headers=auth.auth_header(), timeout=60).json()
        for c in rc.get("result", []):
            if c.get("type") == "Form":
                cf = c.get("content")
                cid = cf.get("content") if isinstance(cf, dict) else cf
                if str(cid).isdigit():
                    embed[int(cid)].append((lp["id"], lp.get("name", "")))

    union = sorted(set(stored) | set(embed),
                   key=lambda i: (all_forms.get(i, {}).get("name", "") or "").lower())
    rows = []
    for fid in union:
        f = all_forms.get(fid, {})
        srcs = []
        if fid in stored:
            srcs.append("stored")
        if fid in embed:
            srcs.append("LP-embedded")
        lp_pairs = sorted(set(embed.get(fid, [])))
        rows.append({
            "form_id": fid,
            "form_name": f.get("name", "(unknown)"),
            "status": f.get("status", ""),
            "sources": " + ".join(srcs),
            "stored_in": (stored.get(fid, {}).get("folder") or {}).get("folderName", "") if fid in stored else "",
            "embedded_lp_count": len(lp_pairs),
            "embedded_landing_pages": " ; ".join(
                f"{nm} [{lp_link(so_id, lid)}]" if lp_link(so_id, lid) else nm
                for lid, nm in lp_pairs),
        })

    cols = ["form_id", "form_name", "status", "sources", "stored_in",
            "embedded_lp_count", "embedded_landing_pages"]
    out = Path(args.out_dir) / "campaign_forms_report.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    log(f"\nWrote {len(rows)} forms -> {out}")
    for r in rows:
        log(f"  {r['form_id']:>5} {r['form_name'][:42]:42} [{r['sources']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
