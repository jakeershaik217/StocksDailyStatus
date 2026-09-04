"""Exchange a Kite Connect request_token for a daily access_token.

1. Open https://kite.zerodha.com/connect/login?v=3&api_key=<KITE_API_KEY> and log in.
2. Copy the ``request_token`` query param from the redirect URL.
3. KITE_API_KEY=... KITE_API_SECRET=... KITE_REQUEST_TOKEN=... python scripts/kite_login.py

Prints the access_token; store it as the KITE_ACCESS_TOKEN secret. Tokens expire every day.
"""

from __future__ import annotations

import hashlib
import os
import sys

import requests


def main() -> int:
    api_key = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]
    request_token = os.environ["KITE_REQUEST_TOKEN"]
    checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    resp = requests.post(
        "https://api.kite.trade/session/token",
        headers={"X-Kite-Version": "3"},
        data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
        timeout=15,
    )
    payload = resp.json()
    if resp.status_code >= 400 or payload.get("status") != "success":
        print(f"Kite login failed: {payload.get('message', payload)}", file=sys.stderr)
        return 1
    print(payload["data"]["access_token"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
