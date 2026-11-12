#!/usr/bin/env python3
"""Load a previously exported session (no 2FA) and print the raw Fusion payload.

This is the companion to ``export_cookies.py``. It restores an authenticated
session from a cookie file and runs ``get_all_usage_data()`` so a running agent
can inspect the actual field names in ``allUsageHistoryData``. It intentionally
prints the raw payload (including field keys and the first row) so the parser can
be validated against a real account — run it in a trusted location only.

Example:
    python diagnostics/load_from_cookies.py /path/to/cookies.json
    python diagnostics/load_from_cookies.py      # uses dominion_cookies.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:
    from custom_components.dominionsc.dominion_sc_client import DominionSCClient
except ModuleNotFoundError as err:
    if err.name == "bs4":
        print(
            "Missing dependency: beautifulsoup4. From the repository root, run:\n"
            f'  "{sys.executable}" -m pip install -r requirements-dev.txt',
            file=sys.stderr,
        )
        raise SystemExit(1) from err
    raise

DEFAULT_COOKIE_PATH = "dominion_cookies.json"


def main() -> int:
    cookie_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_COOKIE_PATH
    cookie_path = Path(cookie_path).expanduser()
    if not cookie_path.exists():
        print(
            f"Cookie file not found: {cookie_path.resolve()}",
            file=sys.stderr,
        )
        return 1

    client = DominionSCClient(verify_ssl=False, log_requests=False)
    if not client.load_cookies(cookie_path):
        print(
            f"load_cookies failed for {cookie_path.resolve()}; "
            "the session likely expired. Re-run diagnostics/export_cookies.py.",
            file=sys.stderr,
        )
        return 1

    print("Authenticated via cookies. service_types:", client._service_types)

    accounts = client.get_account_listing()
    if accounts:
        account_number = accounts[0].get("account_number")
    else:
        account_number = client.get_account_summary().get("account_number")
    print("account_number:", account_number)

    result = client.get_all_usage_data(account_number)
    raw = result.get("all_usage_history_data")
    if not raw or not isinstance(raw, list) or not raw:
        print("allUsageHistoryData is empty; no fields to inspect.")
        return 0

    first = raw[0]
    for key, value in first.items():
        if isinstance(value, list):
            if value:
                print(f"  {key}: list[{len(value)}]; sample item keys:", list(value[0].keys()))
            else:
                print(f"  {key}: empty list")

    print("\n--- FULL FIRST ITEM (raw) ---")
    print(json.dumps(first, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
