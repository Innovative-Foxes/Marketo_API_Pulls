#!/usr/bin/env python3
"""Pages-per-form report for the forms stored in a Marketing folder.

For every form physically stored under a given Marketing Activities folder
(--folder), emit ONE ROW PER FORM with:

    form_id, form_name, stored_in, status,
    fills_<years>, distinct_filled_pages,
    filled_out_on_pages,        # where people actually submitted (from activity)
    lives_on_marketo_lps        # Marketo landing pages that embed the form

Two page sources:
  * "filled out on" — distinct web pages from Fill Out Form activity (your
    FORM_PAGE_URL_FIELDS, then Webpage URL, then Referrer URL), read from the
    local fill cache that form_fill_report.py writes to
    output/.work/form_fills_<year>.jsonl. Captures EXTERNAL pages. Limited to
    the cached years, so run form_fill_report.py first.
  * "lives on" — Marketo landing pages whose content embeds the form
    (content.content == form id). Captures Marketo-hosted pages even with zero
    fills. Cannot see external (non-Marketo) embeds — those only show via fills.

Run:
    python form_pages_report.py --folder 1234
    python form_pages_report.py --folder 1234 --years 2025 2026 --out-dir output
"""

from __future__ import annotations

import argparse
import datetime
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from auth import MarketoAuth, load_config, log
from form_config import page_url_fields


def asset_page(auth, base, path, extra=None):
    """Page through an Asset API list endpoint (offset/maxReturn)."""
    out, off = [], 0
    while True:
        params = {"offset": off, "maxReturn": 200}
        if extra:
            params.update(extra)
        r = requests.get(f"{base}{path}", headers=auth.auth_header(),
                         params=params, timeout=60).json()
        if not r.get("success"):
            raise RuntimeError(f"{path} failed: {r.get('errors')}")
        res = r.get("result", [])
        out += res
        if len(res) < 200:
            break
        off += 200
    return out


def scoped_forms(auth, base, folder_id):
    """All forms physically stored under folder_id (its subfolders + programs)."""
    folders = asset_page(auth, base, "/rest/asset/v1/folders.json", {"maxDepth": 20})
    kids = defaultdict(list)
    for f in folders:
        kids[(f.get("parent") or {}).get("id")].append(f)
    subfolders, stack = {folder_id}, [folder_id]
    while stack:
        for c in kids.get(stack.pop(), []):
            subfolders.add(c["id"])
            stack.append(c["id"])

    programs = asset_page(auth, base, "/rest/asset/v1/programs.json")
    prog_ids = {p["id"] for p in programs if (p.get("folder") or {}).get("value") in subfolders}

    forms = asset_page(auth, base, "/rest/asset/v1/forms.json")
    out = []
    for f in forms:
        fo = f.get("folder") or {}
        v, t = fo.get("value"), fo.get("type")
        if (t == "Folder" and v in subfolders) or (t == "Program" and v in prog_ids):
            out.append(f)
    return out


def base_page(u: str) -> str:
    """Normalize a URL to scheme+host+path (drop query/fragment, trailing slash)."""
    if not u:
        return ""
    s = urlsplit(u.strip())
    if not s.netloc:
        return u.strip()
    return urlunsplit((s.scheme, s.netloc, s.path.rstrip("/"), "", ""))


def filled_pages_from_cache(work_dir: Path, years, form_ids):
    """form_id -> Counter(base_page -> fills) and total fills, from the jsonl cache."""
    pages = defaultdict(Counter)
    fills = Counter()
    for y in years:
        p = work_dir / f"form_fills_{y}.jsonl"
        if not p.is_file():
            log(f"[cache] missing {p} (skipping year {y})")
            continue
        for line in open(p, encoding="utf-8"):
            r = json.loads(line)
            fid = r.get("formid", "")
            if not (fid.isdigit() and int(fid) in form_ids):
                continue
            fid = int(fid)
            fills[fid] += 1
            pg = next((b for b in (base_page(r.get(f, "")) for f in page_url_fields()) if b), "")
            if pg:
                # merge http/https of the same host+path
                key = pg.split("://", 1)[-1]
                pages[fid][("https://" + key)] += 1
    return pages, fills


def lp_embeds(auth, base, form_ids):
    """form_id -> list of dicts {id,name,url,computedUrl,status} for embedding LPs."""
    lps = asset_page(auth, base, "/rest/asset/v1/landingPages.json")
    log(f"[lp] scanning {len(lps)} landing pages for form embeds ...")
    embeds = defaultdict(list)
    for i, lp in enumerate(lps, 1):
        rc = requests.get(base + f"/rest/asset/v1/landingPage/{lp['id']}/content.json",
                          headers=auth.auth_header(), timeout=60).json()
        if not rc.get("success"):
            continue
        for c in rc.get("result", []):
            if c.get("type") == "Form":
                # `content` is sometimes {"content": "2877", ...} and sometimes
                # the form id directly as a string/int.
                cf = c.get("content")
                cid = cf.get("content") if isinstance(cf, dict) else cf
                if str(cid).isdigit() and int(cid) in form_ids:
                    embeds[int(cid)].append({
                        "id": lp.get("id"),
                        "name": lp.get("name", ""),
                        "url": lp.get("url") or "",
                        "computedUrl": lp.get("computedUrl") or "",
                        "status": lp.get("status", ""),
                    })
        if i % 100 == 0:
            log(f"[lp]   {i}/{len(lps)}")
    return embeds


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pages-per-form report for a Marketing folder.")
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

    forms = scoped_forms(auth, base, args.folder)
    forms.sort(key=lambda f: f.get("name", "").lower())
    form_ids = {f["id"] for f in forms}
    log(f"[forms] {len(forms)} forms stored under folder {args.folder}")

    out_dir = Path(args.out_dir)
    work_dir = out_dir / ".work"
    pages, fills = filled_pages_from_cache(work_dir, args.years, form_ids)
    embeds = lp_embeds(auth, base, form_ids)

    def join_pages(counter):
        return " | ".join(f"{pg} ({n})" for pg, n in counter.most_common())

    def join_lps(lst):
        seen = {}
        for e in lst:
            seen[e["id"]] = e  # dedupe by LP id
        return " | ".join(
            f"{e['name']} [{e['url'] or e['computedUrl']}]"
            + ("" if e["status"] in ("approved", "") else f" ({e['status']})")
            for e in sorted(seen.values(), key=lambda x: (x["name"], x["id"]))
        )

    rows = []
    for f in forms:
        fid = f["id"]
        rows.append({
            "form_id": fid,
            "form_name": f.get("name", ""),
            "stored_in": (f.get("folder") or {}).get("folderName", ""),
            "status": f.get("status", ""),
            "fills_" + "_".join(str(y) for y in args.years): fills.get(fid, 0),
            "distinct_filled_pages": len(pages.get(fid, {})),
            "filled_out_on_pages": join_pages(pages.get(fid, Counter())),
            "lives_on_marketo_lps": join_lps(embeds.get(fid, [])),
        })

    fills_col = "fills_" + "_".join(str(y) for y in args.years)
    cols = ["form_id", "form_name", "stored_in", "status", fills_col,
            "distinct_filled_pages", "filled_out_on_pages", "lives_on_marketo_lps"]
    out_path = out_dir / "form_pages_report.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    log(f"\nWrote {len(rows)} forms -> {out_path}")
    for r in rows:
        log(f"  {r['form_id']:>5} {r['form_name'][:38]:38} "
            f"fills={r[fills_col]:>3} pages={r['distinct_filled_pages']} "
            f"LPs={len(embeds.get(r['form_id'], []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
