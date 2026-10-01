#!/usr/bin/env python3
"""Program members + program statuses for every program created in a date window.

Pipeline (each stage caches to raw/ so reruns are cheap):

  1. programs  - GET /rest/asset/v1/programs.json, page through every program
                 in the instance -> raw/programs_all.json
  2. filter    - keep programs whose createdAt falls inside the window
                 -> output/programs_in_window.csv
  3. members   - for each kept program, GET /rest/v1/leads/programs/{id}.json
                 -> raw/members/{id}.json (one file per program)
  4. report    - flatten into CSVs, one row per member-per-program, with the
                 program columns on every row so it pivots cleanly in a
                 spreadsheet, plus a one-row-per-program summary.

Usage:
    python program_members_report.py --start 2025-01-01 --end 2025-12-31
    python program_members_report.py --start 2025-01-01 --end 2025-12-31 --limit 3
    python program_members_report.py --start 2025-01-01 --end 2025-12-31 --refresh
    python program_members_report.py --start 2025-01-01 --end 2025-12-31 --stage report
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from marketo_client import MarketoClient, log

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "raw"
MEMBERS_DIR = RAW_DIR / "members"
OUTPUT_DIR = HERE / "output"

PROGRAMS_CACHE = RAW_DIR / "programs_all.json"

# The program-creation window (UTC), inclusive on both ends. Set from --start /
# --end in main(); these placeholders only guard against use before then.
WINDOW_START = datetime.min.replace(tzinfo=timezone.utc)
WINDOW_END = datetime.max.replace(tzinfo=timezone.utc)

# Core lead fields. Membership data (status, dates, success) comes back in the
# record's "membership" object and does not need to be requested here.
LEAD_FIELDS = ["id", "email", "firstName", "lastName", "updatedAt"]

PROGRAM_COLUMNS = [
    "program_name",
    "program_id",
    "program_type",
    "program_channel",
    "program_status",
    "program_folder",
    "program_created_at",
    "program_url",
]

# The member exports carry email as the only lead field (data minimisation), plus
# the program and its status. Keeping the column count low also keeps large
# rosters small enough for spreadsheet tools: a 700k-row roster at 6 columns is
# about 4.2M cells / 67MB, which Google Sheets accepts (10M-cell, ~100MB limits).
MEMBER_LEAN_COLUMNS = [
    "program_name",
    "program_id",
    "program_channel",
    "email",
    "program_member_status",
    "membership_date",
]

# "Was sent this" / "was invited to" statuses: ~90% of all rows, and no
# engagement signal. The _ALL_ roster and the summary both keep them; only the
# _ENGAGED_ cut drops them.
PASSIVE_STATUSES = {"Member", "Invited"}

# Marketo's flag for clicks from security scanners rather than humans. Kept as a
# visible status in the detail file, but never counted toward engagement totals.
NON_HUMAN_STATUSES = {"Bot Click"}

# Preferred column order for the summary matrix; anything unexpected gets
# appended alphabetically so a new Marketo status can never be silently dropped.
STATUS_ORDER = [
    "Member",
    "Invited",
    "Opened",
    "Clicked",
    "Bot Click",
    "Engaged",
    "Registered",
    "Attended",
    "Attended On-Demand",
    "No Show",
    "Downloaded PDF",
    "Filled-Out Form",
    "Filled-Out Form (Gated)",
    "Registration Error",
]

SUMMARY_BASE_COLUMNS = [
    "program_name",
    "program_id",
    "program_type",
    "program_channel",
    "program_status",
    "program_folder",
    "program_created_at",
    "total_members",
    "engaged_total",
    "engagement_rate",
    "program_url",
]

CSV_COLUMNS = [
    "program_name",
    "program_id",
    "program_type",
    "program_channel",
    "program_status",
    "program_folder",
    "program_created_at",
    "program_url",
    "lead_id",
    "email",
    "first_name",
    "last_name",
    "program_member_status",
    "membership_date",
    "reached_success",
    "acquired_by",
    "is_exhausted",
    "nurture_cadence",
    "stream",
    "lead_updated_at",
]

# Marketo returns e.g. "2025-06-12T15:30:00Z+0000" (asset API) or
# "2025-06-12T15:30:00Z" (lead API). Both are UTC.
_DT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")


def parse_marketo_dt(value: str | None) -> datetime | None:
    """Parse a Marketo timestamp to an aware UTC datetime, or None if unparseable."""
    if not value:
        return None
    m = _DT_RE.match(str(value))
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)}T{m.group(2)}", "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc
    )


def fetch_programs(client: MarketoClient, refresh: bool = False) -> list[dict]:
    """Page through every program in the instance, caching the raw list."""
    if PROGRAMS_CACHE.is_file() and not refresh:
        programs = json.loads(PROGRAMS_CACHE.read_text())
        log(f"[programs] using cache: {len(programs)} programs ({PROGRAMS_CACHE.name})")
        return programs

    log("[programs] fetching all programs from /rest/asset/v1/programs.json")
    programs = []
    for program in client.get_paged_offset("/rest/asset/v1/programs.json", {}):
        programs.append(program)
        if len(programs) % 200 == 0:
            log(f"[programs] {len(programs)} so far...")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROGRAMS_CACHE.write_text(json.dumps(programs, indent=2))
    log(f"[programs] {len(programs)} total -> {PROGRAMS_CACHE}")
    return programs


def program_row(program: dict) -> dict:
    """Flatten the program fields we repeat on every member row."""
    folder = program.get("folder") or {}
    return {
        "program_name": program.get("name", ""),
        "program_id": program.get("id", ""),
        "program_type": program.get("type", ""),
        "program_channel": program.get("channel", ""),
        "program_status": program.get("status", ""),
        # Marketo returns the folder as {"type","value" (id),"folderName"}.
        "program_folder": folder.get("folderName") or folder.get("name") or "",
        "program_created_at": program.get("createdAt", ""),
        "program_url": program.get("url", ""),
    }


def filter_programs(programs: list[dict]) -> list[dict]:
    """Keep programs created inside the window, sorted by creation date."""
    kept, undated = [], 0
    for program in programs:
        created = parse_marketo_dt(program.get("createdAt"))
        if created is None:
            undated += 1
            continue
        if WINDOW_START <= created <= WINDOW_END:
            kept.append(program)

    kept.sort(key=lambda p: (parse_marketo_dt(p.get("createdAt")) or WINDOW_START))
    log(
        f"[filter] {len(kept)} of {len(programs)} programs created between "
        f"{WINDOW_START:%Y-%m-%d} and {WINDOW_END:%Y-%m-%d}"
        + (f" ({undated} skipped: unparseable createdAt)" if undated else "")
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    inventory = OUTPUT_DIR / "programs_in_window.csv"
    with inventory.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PROGRAM_COLUMNS)
        writer.writeheader()
        for program in kept:
            writer.writerow(program_row(program))
    log(f"[filter] program inventory -> {inventory}")
    return kept


def fetch_members(client: MarketoClient, program: dict, refresh: bool = False) -> dict:
    """Fetch every member of one program, caching the raw records per program.

    Returns {"members": [...]} on success or {"error": "..."} if Marketo refused
    the program (deleted, archived, or a type that has no membership).
    """
    program_id = program["id"]
    cache = MEMBERS_DIR / f"{program_id}.json"
    if cache.is_file() and not refresh:
        return json.loads(cache.read_text())

    payload: dict
    try:
        members = list(
            client.get_paged_token(
                f"/rest/v1/leads/programs/{program_id}.json",
                {"fields": ",".join(LEAD_FIELDS)},
            )
        )
        payload = {"members": members}
    except RuntimeError as exc:
        log(f"[members] program {program_id} failed: {exc}")
        payload = {"members": [], "error": str(exc)}

    MEMBERS_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(payload, indent=2))
    return payload


def member_row(program: dict, member: dict) -> dict:
    """Combine one program + one member record into a flat CSV row."""
    membership = member.get("membership") or {}
    row = program_row(program)
    row.update(
        {
            "lead_id": member.get("id", ""),
            "email": member.get("email") or "",
            "first_name": member.get("firstName") or "",
            "last_name": member.get("lastName") or "",
            "program_member_status": membership.get("progressionStatus", ""),
            "membership_date": membership.get("membershipDate", ""),
            "reached_success": membership.get("reachedSuccess", ""),
            "acquired_by": membership.get("acquiredBy", ""),
            "is_exhausted": membership.get("isExhausted", ""),
            "nurture_cadence": membership.get("nurtureCadence", ""),
            "stream": membership.get("stream", ""),
            "lead_updated_at": member.get("updatedAt", ""),
        }
    )
    return row


def collect_rows(programs: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Read the cached member files and pair each program with its flat rows."""
    collected = []
    for program in programs:
        cache = MEMBERS_DIR / f"{program['id']}.json"
        members = []
        if cache.is_file():
            members = (json.loads(cache.read_text()).get("members")) or []
        # Sort by status then email so each program's block reads well.
        members.sort(
            key=lambda m: (
                (m.get("membership") or {}).get("progressionStatus") or "",
                (m.get("email") or "").lower(),
            )
        )
        collected.append((program_row(program), [member_row(program, m) for m in members]))
    return collected


