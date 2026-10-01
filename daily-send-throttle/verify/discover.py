#!/usr/bin/env python3
"""Read-only inventory of your Marketo instance, for planning the daily send throttle.

Run this BEFORE building anything. It answers the three questions that
otherwise turn into build-time surprises:

  1. What are the real REST names of the fields that define your audience?
     (The display name in the UI is almost never the REST name.)
  2. Do any throttle-style fields already exist (queue, slot, sequence...)?
  3. What do the existing static lists and campaigns look like?

Strictly read-only - every call is a GET. Nothing is created or modified.

    python discover.py                          # matched fields + lists + campaigns
    python discover.py --match "priority|segment"   # also match your audience field names
    python discover.py --all-fields             # dump every field to output/fields.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auth import MarketoAuth, load_config, log  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "output"

# Names that look like a previous attempt at throttling, so you don't collide with
# it. --match adds your own audience terms on top (e.g. "priority|segment").
BASE_FIELD_PATTERN = r"queue|bucket|slot|sequence|throttl|drip|daily.?cap|send.?date"
BASE_LIST_PATTERN = r"queue|throttl|drip|sent"


class AccessDenied(RuntimeError):
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all-fields", action="store_true",
                    help="also write every field to output/fields.json")
    ap.add_argument("--match", metavar="REGEX",
                    help="extra case-insensitive regex of names to include, "
                         "e.g. 'priority|segment' (matched against fields, lists and campaigns)")
    args = ap.parse_args()

    extra = f"|{args.match}" if args.match else ""
    field_pattern = re.compile(BASE_FIELD_PATTERN + extra, re.I)
    list_pattern = re.compile(BASE_LIST_PATTERN + extra, re.I)

    cfg = load_config()
    auth = MarketoAuth(**cfg)
    base = cfg["rest_base"]
    session = requests.Session()

    def get(path: str, **params):
        """GET a REST path. Raises AccessDenied on a 603 so we can explain it once."""
        params["access_token"] = auth.token()
        resp = session.get(f"{base}{path}", params=params, timeout=60)
        try:
            body = resp.json()
        except ValueError:
            raise RuntimeError(f"{path}: non-JSON response (HTTP {resp.status_code})")
        if not body.get("success"):
            codes = {str(e.get("code")) for e in body.get("errors", [])}
            if "603" in codes:
                raise AccessDenied(path)
            raise RuntimeError(f"{path}: {body.get('errors')}")
        return body

    print(f"instance: {base}\n")

    try:
        fields = fetch_fields(get)
    except AccessDenied:
        explain_access_denied(auth)
        return 2

    print(f"=== FIELDS ({len(fields)} total) ===")
    matches = [f for f in fields
               if field_pattern.search(f["name"]) or field_pattern.search(f["display"])]
    if matches:
        print(f"{'REST NAME':<42} {'DISPLAY NAME':<42} TYPE")
        print("-" * 100)
        for f in sorted(matches, key=lambda x: x["name"].lower()):
            print(f"{f['name']:<42} {f['display']:<42} {f['dataType']}")
    else:
        print("  (no fields matched - pass --match with part of your audience field "
              "names, or rerun with --all-fields and grep)")

    print("\n>>> Record the exact REST names in marketo/BUILD-CHECKLIST.md "
          "before building the smart lists.")

    if args.all_fields:
        OUT_DIR.mkdir(exist_ok=True)
        dest = OUT_DIR / "fields.json"
        dest.write_text(json.dumps(fields, indent=2))
        print(f"\nwrote {len(fields)} fields to {dest}")

    for label, path, key, pattern in (
        ("STATIC LISTS", "/rest/v1/lists.json", "name", list_pattern),
        ("CAMPAIGNS", "/rest/v1/campaigns.json", "name", list_pattern),
    ):
        print(f"\n=== {label} (filtered) ===")
        try:
            rows = fetch_paged(get, path)
        except AccessDenied:
            print("  603 Access denied - role is missing Read-Only Asset.")
            continue
        hits = [r for r in rows if pattern.search(r.get(key, ""))]
        if not hits:
            print(f"  none of {len(rows)} matched")
        for r in hits:
            print(f"  id={r.get('id'):<8} {r.get(key)}")

    return 0


def fetch_fields(get) -> list[dict]:
    """Normalise describe2 / describe into a flat list of field dicts."""
    try:
        body = get("/rest/v1/leads/describe2.json")
        raw = body.get("result") or []
        fields = raw[0].get("fields", []) if raw else []
        out = []
        for f in fields:
            rest = f.get("rest") or {}
            out.append({
                "name": rest.get("name") or f.get("name") or "",
                "display": f.get("displayName") or "",
                "dataType": f.get("dataType") or "",
            })
        if out:
            return out
    except (AccessDenied, RuntimeError) as exc:
        if isinstance(exc, AccessDenied):
            raise
        log(f"[warn] describe2 unusable ({exc}); falling back to describe")

    body = get("/rest/v1/leads/describe.json")
    return [{
        "name": (f.get("rest") or {}).get("name") or f.get("id") or "",
        "display": f.get("displayName") or "",
        "dataType": f.get("dataType") or "",
    } for f in body.get("result") or []]


def fetch_paged(get, path: str, page_size: int = 200) -> list[dict]:
    """Walk Marketo's offset pagination to the end."""
    rows, offset = [], 0
    while True:
        body = get(path, batchSize=page_size, offset=offset)
        page = body.get("result") or []
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += page_size


def explain_access_denied(auth: MarketoAuth) -> None:
    """Print an actionable 603 diagnosis, naming the API user involved.

    The identity endpoint's `scope` is the API user's login. Getting that in
    front of whoever is debugging saves the most common wasted hour: fixing the
    role on a different API user than the credentials actually belong to.
    """
    info = auth.last_token_response or {}
    scope = info.get("scope") or "(unknown)"
    expires_in = info.get("expires_in")

    print(
        "603 Access denied on the field describe endpoint.\n"
        "\n"
        f"  API user (token scope) : {scope}\n"
        f"  client_id              : {auth.client_id[:8]}...{auth.client_id[-4:]}\n"
        f"  token expires_in       : {expires_in}s\n"
        "\n"
        "The client id/secret are valid - a token was issued - so this is a\n"
        "permissions problem, not a credentials problem. Check these in order:\n"
        "\n"
        "1. Is the role fixed on the RIGHT user? Admin > LaunchPoint, find the\n"
        "   service whose Client Id starts with the prefix above and confirm which\n"
        "   user it is tied to. It must be the one named in the token scope.\n"
        "\n"
        "2. Does that user's role actually have the Access API boxes ticked?\n"
        "   Role NAMES are arbitrary - a role called \"API Admin\" does not\n"
        "   necessarily have any Access API permissions enabled. These scripts\n"
        "   need Access API > Read-Only Person and Read-Only Asset.\n"
        "\n"
        "3. Workspace: if the role is granted in a workspace that does not hold\n"
        "   these assets, reads fail the same way.\n"
        "\n"
        "If a role change was JUST made and expires_in above is well under 3600,\n"
        "Marketo handed back a token minted before the change. Wait for it to\n"
        "expire and rerun before digging further.\n"
        "\n"
        "None of this blocks the Marketo build itself - these scripts are only\n"
        "for verification convenience. See verify/README.md.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
