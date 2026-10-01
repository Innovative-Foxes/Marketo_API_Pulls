#!/usr/bin/env python3
"""All pages per form (one row per form+page), with landing-page links.

For each form stored under the target folder (--folder, including its
subfolders), enumerate EVERY page the form lives on or has been filled out on — one row per
(form, page):

    form_id, form_name, form_status, relationship,
    page_url, page_name, fills, lp_id, lp_public_url, lp_status

Two relationships:
  * "Lives on (Marketo LP)" — a Marketo landing page that embeds the form.
    page_url is the Adobe Experience Cloud deep link when ADOBE_EXPERIENCE_TENANT
    is set (https://experience.adobe.com/#/@<tenant>/so:<id>/marketo-engage/
    landingpage/<lp id>), otherwise the landing page's public URL.
  * "Filled out on"          — an external/web page where the form was actually
    submitted (from Fill Out Form activity cache). page_url is the real URL.

Run:
    python form_page_links_report.py --folder 1234
    python form_page_links_report.py --folder 1234 --years 2025 2026
"""

from __future__ import annotations

import argparse
import datetime
import csv
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from auth import MarketoAuth, load_config, log
from form_config import lp_link
from form_pages_report import (asset_page, filled_pages_from_cache, lp_embeds,
                               scoped_forms)



def main(argv=None):
    ap = argparse.ArgumentParser(description="All-pages-per-form report with landing-page links.")
    ap.add_argument("--folder", type=int, required=True,
                    help="Marketo Design Studio / Marketing Activities folder ID that holds the forms (see README).")
    ap.add_argument("--years", type=int, nargs="*", default=None,
                    help="Years of cached fill data to use. Default: the current year.")
    ap.add_argument("--out-dir", default="output")
    args = ap.parse_args(argv)
    args.years = args.years or [datetime.date.today().year]

    cfg = load_config()
    auth = MarketoAuth(**cfg)
    base = cfg["rest_base"]
    # instance / subscription id, e.g. "123-ABC-456" from 123-ABC-456.mktorest.com
    so_id = urlsplit(base).netloc.split(".")[0]
    log(f"[cfg] subscription id (so) = {so_id}")

    forms = scoped_forms(auth, base, args.folder)
    forms.sort(key=lambda f: f.get("name", "").lower())
    form_ids = {f["id"] for f in forms}
    fmeta = {f["id"]: f for f in forms}
    log(f"[forms] {len(forms)} forms under folder {args.folder}")

    work_dir = Path(args.out_dir) / ".work"
    pages, fills = filled_pages_from_cache(work_dir, args.years, form_ids)
    embeds = lp_embeds(auth, base, form_ids)

    rows = []
    for fid in sorted(form_ids, key=lambda i: fmeta[i].get("name", "").lower()):
        f = fmeta[fid]
        base_row = {
            "form_id": fid,
            "form_name": f.get("name", ""),
            "form_status": f.get("status", ""),
        }
        n_pages = 0

        # Lives on — Marketo landing pages (deduped by LP id)
        seen_lp = {}
        for e in embeds.get(fid, []):
            seen_lp[e["id"]] = e
        for e in sorted(seen_lp.values(), key=lambda x: (x["name"], x["id"])):
            rows.append({**base_row,
                         "relationship": "Lives on (Marketo LP)",
                         "page_url": lp_link(so_id, e["id"], e["computedUrl"] or e["url"]),
                         "page_name": e["name"],
                         "fills": "",
                         "lp_id": e["id"],
                         "lp_public_url": e["computedUrl"] or e["url"],
                         "lp_status": e["status"]})
            n_pages += 1

        # Filled out on — external/web pages (from activity), most-filled first
        for pg, c in (pages.get(fid) or Counter()).most_common():
            host = urlsplit(pg).netloc
            rows.append({**base_row,
                         "relationship": "Filled out on",
                         "page_url": pg,
                         "page_name": host,
                         "fills": c,
                         "lp_id": "",
                         "lp_public_url": "",
                         "lp_status": ""})
            n_pages += 1

        if n_pages == 0:
            rows.append({**base_row,
                         "relationship": "(no pages found in scan/cache)",
                         "page_url": "", "page_name": "", "fills": "",
                         "lp_id": "", "lp_public_url": "", "lp_status": ""})

    cols = ["form_id", "form_name", "form_status", "relationship",
            "page_url", "page_name", "fills", "lp_id", "lp_public_url", "lp_status"]
    out_path = Path(args.out_dir) / "form_page_links_report.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    log(f"\nWrote {len(rows)} rows across {len(forms)} forms -> {out_path}")
    # quick console view
    cur = None
    for r in rows:
        if r["form_id"] != cur:
            cur = r["form_id"]
            log(f"\n{r['form_id']}  {r['form_name']}")
        extra = f"  ({r['fills']} fills)" if r["fills"] else ""
        log(f"    [{r['relationship']}] {r['page_url']}{extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
