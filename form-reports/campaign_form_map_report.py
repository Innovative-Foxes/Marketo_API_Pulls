#!/usr/bin/env python3
"""Campaign-folder forms incl. INFERRED program->form mapping.

Marketo's REST API does not expose smart-campaign trigger logic, so the form a
campaign fires on cannot be read directly. As an approximation, we match each
program's name to the best-fitting form by normalized name tokens (Jaccard).
This is INFERRED, not authoritative — confidence is reported per row.

Writes two CSVs:
  1. campaign_program_form_map.csv  — one row per program (the audit trail):
       program_id, program_name, inferred_form_id, inferred_form_name,
       confidence, match_score, form_has_fills
  2. campaign_forms_full.csv        — one row per distinct form, union of:
       stored-in-folder + embedded-in-LP + campaign-inferred, with source flags.

Run:
    python campaign_form_map_report.py --folder 1234
"""

from __future__ import annotations

import argparse
import datetime
import collections
import csv
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests

from auth import MarketoAuth, load_config, log
from form_config import lp_link
from form_pages_report import asset_page, scoped_forms

HI, MID = 0.60, 0.34  # confidence thresholds
STOP = set("form the a to of on for in and or new with".split())


def toks(s: str) -> set:
    s = s.lower()
    s = re.sub(r"\b(19|20)\d{2}\b", "", s)                       # years
    s = re.sub(r"\bform\b|\(.*?\)|v\d+", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return {t for t in s.split() if t and t not in STOP and len(t) > 1}


def cache_form_ids(work_dir: Path, years):
    ids = set()
    for y in years:
        p = work_dir / f"form_fills_{y}.jsonl"
        if not p.is_file():
            continue
        for line in open(p, encoding="utf-8"):
            fid = json.loads(line).get("formid", "")
            if fid.isdigit():
                ids.add(int(fid))
    return ids


def main(argv=None):
    ap = argparse.ArgumentParser()
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
    so_id = urlsplit(base).netloc.split(".")[0]

    fol = asset_page(auth, base, "/rest/asset/v1/folders.json", {"maxDepth": 20})
    kids = collections.defaultdict(list)
    for f in fol:
        kids[(f.get("parent") or {}).get("id")].append(f)
    sub, stack = {args.folder}, [args.folder]
    while stack:
        for c in kids.get(stack.pop(), []):
            sub.add(c["id"]); stack.append(c["id"])
    programs = asset_page(auth, base, "/rest/asset/v1/programs.json")
    mine = [p for p in programs if (p.get("folder") or {}).get("value") in sub]
    prog_ids = {p["id"] for p in mine}
    scope = sub | prog_ids

    all_forms = {f["id"]: f for f in asset_page(auth, base, "/rest/asset/v1/forms.json")}
    ftok = [(fid, f.get("name", ""), toks(f.get("name", ""))) for fid, f in all_forms.items()]
    filled_ids = cache_form_ids(Path(args.out_dir) / ".work", args.years)

    # ---- 1. program -> best form match
    def best_match(pname):
        pt = toks(pname)
        best, bs = None, 0.0
        if pt:
            for fid, fn, ft in ftok:
                if not ft:
                    continue
                j = len(pt & ft) / len(pt | ft)
                if j > bs:
                    bs, best = j, (fid, fn)
        return best, bs

    map_rows = []
    inferred = collections.defaultdict(list)  # form_id -> [(program_name, conf)]
    for p in sorted(mine, key=lambda x: x.get("name", "")):
        best, score = best_match(p.get("name", ""))
        conf = "High" if score >= HI else ("Plausible" if score >= MID else "Weak/none")
        fid = best[0] if (best and score >= MID) else ""
        fname = best[1] if (best and score >= MID) else ""
        if fid:
            inferred[fid].append((p.get("name", ""), conf))
        map_rows.append({
            "program_id": p["id"],
            "program_name": p.get("name", ""),
            "inferred_form_id": fid,
            "inferred_form_name": fname,
            "confidence": conf,
            "match_score": round(score, 2),
            "form_has_fills": "yes" if fid and int(fid) in filled_ids else ("no" if fid else ""),
        })

    out_map = Path(args.out_dir) / "campaign_program_form_map.csv"
    with open(out_map, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["program_id", "program_name", "inferred_form_id",
                                           "inferred_form_name", "confidence", "match_score",
                                           "form_has_fills"])
        w.writeheader(); w.writerows(map_rows)

    # ---- 2. union one-row-per-form (stored + LP-embedded + inferred)
    stored = {f["id"]: f for f in scoped_forms(auth, base, args.folder)}
    lps = asset_page(auth, base, "/rest/asset/v1/landingPages.json")
    in_lps = [lp for lp in lps if (lp.get("folder") or {}).get("value") in scope]
    embed = collections.defaultdict(list)
    for lp in in_lps:
        rc = requests.get(base + f"/rest/asset/v1/landingPage/{lp['id']}/content.json",
                          headers=auth.auth_header(), timeout=60).json()
        for c in rc.get("result", []):
            if c.get("type") == "Form":
                cf = c.get("content")
                cid = cf.get("content") if isinstance(cf, dict) else cf
                if str(cid).isdigit():
                    embed[int(cid)].append((lp["id"], lp.get("name", "")))

    union = sorted(set(stored) | set(embed) | set(inferred),
                   key=lambda i: (all_forms.get(i, {}).get("name", "") or "").lower())
    full_rows = []
    for fid in union:
        f = all_forms.get(fid, {})
        srcs = []
        if fid in stored:
            srcs.append("stored")
        if fid in embed:
            srcs.append("LP-embedded")
        if fid in inferred:
            srcs.append("campaign-inferred")
        lp_pairs = sorted(set(embed.get(fid, [])))
        conf = ""
        if fid in inferred:
            conf = "High" if any(c == "High" for _, c in inferred[fid]) else "Plausible"
        full_rows.append({
            "form_id": fid,
            "form_name": f.get("name", "(unknown)"),
            "status": f.get("status", ""),
            "sources": " + ".join(srcs),
            "has_fills_in_cache": "yes" if int(fid) in filled_ids else "no",
            "embedded_lp_count": len(lp_pairs),
            "embedded_landing_pages": " ; ".join(
                f"{nm} [{lp_link(so_id, lid)}]" if lp_link(so_id, lid) else nm
                for lid, nm in lp_pairs),
            "inferred_from_programs": " ; ".join(f"{nm} ({c})" for nm, c in inferred.get(fid, [])),
            "inferred_confidence": conf,
        })

    out_full = Path(args.out_dir) / "campaign_forms_full.csv"
    with open(out_full, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["form_id", "form_name", "status", "sources",
                                           "has_fills_in_cache", "embedded_lp_count",
                                           "embedded_landing_pages", "inferred_from_programs",
                                           "inferred_confidence"])
        w.writeheader(); w.writerows(full_rows)

    hi = sum(1 for r in map_rows if r["confidence"] == "High")
    mid = sum(1 for r in map_rows if r["confidence"] == "Plausible")
    lo = sum(1 for r in map_rows if r["confidence"] == "Weak/none")
    log(f"\nProgram map: {len(map_rows)} programs  (High {hi} / Plausible {mid} / Weak {lo})")
    log(f"  -> {out_map}")
    log(f"Union forms: {len(full_rows)} distinct  -> {out_full}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
