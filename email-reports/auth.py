#!/usr/bin/env python3
"""Marketo access-token helper.

Generates (and caches) an OAuth bearer token from a Client ID / Client Secret
using the Marketo Identity endpoint:

    GET {REST_BASE}/identity/oauth/token?grant_type=client_credentials
        &client_id=...&client_secret=...

Use it two ways:

  * As a module:   from auth import MarketoAuth, load_config
                   auth = MarketoAuth(**load_config())
                   token = auth.token()

  * As a script:   python auth.py            # prints a fresh token
                   python auth.py --json     # prints the full token response

Configuration is read from the `.env` file that sits next to this script (copy
`.env.example` to `.env` and fill it in). Real environment variables with the
same names also work and take precedence. Nothing is hardcoded.

Required keys:
    MARKETO_CLIENT_ID
    MARKETO_CLIENT_SECRET
    MARKETO_REST_BASE      host only, e.g. https://123-ABC-456.mktorest.com
Optional:
    MARKETO_APP_BASE       Marketo UI host, used only to build clickable links
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

# Marketo error codes that mean "your token is bad / expired" -> refresh and retry.
TOKEN_ERROR_CODES = {"600", "601", "602", "603"}

# A safety margin (seconds) subtracted from the reported lifetime so we refresh
# slightly before the token actually expires.
EXPIRY_SKEW_SECONDS = 30

ENV_FILE = Path(__file__).resolve().parent / ".env"


def log(msg: str) -> None:
    """Progress / diagnostics go to stderr so stdout stays clean for piping."""
    print(msg, file=sys.stderr, flush=True)


def load_config(need_app_base: bool = False) -> dict:
    """Load and validate Marketo config from `.env` (or the process environment).

    Returns a dict with keys: client_id, client_secret, rest_base, app_base.
    Exits with a clear message if a required value is missing. Set
    `need_app_base=True` for tools that build clickable Marketo UI links.
    """
    if ENV_FILE.is_file():
        # override=False: variables already set in the real environment win.
        load_dotenv(ENV_FILE, override=False)
        log(f"[config] loaded {ENV_FILE}")
    else:
        log(f"[config] no .env found at {ENV_FILE}; relying on process environment")

    def get(name: str) -> str:
        return os.getenv(name, "").strip()

    rest_base = get("MARKETO_REST_BASE").rstrip("/")
    # The tools build full paths themselves ({host}/identity, {host}/rest/v1,
    # {host}/bulk/v1), so normalize to the bare host even if the REST endpoint
    # URL was pasted with a trailing "/rest".
    if rest_base.endswith("/rest"):
        rest_base = rest_base[: -len("/rest")]

    cfg = {
        "client_id": get("MARKETO_CLIENT_ID"),
        "client_secret": get("MARKETO_CLIENT_SECRET"),
        "rest_base": rest_base,
        "app_base": get("MARKETO_APP_BASE").rstrip("/"),
    }

    required = [
        ("MARKETO_CLIENT_ID", "client_id"),
        ("MARKETO_CLIENT_SECRET", "client_secret"),
        ("MARKETO_REST_BASE", "rest_base"),
    ]
    if need_app_base:
        required.append(("MARKETO_APP_BASE", "app_base"))

    missing = [env_name for env_name, key in required if not cfg[key]]
    if missing:
        log(
            "ERROR: missing required config value(s): "
            + ", ".join(missing)
            + f"\n       Copy .env.example to {ENV_FILE.name} and fill them in."
        )
        sys.exit(2)

    return cfg


class MarketoAuth:
    """Fetches and caches a Marketo bearer token, refreshing as needed."""

    def __init__(self, client_id: str, client_secret: str, rest_base: str, **_ignored):
        self.client_id = client_id
        self.client_secret = client_secret
        self.rest_base = rest_base.rstrip("/")
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._session = requests.Session()
        # The most recent raw token response (includes `scope`, the API user's login),
        # handy for diagnosing permission problems.
        self.last_token_response: dict | None = None

    def _fetch(self) -> dict:
        """Call the identity endpoint and return the raw JSON token response."""
        url = f"{self.rest_base}/identity/oauth/token"
        params = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        log("[auth] requesting new access token")
        resp = self._session.get(url, params=params, timeout=30)

        # The identity endpoint returns errors as JSON with a 200 *or* 4xx; try JSON first.
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise RuntimeError(f"[auth] non-JSON response from identity endpoint: {resp.text[:200]}")

        if "error" in data:
            raise RuntimeError(
                f"[auth] token request failed: {data.get('error')} - "
                f"{data.get('error_description', 'no description')}"
            )

        if "access_token" not in data:
            resp.raise_for_status()
            raise RuntimeError(f"[auth] unexpected token response: {data}")

        self.last_token_response = data
        return data

    def token(self, force_refresh: bool = False) -> str:
        """Return a valid bearer token, fetching/refreshing if necessary."""
        now = time.time()
        if force_refresh or not self._token or now >= self._expires_at:
            data = self._fetch()
            self._token = data["access_token"]
            lifetime = int(data.get("expires_in", 3600))
            self._expires_at = now + max(0, lifetime - EXPIRY_SKEW_SECONDS)
            log(f"[auth] token acquired (valid ~{lifetime}s)")
        return self._token

    def refresh(self) -> str:
        """Force a brand-new token (used after a 601/602 token error mid-run)."""
        return self.token(force_refresh=True)

    def auth_header(self) -> dict:
        """Convenience: ready-to-use Authorization header."""
        return {"Authorization": f"Bearer {self.token()}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a Marketo access token.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full token response as JSON instead of just the token.",
    )
    args = parser.parse_args(argv)

    auth = MarketoAuth(**load_config())

    if args.json:
        import json

        print(json.dumps(auth._fetch(), indent=2))
    else:
        print(auth.token())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
