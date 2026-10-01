#!/usr/bin/env python3
"""Read-only Marketo REST client for the MQL-reason report.

Deliberately GET-only: there is no method on this class that can write to the
instance, so the audit cannot change anything in Marketo no matter how it is driven.

Credentials come from the `.env` next to this file via auth.load_config().
"""

from __future__ import annotations

import sys
import time

import requests

from auth import MarketoAuth, load_config

TOKEN_ERROR_CODES = {"600", "601", "602", "603"}
# 606 = rate limit (100 calls/20s), 615 = concurrency, 604 = timeout, 1029 = too many jobs.
RETRY_ERROR_CODES = {"604", "606", "615", "1029"}
MAX_RETRIES = 5

# Marketo instances share one daily quota across every integration, so stay well
# under the 100-calls-per-20-seconds ceiling rather than racing it.
MIN_SECONDS_BETWEEN_CALLS = 0.25


def log(msg: str) -> None:
    """Progress goes to stderr so stdout stays clean for piping."""
    print(msg, file=sys.stderr, flush=True)


class ReadOnlyMarketoClient:
    """GET-only Marketo wrapper with token refresh and rate-limit backoff."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.rest_base = self.cfg["rest_base"]
        self.app_base = self.cfg.get("app_base", "")
        self.auth = MarketoAuth(**self.cfg)
        self._session = requests.Session()
        self._last_call = 0.0
        self.call_count = 0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_call
        if elapsed < MIN_SECONDS_BETWEEN_CALLS:
            time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)
        self._last_call = time.time()

    def get(self, path: str, params: dict | None = None, allow_codes: set | None = None) -> dict:
        """GET {rest_base}{path}, retrying token and rate-limit errors.

        `allow_codes` lists Marketo error codes to return rather than raise on
        (e.g. 702 "Folder not found" while probing for tokens).
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

            if allow_codes and codes & {str(c) for c in allow_codes}:
                return data

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
        """Yield records from an asset-API endpoint paging via offset/maxReturn."""
        offset = 0
        while True:
            data = self.get(path, dict(params, maxReturn=page_size, offset=offset))
            result = data.get("result") or []
            for record in result:
                yield record
            if len(result) < page_size:
                return
            offset += page_size

    def usage(self) -> list:
        """Today's API call usage per user — used to check quota headroom."""
        return self.get("/rest/v1/stats/usage.json").get("result") or []


def main() -> int:
    """Smoke test: prove the credentials work and report quota headroom."""
    client = ReadOnlyMarketoClient()
    client.auth.token()
    print(f"REST base : {client.rest_base}")
    for row in client.usage():
        print(f"Usage {row.get('date')}: {row.get('total'):,} calls today")
        for user in sorted(row.get("users", []), key=lambda u: -u["count"])[:8]:
            print(f"   {user['count']:>7,}  {user['userId']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
