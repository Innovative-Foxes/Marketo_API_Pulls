#!/usr/bin/env python3
"""Marketo REST client for the program-members report.

Wraps `requests` with the things every Marketo pull needs: bearer-token refresh
on 601/602, backoff on 606 rate-limit errors, and paging over both the asset API
(offset/maxReturn) and the lead API (nextPageToken). Credentials come from the
`.env` next to this file via auth.load_config().
"""

from __future__ import annotations

import sys
import time

import requests

from auth import MarketoAuth, load_config

# Marketo error codes meaning "token is bad/expired" -> refresh once and retry.
TOKEN_ERROR_CODES = {"600", "601", "602", "603"}
# 606 = rate limit (100 calls/20s), 615 = concurrent limit, 604 = request timeout.
RETRY_ERROR_CODES = {"604", "606", "615", "1029"}

MAX_RETRIES = 5
# Marketo allows 100 calls per 20 seconds; ~4/sec leaves plenty of headroom.
MIN_SECONDS_BETWEEN_CALLS = 0.25


def log(msg: str) -> None:
    """Progress goes to stderr so stdout stays clean for piping."""
    print(msg, file=sys.stderr, flush=True)


class MarketoClient:
    """Thin REST wrapper with token refresh, rate-limit backoff, and paging."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.rest_base = self.cfg["rest_base"]
        self.app_base = self.cfg.get("app_base", "")
        self.auth = MarketoAuth(**self.cfg)
        self._session = requests.Session()
        self._last_call = 0.0
        self.call_count = 0

    def _throttle(self) -> None:
        """Keep us comfortably under Marketo's 100-calls-per-20-seconds ceiling."""
        elapsed = time.time() - self._last_call
        if elapsed < MIN_SECONDS_BETWEEN_CALLS:
            time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)
        self._last_call = time.time()

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET {rest_base}{path} and return the parsed JSON envelope.

        Retries on token expiry and transient rate-limit errors. Raises
        RuntimeError on any other Marketo-reported failure.
        """
        url = f"{self.rest_base}{path}"
        params = dict(params or {})

        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            headers = {"Authorization": f"Bearer {self.auth.token()}"}
            self.call_count += 1
            resp = self._session.get(url, params=params, headers=headers, timeout=60)

            try:
                data = resp.json()
            except ValueError:
                resp.raise_for_status()
                raise RuntimeError(f"non-JSON response from {url}: {resp.text[:300]}")

            if data.get("success"):
                return data

            errors = data.get("errors") or []
            codes = {str(e.get("code")) for e in errors}
            detail = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errors)

            if codes & TOKEN_ERROR_CODES:
                log(f"[retry {attempt}/{MAX_RETRIES}] token error ({detail}); refreshing")
                self.auth.refresh()
                continue

            if codes & RETRY_ERROR_CODES:
                backoff = min(2 ** attempt, 30)
                log(f"[retry {attempt}/{MAX_RETRIES}] {detail}; sleeping {backoff}s")
                time.sleep(backoff)
                continue

            raise RuntimeError(f"Marketo error on {path} {params}: {detail or data}")

        raise RuntimeError(f"gave up on {path} after {MAX_RETRIES} attempts")

    def get_paged_offset(self, path: str, params: dict, page_size: int = 200):
        """Yield records from an asset-API endpoint that pages via offset/maxReturn."""
        offset = 0
        while True:
            page = dict(params, maxReturn=page_size, offset=offset)
            data = self.get(path, page)
            result = data.get("result") or []
            for record in result:
                yield record
            if len(result) < page_size:
                return
            offset += page_size

    def get_paged_token(self, path: str, params: dict, batch_size: int = 300):
        """Yield records from a lead-API endpoint that pages via nextPageToken."""
        token = None
        while True:
            page = dict(params, batchSize=batch_size)
            if token:
                page["nextPageToken"] = token
            data = self.get(path, page)
            for record in data.get("result") or []:
                yield record
            token = data.get("nextPageToken")
            # Marketo keeps handing back a token past the end, so trust
            # moreResult when it is present and fall back to a short page.
            more = data.get("moreResult")
            if more is False or not token:
                return
            if more is None and len(data.get("result") or []) < batch_size:
                return


def main() -> int:
    """Smoke test: verify config + token and print the instance identity."""
    client = MarketoClient()
    token = client.auth.token()
    log(f"[ok] token acquired: {token[:8]}...{token[-4:]}")
    # /rest/v1/stats/usage.json is a cheap authenticated call that proves the
    # credentials work against this specific instance.
    data = client.get("/rest/v1/stats/usage.json")
    print(f"REST base : {client.rest_base}")
    print(f"Usage     : {data.get('result')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
