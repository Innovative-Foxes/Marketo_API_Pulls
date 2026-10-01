#!/usr/bin/env python3
"""Every program/campaign that stamps an MQL reason, categorized by reason.

All calls are GET. Nothing in this tool can modify the Marketo instance.

How a reason gets stamped
-------------------------
A smart campaign flow step of type "Change Data Value" sets the lead field
"Last MQL Reason" (or "MQL Reason"). The value is usually NOT a literal string —
it is usually a program token, e.g. {{my.Last MQL Reason}}, whose
real value is defined on the program or inherited from a parent folder. So the
reason can only be determined by resolving that token per program.

Pipeline (each stage caches to raw/, so a rerun resumes and costs no API calls):

  1. campaigns - GET /rest/asset/v1/smartCampaigns.json (paged)
                 -> raw/smart_campaigns_all.json
  2. scope     - keep campaigns that can actually fire (active, or Recurring /
                 Single Run / Requested)
  3. flows     - GET /rest/asset/v1/smartCampaign/{id}.json per campaign, keep
                 the Change Data Value steps targeting an MQL reason field
                 -> raw/flow_scan.jsonl (append-only, resumable)
  4. tokens    - for each parent program of a stamping campaign:
                 GET /rest/asset/v1/program/{id}.json           (metadata)
                 GET /rest/asset/v1/folder/{id}/tokens.json?type=Program
                 which returns inherited tokens already resolved
                 -> raw/program_tokens.json
  5. report    - resolve each step's value and write the two CSVs

Usage:
    python mql_reason_report.py                 # full run
    python mql_reason_report.py --limit 50      # smoke test
    python mql_reason_report.py --stage report  # rebuild CSVs from cache
    python mql_reason_report.py --scope all     # widen (see --help for cost)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from marketo_client import ReadOnlyMarketoClient, log

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "raw"
OUTPUT_DIR = HERE / "output"

CAMPAIGNS_CACHE = RAW_DIR / "smart_campaigns_all.json"
FLOW_SCAN = RAW_DIR / "flow_scan.jsonl"
TOKENS_CACHE = RAW_DIR / "program_tokens.json"

# Flow steps match on the field's DISPLAY name (the label shown in Marketo), not
# its REST/API name. These two are common and easy to confuse, so both are scanned
# by default and the detail CSV records which one each step targets. Override with
# --field if your instance names the field(s) differently.
DEFAULT_REASON_FIELDS = ["Last MQL Reason", "MQL Reason"]
REASON_FIELDS = {name.lower(): name for name in DEFAULT_REASON_FIELDS}

CHANGE_DATA_VALUE = "Change Data Value"

# Campaign statuses that still execute even when isActive is false.
RUNNING_STATUSES = {"Recurring Run", "Single Run", "Requested"}

# {{my.Some Token}} -> "Some Token"; the tokens API returns names without the
# braces or the "my." prefix.
MY_TOKEN_RE = re.compile(r"^\{\{my\.([^}]+)\}\}$", re.IGNORECASE)
# Any other token family ({{lead.X}}, {{company.X}}, {{system.X}}, ...) resolves
# per-lead at send time and has no single static value.
OTHER_TOKEN_RE = re.compile(r"^\{\{([a-z]+)\.([^}]+)\}\}$", re.IGNORECASE)

DETAIL_COLUMNS = [
    "mql_reason",
    "reason_field",
    "value_kind",
    "raw_value",
    "program_name",
    "program_id",
    "program_channel",
    "program_folder",
    "campaign_name",
    "campaign_id",
    "campaign_type",
    "campaign_status",
    "campaign_is_active",
    "workspace",
    "token_defined_on",
    "campaign_url",
    "program_url",
    "campaign_updated_at",
]

SUMMARY_COLUMNS = [
    "mql_reason",
    "reason_field",
    "value_kind",
    "campaign_count",
    "active_campaign_count",
    "program_count",
    "programs",
]


def scope_campaigns(campaigns: list[dict], scope: str) -> list[dict]:
    """Filter the campaign list down to the requested scope."""
    if scope == "active":
        kept = [c for c in campaigns if c.get("isActive")]
    elif scope == "active-plus":
        kept = [
            c
            for c in campaigns
            if c.get("isActive") or c.get("status") in RUNNING_STATUSES
        ]
    elif scope == "non-system":
        kept = [c for c in campaigns if not c.get("isSystem") and c.get("parentProgramId")]
    elif scope == "all":
        kept = list(campaigns)
    else:
        raise ValueError(f"unknown scope: {scope}")
    kept.sort(key=lambda c: c.get("id") or 0)
    log(f"[scope] {scope}: {len(kept):,} of {len(campaigns):,} smart campaigns")
    return kept


def fetch_campaign_list(client: ReadOnlyMarketoClient, refresh: bool = False) -> list[dict]:
    """Page through every smart campaign in the instance, caching the list."""
    if CAMPAIGNS_CACHE.is_file() and not refresh:
        campaigns = json.loads(CAMPAIGNS_CACHE.read_text())
        log(f"[campaigns] using cache: {len(campaigns):,} campaigns")
        return campaigns

    log("[campaigns] listing all smart campaigns")
    campaigns = []
    for record in client.get_paged_offset("/rest/asset/v1/smartCampaigns.json", {}):
        campaigns.append(
            {
                k: record.get(k)
                for k in (
                    "id", "name", "status", "type", "isActive", "isSystem",
                    "parentProgramId", "workspace", "updatedAt", "computedUrl",
                )
            }
        )
        if len(campaigns) % 2000 == 0:
            log(f"[campaigns] {len(campaigns):,} so far...")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    CAMPAIGNS_CACHE.write_text(json.dumps(campaigns, indent=2))
    log(f"[campaigns] {len(campaigns):,} total -> {CAMPAIGNS_CACHE.name}")
    return campaigns


def extract_reason_steps(full: dict) -> list[dict]:
    """Pull the Change Data Value steps that target an MQL reason field.

    One flow step can carry several stepChoices (Marketo's if/then branches),
    each with its own value, so every choice becomes its own record.
    """
    steps = ((full.get("flow") or {}).get("steps")) or []
    found = []
    for step in steps:
        if step.get("activityTypeName") != CHANGE_DATA_VALUE:
            continue
        for choice in step.get("stepChoice") or []:
            attrs = {a.get("name"): a.get("value") for a in choice.get("attributes") or []}
            attr_name = str(attrs.get("Attribute Name") or "").strip()
            field = REASON_FIELDS.get(attr_name.lower())
            if not field:
                continue
            found.append(
                {
                    "reason_field": field,
                    "raw_value": attrs.get("New Value"),
                    "step_id": step.get("id"),
                    "choice_id": choice.get("id"),
                }
            )
    return found


def load_scanned() -> dict:
    """Read the resumable flow scan. Errored campaigns are NOT treated as done.

    That distinction matters: a campaign skipped because of a transient API
    error must be retried on the next run, or it silently vanishes from the audit.
    """
    done: dict = {}
    if not FLOW_SCAN.is_file():
        return done
    for line in FLOW_SCAN.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        cid = record.get("campaign_id")
        if cid is None:
            continue
        if record.get("error"):
            # Keep only if we have not since succeeded for this campaign.
            done.setdefault(cid, record)
        else:
            done[cid] = record
    current = sorted(REASON_FIELDS)
    return {
        cid: r
        for cid, r in done.items()
        if not r.get("error") and r.get("fields") == current
    }


def scan_flows(client: ReadOnlyMarketoClient, campaigns: list[dict]) -> dict:
    """Fetch each campaign's flow and record any MQL-reason steps."""
    scanned = load_scanned()
    todo = [c for c in campaigns if c["id"] not in scanned]
    log(f"[flows] {len(scanned):,} already scanned, {len(todo):,} to go")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    errors = 0
    with FLOW_SCAN.open("a", encoding="utf-8") as fh:
        for i, campaign in enumerate(todo, 1):
            cid = campaign["id"]
            try:
                data = client.get(f"/rest/asset/v1/smartCampaign/{cid}.json")
                full = (data.get("result") or [{}])[0]
                record = {
                    "campaign_id": cid,
                    "campaign_name": full.get("name") or campaign.get("name"),
                    "campaign_type": full.get("type") or campaign.get("type"),
                    "campaign_status": full.get("status") or campaign.get("status"),
                    "campaign_is_active": full.get("isActive", campaign.get("isActive")),
                    "workspace": full.get("workspace") or campaign.get("workspace"),
                    "parent_program_id": full.get("parentProgramId") or campaign.get("parentProgramId"),
                    "campaign_url": full.get("computedUrl") or campaign.get("computedUrl") or "",
                    "campaign_updated_at": full.get("updatedAt") or campaign.get("updatedAt"),
                    "total_flow_steps": len(((full.get("flow") or {}).get("steps")) or []),
                    "reason_steps": extract_reason_steps(full),
                    # Which fields this record was scanned for, so a later run with
                    # different --field values re-scans instead of reusing it.
                    "fields": sorted(REASON_FIELDS),
                }
            except RuntimeError as exc:
                errors += 1
                record = {"campaign_id": cid, "campaign_name": campaign.get("name"), "error": str(exc)}
                log(f"[flows] campaign {cid} failed: {exc}")

            fh.write(json.dumps(record) + "\n")
            fh.flush()
            scanned[cid] = record

            if i % 100 == 0 or i == len(todo):
                hits = sum(1 for r in scanned.values() if r.get("reason_steps"))
                log(f"[flows] {i:,}/{len(todo):,} scanned (stamping campaigns so far: {hits}, errors: {errors})")

    if errors:
        log(f"[flows] {errors} campaigns errored; rerun to retry just those")
    return {cid: r for cid, r in scanned.items() if not r.get("error")}


def _tokens_at(client: ReadOnlyMarketoClient, fid, ftype: str, cache: dict) -> dict:
    """Tokens defined at one program or folder, cached by (type, id).

    Programs share folders, so caching here keeps the ancestry walk cheap.
    """
    key = f"{ftype}:{fid}"
    if key in cache:
        return cache[key]
    data = client.get(
        f"/rest/asset/v1/folder/{fid}/tokens.json",
        {"type": ftype},
        allow_codes={702, 610, 709},
    )
    tokens = {}
    for group in data.get("result") or []:
        for token in group.get("tokens") or []:
            name = token.get("name")
            if name and name not in tokens:
                tokens[name] = {
                    "value": token.get("value"),
                    "inherited": bool(token.get("inherited")),
                }
    cache[key] = tokens
    return tokens


def _folder_meta(client: ReadOnlyMarketoClient, fid, cache: dict) -> dict:
    """A folder's name and parent id, cached."""
    key = f"meta:{fid}"
    if key in cache:
        return cache[key]
    meta = {"name": "", "parent_id": None}
    data = client.get(f"/rest/asset/v1/folder/{fid}.json", allow_codes={702, 610})
    folder = (data.get("result") or [{}])[0] or {}
    parent = folder.get("parent") or {}
    meta = {"name": folder.get("name") or "", "parent_id": parent.get("id") or parent.get("value")}
    cache[key] = meta
    return meta


def fetch_program_context(
    client: ReadOnlyMarketoClient, program_ids: list[int], refresh: bool = False
) -> dict:
    """Fetch metadata + fully resolved tokens for each program.

    Token resolution has to merge two sources, because the program-level
    endpoint is not reliably complete: for some programs it returns inherited
    tokens, for others only the program's own. So we start with the program's
    tokens and then walk up the folder ancestry, adding any name we have not
    seen yet. Nearest definition wins, which is how Marketo itself resolves a
    my-token. Each program/folder lookup is cached, and folders are shared
    across programs, so the walk costs little.
    """
    cache = json.loads(TOKENS_CACHE.read_text()) if TOKENS_CACHE.is_file() and not refresh else {}
    lookup: dict = {}  # per-run cache of program/folder token sets and folder metadata
    todo = [pid for pid in program_ids if str(pid) not in cache]
    log(f"[programs] {len(cache)} cached, {len(todo)} to fetch")

    for i, pid in enumerate(todo, 1):
        entry: dict = {"program_id": pid}
        try:
            data = client.get(f"/rest/asset/v1/program/{pid}.json")
            program = (data.get("result") or [{}])[0]
            folder = program.get("folder") or {}
            entry.update(
                {
                    "program_name": program.get("name") or "",
                    "program_channel": program.get("channel") or "",
                    "program_folder": folder.get("folderName") or "",
                    "program_folder_id": folder.get("value"),
                    "program_url": program.get("url") or "",
                }
            )
        except RuntimeError as exc:
            entry["error"] = str(exc)

        tokens: dict = {}

        def merge(found: dict, source: str) -> None:
            for name, token in found.items():
                if name not in tokens:
                    tokens[name] = dict(token, source=source)

        # 1. The program's own token set (may or may not include inherited ones).
        merge(_tokens_at(client, pid, "Program", lookup), "program")

        # 2. Walk up the folder ancestry, nearest first.
        fid = entry.get("program_folder_id")
        depth = 0
        while fid and depth < 12:
            meta = _folder_meta(client, fid, lookup)
            label = f"folder: {meta['name']}" if meta["name"] else f"folder {fid}"
            merge(_tokens_at(client, fid, "Folder", lookup), label)
            fid = meta["parent_id"]
            depth += 1

        entry["tokens"] = tokens
        cache[str(pid)] = entry

        if i % 25 == 0 or i == len(todo):
            log(f"[programs] {i}/{len(todo)} fetched ({client.call_count:,} calls so far)")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    TOKENS_CACHE.write_text(json.dumps(cache, indent=2))
    return cache


def resolve_value(raw_value, program: dict) -> tuple[str, str, str]:
    """Turn a flow step's New Value into (reason, value_kind, token_defined_on).

    value_kind is one of:
      literal          - a plain string typed into the flow step
      program token    - {{my.X}} resolved from the program's tokens
      unresolved token - {{my.X}} with no matching token found
      dynamic token    - {{lead.X}} etc., which varies per lead
      empty            - the step sets nothing
    """
    raw = "" if raw_value is None else str(raw_value).strip()
    if not raw:
        return "(empty)", "empty", ""

    match = MY_TOKEN_RE.match(raw)
    if match:
        token_name = match.group(1).strip()
        token = (program.get("tokens") or {}).get(token_name)
        if token is None:
            # Try a case-insensitive match before giving up.
            for name, value in (program.get("tokens") or {}).items():
                if name.strip().lower() == token_name.lower():
                    token = value
                    break
        if token is None:
            return "(unresolved: no such token)", "unresolved token", ""
        value = token.get("value")
        if value is None or not str(value).strip():
            return "(unresolved: token is empty)", "unresolved token", token.get("source", "")
        return str(value).strip(), "program token", token.get("source", "")

    if OTHER_TOKEN_RE.match(raw):
        return f"(dynamic: {raw})", "dynamic token", ""

    return raw, "literal", ""


def build_rows(scanned: dict, programs: dict) -> list[dict]:
    """Flatten scanned campaigns + resolved tokens into detail rows."""
    rows = []
    for record in scanned.values():
        steps = record.get("reason_steps") or []
        if not steps:
            continue
        pid = record.get("parent_program_id")
        program = programs.get(str(pid), {}) if pid else {}
        for step in steps:
            reason, kind, defined_on = resolve_value(step.get("raw_value"), program)
            rows.append(
                {
                    "mql_reason": reason,
                    "reason_field": step["reason_field"],
                    "value_kind": kind,
                    "raw_value": "" if step.get("raw_value") is None else str(step["raw_value"]),
                    "program_name": program.get("program_name", "(no parent program)" if not pid else ""),
                    "program_id": pid or "",
                    "program_channel": program.get("program_channel", ""),
                    "program_folder": program.get("program_folder", ""),
                    "campaign_name": record.get("campaign_name", ""),
                    "campaign_id": record.get("campaign_id", ""),
                    "campaign_type": record.get("campaign_type", ""),
                    "campaign_status": record.get("campaign_status", ""),
                    "campaign_is_active": record.get("campaign_is_active", ""),
                    "workspace": record.get("workspace", ""),
                    "token_defined_on": defined_on,
                    "campaign_url": record.get("campaign_url", ""),
                    "program_url": program.get("program_url", ""),
                    "campaign_updated_at": record.get("campaign_updated_at", ""),
                }
            )

    # Sort so the CSV groups by reason on sight.
    rows.sort(
        key=lambda r: (
            r["mql_reason"].lower(),
            r["reason_field"],
            (r["program_name"] or "").lower(),
            (r["campaign_name"] or "").lower(),
        )
    )
    return rows


def write_csvs(rows: list[dict]) -> tuple[Path, Path]:
    """Write the detail CSV and the reason-summary CSV."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    detail_path = OUTPUT_DIR / "mql_reason_detail.csv"
    summary_path = OUTPUT_DIR / "mql_reason_summary.csv"

    with detail_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=DETAIL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    groups: dict = defaultdict(
        lambda: {"campaigns": set(), "active": set(), "programs": {}, "kinds": Counter()}
    )
    for row in rows:
        key = (row["mql_reason"], row["reason_field"])
        group = groups[key]
        group["campaigns"].add(row["campaign_id"])
        if row["campaign_is_active"] is True:
            group["active"].add(row["campaign_id"])
        if row["program_id"] != "":
            group["programs"][row["program_id"]] = row["program_name"]
        group["kinds"][row["value_kind"]] += 1

    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        for (reason, field), group in sorted(
            groups.items(), key=lambda kv: (-len(kv[1]["campaigns"]), kv[0][0].lower())
        ):
            names = sorted({n for n in group["programs"].values() if n}, key=str.lower)
            writer.writerow(
                {
                    "mql_reason": reason,
                    "reason_field": field,
                    "value_kind": ", ".join(sorted(group["kinds"])),
                    "campaign_count": len(group["campaigns"]),
                    "active_campaign_count": len(group["active"]),
                    "program_count": len(group["programs"]),
                    "programs": " | ".join(names),
                }
            )

    return detail_path, summary_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scope",
        choices=["active", "active-plus", "non-system", "all"],
        default="active-plus",
        help=(
            "Which campaigns to scan. Each campaign costs one API call, so wider "
            "scopes use far more of your daily quota. active = isActive only; "
            "active-plus (default) = active plus Recurring Run / Single Run / "
            "Requested, i.e. everything that can still fire; non-system = every "
            "non-system campaign that belongs to a program; all = every smart "
            "campaign, including system ones."
        ),
    )
    parser.add_argument(
        "--field",
        action="append",
        metavar="NAME",
        help=(
            "Display name of a lead field that holds the MQL reason, exactly as "
            "shown in Marketo (repeatable). Default: 'Last MQL Reason' and "
            "'MQL Reason'."
        ),
    )
    parser.add_argument("--limit", type=int, help="Only scan the first N in-scope campaigns.")
    parser.add_argument("--refresh", action="store_true", help="Ignore caches and refetch.")
    parser.add_argument(
        "--stage",
        choices=["all", "campaigns", "flows", "report"],
        default="all",
        help="'report' rebuilds the CSVs from cache with zero API calls.",
    )
    args = parser.parse_args()

    global REASON_FIELDS
    if args.field:
        REASON_FIELDS = {name.strip().lower(): name.strip() for name in args.field if name.strip()}
    log(f"[fields] looking for steps that set: {', '.join(REASON_FIELDS.values())}")

    client = ReadOnlyMarketoClient()

    campaigns = fetch_campaign_list(client, refresh=args.refresh)
    in_scope = scope_campaigns(campaigns, args.scope)
    if args.limit:
        in_scope = in_scope[: args.limit]
        log(f"[limit] scanning first {len(in_scope):,} campaigns only")

    if args.stage == "campaigns":
        return 0

    if args.stage in ("all", "flows"):
        scanned = scan_flows(client, in_scope)
    else:
        scanned = load_scanned()

    in_scope_ids = {c["id"] for c in in_scope}
    scanned = {cid: r for cid, r in scanned.items() if cid in in_scope_ids}
    stamping = {cid: r for cid, r in scanned.items() if r.get("reason_steps")}
    log(f"[flows] {len(stamping):,} of {len(scanned):,} scanned campaigns stamp an MQL reason")

    program_ids = sorted({r["parent_program_id"] for r in stamping.values() if r.get("parent_program_id")})
    programs = fetch_program_context(client, program_ids, refresh=args.refresh)

    rows = build_rows(stamping, programs)
    detail_path, summary_path = write_csvs(rows)

    reasons = {r["mql_reason"] for r in rows}
    log(f"[report] {len(rows):,} stamping steps, {len(reasons)} distinct reasons")
    log(f"[report] detail  -> {detail_path.name}")
    log(f"[report] summary -> {summary_path.name}")
    log(f"[done] {client.call_count:,} API calls made (all GET)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
