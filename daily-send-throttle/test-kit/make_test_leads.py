#!/usr/bin/env python3
"""Generate a Marketo-importable CSV of throttle test leads.

Every address is plus-addressed onto a real inbox you control, so any stray email
lands with you. The "+throttletest" marker also lets you add a safety filter to the
test-mode campaigns (Email Address contains +throttletest).

    python3 make_test_leads.py --mailbox you --domain example.com --count 51 \\
        --audience "Lead Segment=Priority" --out demo1_51_leads.csv

--audience adds a column with a fixed value to every lead, so the leads match your
queue's audience criteria. Repeat it for several fields. Use the field's display
name as the CSV header Marketo's import will map from.
"""
import argparse
import csv

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--count", type=int, required=True, help="how many leads to generate")
ap.add_argument("--start", type=int, default=1,
                help="first index, so batches never reuse an address")
ap.add_argument("--mailbox", required=True, help="local part of a real inbox you control")
ap.add_argument("--domain", required=True, help="domain of that inbox, e.g. example.com")
ap.add_argument("--audience", action="append", default=[], metavar="FIELD=VALUE",
                help="a field/value that qualifies a lead for the queue (repeatable)")
ap.add_argument("--out", required=True, help="CSV file to write")
a = ap.parse_args()

audience = []
for item in a.audience:
    if "=" not in item:
        ap.error(f"--audience expects FIELD=VALUE, got {item!r}")
    field, value = item.split("=", 1)
    audience.append((field.strip(), value.strip()))

with open(a.out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Email Address", "First Name", "Last Name", "Company Name"]
               + [field for field, _ in audience])
    for i in range(a.start, a.start + a.count):
        w.writerow([f"{a.mailbox}+throttletest{i:03d}@{a.domain}", f"ThrottleTest{i:03d}",
                    "Throttle", "Throttle Test"] + [value for _, value in audience])
print(f"wrote {a.count} leads ({a.start}..{a.start + a.count - 1}) to {a.out}")