def _out(name: str) -> Path:
    """Build a dated output path."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / (
        f"program_{name}_{WINDOW_START:%Y-%m-%d}_to_{WINDOW_END:%Y-%m-%d}.csv"
    )


def write_members_csv(
    collected, path: Path, skip_passive: bool, columns: list[str] | None = None
) -> int:
    """Write member rows grouped by program. Returns the row count written."""
    total = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=columns or CSV_COLUMNS, extrasaction="ignore"
        )
        writer.writeheader()
        for _program, rows in collected:
            for row in rows:
                if skip_passive and row["program_member_status"] in PASSIVE_STATUSES:
                    continue
                writer.writerow(row)
                total += 1
    return total


def write_summary_csv(collected, path: Path) -> int:
    """Write one row per program: totals, engagement rate, and a status matrix."""
    # Every status actually present, in preferred order, unknowns appended.
    seen = {row["program_member_status"] or "(none)" for _p, rows in collected for row in rows}
    statuses = [s for s in STATUS_ORDER if s in seen]
    statuses += sorted(seen - set(statuses))

    columns = SUMMARY_BASE_COLUMNS + [f"status: {s}" for s in statuses]

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for program, rows in collected:
            counts = Counter((row["program_member_status"] or "(none)") for row in rows)
            total = len(rows)
            engaged = sum(
                n
                for status, n in counts.items()
                if status not in PASSIVE_STATUSES and status not in NON_HUMAN_STATUSES
            )
            record = {k: program.get(k, "") for k in SUMMARY_BASE_COLUMNS}
            record["total_members"] = total
            record["engaged_total"] = engaged
            record["engagement_rate"] = round(engaged / total, 4) if total else 0
            for status in statuses:
                record[f"status: {status}"] = counts.get(status, 0)
            writer.writerow(record)

    return len(collected)


def build_report(programs: list[dict]) -> Path:
    """Write the three report files from the cached member data."""
    collected = collect_rows(programs)
    with_members = sum(1 for _p, rows in collected if rows)
    empty = len(collected) - with_members

    all_path = _out("members_ALL")
    all_rows = write_members_csv(
        collected, all_path, skip_passive=False, columns=MEMBER_LEAN_COLUMNS
    )

    engaged_path = _out("members_ENGAGED")
    engaged_rows = write_members_csv(
        collected, engaged_path, skip_passive=True, columns=MEMBER_LEAN_COLUMNS
    )

    summary_path = _out("summary")
    summary_rows = write_summary_csv(collected, summary_path)

    log(
        f"[report] {all_rows} total member rows across {with_members} programs "
        f"({empty} programs had no members)"
    )
    log(f"[report] full roster       : {all_rows} rows -> {all_path.name}")
    log(f"[report] engagement detail : {engaged_rows} rows -> {engaged_path.name}")
    log(f"[report] program summary   : {summary_rows} rows -> {summary_path.name}")
    return all_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--start",
        required=True,
        metavar="YYYY-MM-DD",
        help="First day of the window: programs created on/after this date (UTC) are included.",
    )
    parser.add_argument(
        "--end",
        required=True,
        metavar="YYYY-MM-DD",
        help="Last day of the window (inclusive, through 23:59:59 UTC).",
    )
    parser.add_argument(
        "--passive-status",
        action="append",
        metavar="STATUS",
        help=(
            "Program status that means 'was sent / invited' rather than engaged "
            "(repeatable). Excluded from the ENGAGED file and from engagement totals. "
            "Default: Member, Invited."
        ),
    )
    parser.add_argument(
        "--non-human-status",
        action="append",
        metavar="STATUS",
        help=(
            "Program status for non-human activity such as security-scanner clicks "
            "(repeatable). Kept in the ENGAGED file but not counted as engagement. "
            "Default: Bot Click."
        ),
    )
    parser.add_argument(
        "--stage",
        choices=["all", "programs", "members", "report"],
        default="all",
        help="Run one stage only. 'report' rebuilds the CSV from cached data.",
    )
    parser.add_argument(
        "--limit", type=int, help="Only process the first N in-window programs."
    )
    parser.add_argument(
        "--refresh", action="store_true", help="Ignore caches and refetch from Marketo."
    )
    args = parser.parse_args()

    global WINDOW_START, WINDOW_END, PASSIVE_STATUSES, NON_HUMAN_STATUSES
    try:
        WINDOW_START = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        WINDOW_END = datetime.strptime(args.end, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
    except ValueError:
        parser.error("--start and --end must be dates in YYYY-MM-DD format")
    if WINDOW_END < WINDOW_START:
        parser.error("--end must not be before --start")
    if args.passive_status:
        PASSIVE_STATUSES = set(args.passive_status)
    if args.non_human_status:
        NON_HUMAN_STATUSES = set(args.non_human_status)

    client = MarketoClient()

    programs = fetch_programs(client, refresh=args.refresh)
    kept = filter_programs(programs)
    if args.limit:
        kept = kept[: args.limit]
        log(f"[limit] processing first {len(kept)} programs only")

    if args.stage in ("all", "members"):
        for i, program in enumerate(kept, 1):
            payload = fetch_members(client, program, refresh=args.refresh)
            count = len(payload.get("members") or [])
            log(f"[members] {i}/{len(kept)} {program.get('name')} -> {count} members")

    if args.stage in ("all", "report", "members"):
        build_report(kept)

    log(f"[done] {client.call_count} API calls made")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
