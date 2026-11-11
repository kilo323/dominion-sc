#!/usr/bin/env python3
"""Probe Dominion Fusion daily usage without initializing the Bidgely client.

Reads credentials from DOMINIONSC_TEST_USERNAME / DOMINIONSC_TEST_PASSWORD
environment variables or the repository's .env file. The report contains
response shapes, date ranges, and row counts only; it does not print or save
account identifiers, credentials, or raw usage values.

Example:
    python diagnostics/probe_fusion_daily_usage.py
    python diagnostics/probe_fusion_daily_usage.py --start-date 2024-11-01 --end-date 2026-11-10
"""

from __future__ import annotations

import argparse
import getpass
import os
from datetime import date, timedelta
from pathlib import Path
import sys
from typing import Any

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


class FusionOnlyClient(DominionSCClient):
    """Keep normal Dominion authentication but skip the optional Bidgely bootstrap."""

    def _finalize_authenticated_session(self) -> bool:
        self._logged_in = True
        self._pending_2fa = False
        return True


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _credentials() -> tuple[str, str]:
    env_values = _read_dotenv(Path(".env"))
    username = os.environ.get("DOMINIONSC_TEST_USERNAME") or env_values.get(
        "DOMINIONSC_TEST_USERNAME"
    )
    password = os.environ.get("DOMINIONSC_TEST_PASSWORD") or env_values.get(
        "DOMINIONSC_TEST_PASSWORD"
    )
    if not username:
        username = input("Dominion username: ").strip()
    if not password:
        password = getpass.getpass("Dominion password: ")
    if not username or not password:
        raise ValueError("Both username and password are required.")
    return username, password


def _authenticate(client: FusionOnlyClient, username: str, password: str) -> None:
    try:
        client.login(username, password)
        return
    except Exception as err:
        detail = err.args[0] if err.args else None
        if not (isinstance(detail, dict) and detail.get("2fa_required")):
            raise

    options = client._2fa_options
    if not options:
        raise RuntimeError("Login requested 2FA but returned no verification methods.")

    print("2FA is required. Available delivery methods:")
    for index, option in enumerate(options, start=1):
        print(f"  {index}: {option.get('method', 'unknown')}")
    while True:
        try:
            choice = int(input("Choose a method number: "))
            if 1 <= choice <= len(options):
                break
        except ValueError:
            pass
        print("Enter one of the listed numbers.")

    client.select_2fa_method(options[choice - 1])
    code = getpass.getpass("Enter the verification code: ").strip()
    if not code:
        raise ValueError("A verification code is required.")
    client.verify_2fa_code(code, remember_device=False)


def _summarize_daily_response(
    payload: dict[str, Any], requested_start: date, requested_end: date
) -> dict[str, Any]:
    data = payload.get("raw_data") or {}
    records = payload.get("daily_records") or []
    summary: dict[str, Any] = {
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "response_start": data.get("startDate"),
        "response_end": data.get("endDate"),
        "record_count": len(records),
        "fuel_flags": {
            "electric_only": payload.get("is_electric_only"),
            "gas_only": payload.get("is_gas_only"),
        },
        "valid_read_day_counts": {
            "electric": data.get("numberOfValidElectricDailyReadDates"),
            "gas": data.get("numberOfValidGasDailyReadDates"),
            "total": data.get("numberOfValidTotalDailyReadDates"),
        },
        "series": {},
    }

    series_fields = {
        "electric_usage": "electric_usage",
        "electric_cost": "electric_cost",
        "gas_usage": "gas_usage",
        "gas_cost": "gas_cost",
    }
    for series_name, row_key in series_fields.items():
        values = [
            row.get(row_key)
            for row in records
            if isinstance(row, dict) and isinstance(row.get(row_key), (int, float))
        ]
        summary["series"][series_name] = {
            "value_count": len(values),
            "nonzero_count": sum(value != 0 for value in values),
        }

    return summary


def main() -> int:
    today = date.today()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=today - timedelta(days=730),
        help="Inclusive start date (YYYY-MM-DD); defaults to about two years ago.",
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=today - timedelta(days=1),
        help="Inclusive end date (YYYY-MM-DD); defaults to yesterday.",
    )
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")

    try:
        username, password = _credentials()
        client = FusionOnlyClient(verify_ssl=True)
        _authenticate(client, username, password)

        # Match the portal's account initialization sequence before probing usage.
        client.get_account_summary()
        result = client.get_current_daily_usage(
            usage_type="BOTH",
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except Exception as err:
        response = getattr(err, "response", None)
        status = getattr(response, "status_code", None)
        if status is not None:
            print(
                f"Fusion probe failed: HTTP {status} "
                f"({type(err).__name__}). No response body was printed."
            )
        else:
            print(
                f"Fusion probe failed: {type(err).__name__}: {err}",
                file=sys.stderr,
            )
        return 1

    print("Fusion GetAccountDailyUsageDetails probe (no raw usage values):")
    import json

    print(
        json.dumps(
            _summarize_daily_response(result, args.start_date, args.end_date),
            indent=2,
        )
    )
    print(
        "\nPlease share this summary only. Do not share .env, cookies, "
        "credentials, or an unredacted HAR."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
