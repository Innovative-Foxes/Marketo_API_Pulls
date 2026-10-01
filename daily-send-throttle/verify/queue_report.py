#!/usr/bin/env python3
"""Read-only snapshot of the daily-send-throttle queue.

This is the instrument for the runbook's test plan. After a daily draw run it
tells you - without clicking through Marketo - exactly how many people were
released, how many are still waiting, and how long the oldest has waited.

Strictly read-only: every call is a GET.

    python queue_report.py --queue-list-id 1234
    python queue_report.py --queue-list-id 1234 --sent-list-id 1235
    python queue_report.py --queue-list-id 1234 --csv output/queue_snapshot.csv
    python queue_report.py --queue-list-id 1234 --daily-cap 100

Field REST names default to the camel-cased form of the fields in the build
checklist (Throttle Queue Status -> throttleQueueStatus). Override them if your
instance generated different ones (run discover.py to find out).
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auth import MarketoAuth, load_config  # noqa: E402

DEFAULT_DAILY_CAP = 50


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queue-list-id", required=True,
                    help="id of the 'Throttle - Send Queue' static list")
    ap.add_argument("--sent-list-id", help="id of the 'Throttle - Sent' static list")
    ap.add_argument("--status-field", default="throttleQueueStatus")
    ap.add_argument("--date-field", default="throttleQueueDate")
    ap.add_argument("--slot-field", default="throttleDailySlot")
    ap.add_argument("--seq-field", default="throttleSendSequence")
    ap.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP,
                    help=f"your daily cap, used to flag slots that should have been "
                         f"released (default {DEFAULT_DAILY_CAP})")
    ap.add_argument("--csv", help="also write the raw rows to this path")
    args = ap.parse_args()
    daily_cap = args.daily_cap

    cfg = load_config()
    auth = MarketoAuth(**cfg)
    base = cfg["rest_base"]
    session = requests.Session()

    fields = ["id", "email", args.status_field, args.date_field,
              args.slot_field, args.seq_field]

    def members(list_id: str) -> list[dict]:
        """Page through a static list's members via nextPageToken."""
        rows: list[dict] = []
        token = None
        while True:
            params = {
                "access_token": auth.token(),
                "fields": ",".join(fields),
                "batchSize": 300,
            }
            if token:
                params["nextPageToken"] = token
            resp = session.get(f"{base}/rest/v1/list/{list_id}/leads.json",
                               params=params, timeout=60)
            body = resp.json()
            if not body.get("success"):
                codes = {str(e.get("code")) for e in body.get("errors", [])}
                if "603" in codes:
                    print("603 Access denied - the API role needs Read-Only Person. "
                          "See verify/README.md.", file=sys.stderr)
                    raise SystemExit(2)
                if "1003" in codes:  # unknown field
                    print(f"Marketo rejected a field name: {body.get('errors')}\n"
                          f"Run discover.py and pass the real names via "
                          f"--status-field/--date-field/--slot-field/--seq-field.",
                          file=sys.stderr)
                    raise SystemExit(2)
                print(f"error: {body.get('errors')}", file=sys.stderr)
                raise SystemExit(1)
            rows.extend(body.get("result") or [])
            token = body.get("nextPageToken")
            if not token or not body.get("result"):
                return rows

    print(f"instance: {base}\n")

    queue = members(args.queue_list_id)
    print(f"=== QUEUE (list {args.queue_list_id}) ===")
    print(f"members: {len(queue)}")

    statuses = Counter(r.get(args.status_field) or "(empty)" for r in queue)
    for status, count in statuses.most_common():
        print(f"  status {status:<14} {count}")

    dates = sorted(d for d in (r.get(args.date_field) for r in queue) if d)
    if dates:
        print(f"  oldest queued : {dates[0]}")
        print(f"  newest queued : {dates[-1]}")

    slots = [r.get(args.slot_field) for r in queue]
    drawn = [s for s in slots if isinstance(s, int)]
    if drawn:
        within = sum(1 for s in drawn if 1 <= s <= daily_cap)
        print(f"\n  last draw: {len(drawn)} of {len(queue)} carry a slot number; "
              f"max slot {max(drawn)}")
        if within:
            print(f"  !! {within} still-queued people hold a slot 1-{daily_cap}. "
                  f"Expected 0 after a clean run - they should have been sent and "
                  f"removed. Check the choice steps in '03 - Draw Handler'.")

    if args.sent_list_id:
        sent = members(args.sent_list_id)
        print(f"\n=== SENT (list {args.sent_list_id}) ===")
        print(f"members: {len(sent)}")
        overlap = {r["id"] for r in queue} & {r["id"] for r in sent}
        if overlap:
            print(f"  !! {len(overlap)} people are in BOTH lists. The 'Remove from "
                  f"List' choice step is not firing. Ids: "
                  f"{sorted(overlap)[:10]}{'...' if len(overlap) > 10 else ''}")
        else:
            print("  no overlap with the queue - good")

    if args.csv:
        dest = Path(args.csv)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(queue)
        print(f"\nwrote {len(queue)} rows to {dest}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
