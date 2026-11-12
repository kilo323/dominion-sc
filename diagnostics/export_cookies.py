#!/usr/bin/env python3
"""Export a fully-authenticated Dominion session to a JSON cookie file.

Run this ONCE in an environment where you can answer the 2FA prompt yourself.
It authenticates, drives the 2FA verification interactively, then saves the
authenticated session so later runs can reuse it without 2FA.

The resulting file contains authentication cookies and (optionally) the
password, so treat it as a credential: it is already gitignored as
``dominion_cookies.json`` — do not commit it or share its contents.

Example:
    .env:
        DOMINIONSC_TEST_USERNAME=your_username_here
        DOMINIONSC_TEST_PASSWORD=your_password_here
    python diagnostics/export_cookies.py
    python diagnostics/export_cookies.py /path/to/cookies.json
"""
from __future__ import annotations

import getpass
import os
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


def _authenticate(client: DominionSCClient, username: str, password: str) -> None:
    """Log in, walking the user through 2FA when the account requires it."""
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


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "dominion_cookies.json"
    username, password = _credentials()

    client = DominionSCClient(verify_ssl=False, log_requests=False)
    _authenticate(client, username, password)

    if not client._logged_in:
        print("Authentication did not complete.")
        return 1

    Path(out_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    client.save_cookies(out_path)
    print(f"Saved authenticated session to {Path(out_path).resolve()}")
    print("You can now load it in a later run with diagnostics/load_from_cookies.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
