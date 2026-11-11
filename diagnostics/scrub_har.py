#!/usr/bin/env python3
"""Scrub sensitive values from a HAR file before sharing it for debugging.

Usage:
    python diagnostics/scrub_har.py mycapture.har
    python diagnostics/scrub_har.py mycapture.har -o cleaned.har

Writes `<input>.scrubbed.har` (or the path given with -o) and prints a
summary of what was scrubbed. Only uses the Python standard library.

Scrubbed:
  - Cookie / Set-Cookie / Authorization / CSRF header values
  - __RequestVerificationToken query parameters
  - Passwords, usernames, PINs, 2FA tokens in request bodies
  - encryptedData / session payloads
  - Access tokens and account PII in response bodies (phone, email, name, address, ...)
  - The `payload` value in JSON responses (keys/shape kept, data removed)
  - Large or HTML response bodies (replaced with a size marker)

Preserved:
  - Request URLs and non-sensitive query params (service=, pilotId=, ...)
  - Status codes, content types, timing info
  - Non-sensitive JSON response values (error IDs, client_id, ...)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

HEADER_SCRUB = {
    "cookie",
    "set-cookie",
    "authorization",
    "proxy-authorization",
    "x-csrf-token",
    "x-xsrf-token",
}

SENSITIVE_KEYS = {
    "password",
    "username",
    "pin",
    "otp",
    "token",
    "2fatoken",
    "tfa_token",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authtoken",
    "sessiontoken",
    "encrypteddata",
    "verificationtoken",
    "__requestverificationtoken",
    "phonenumber",
    "phone",
    "mobilephone",
    "email",
    "emailaddress",
    "accountnumber",
    "serviceaddressandaccountno",
    "serviceaddress",
    "accounttown",
    "customername",
}

CSRF_RE = re.compile(r'(__RequestVerificationToken["\']?\s*[:=]\s*["\'])([^"\']+)')
MAX_TEXT_RESPONSE = 5000  # non-JSON response bodies longer than this are dropped


def payload_marker(value):
    """Replace a response `payload` value with a shape-preserving marker."""
    if isinstance(value, dict):
        keys = ", ".join(sorted(str(k) for k in value.keys()))
        return {"[scrubbed]": f"payload omitted; top-level keys: {keys}"}
    if isinstance(value, list):
        return {"[scrubbed]": f"payload omitted; list of {len(value)} items"}
    if isinstance(value, str):
        return f"[scrubbed payload string, length={len(value)}]"
    return "[scrubbed]"


def scrub_json(node, scrub_payload: bool):
    """Recursively scrub sensitive keys. Returns (scrubbed_node, count)."""
    if isinstance(node, dict):
        out = {}
        count = 0
        for key, value in node.items():
            lk = str(key).lower()
            if scrub_payload and lk == "payload" and value is not None:
                out[key] = payload_marker(value)
                count += 1
            elif lk in SENSITIVE_KEYS:
                out[key] = "[SCRUBBED]"
                count += 1
            else:
                scrubbed, n = scrub_json(value, scrub_payload)
                out[key] = scrubbed
                count += n
        return out, count
    if isinstance(node, list):
        out = []
        count = 0
        for item in node:
            scrubbed, n = scrub_json(item, scrub_payload)
            out.append(scrubbed)
            count += n
        return out, count
    return node, 0


def scrub_url(url: str):
    """Replace __RequestVerificationToken query values. Returns (url, count)."""
    parts = urlparse(url)
    if not parts.query:
        return url, 0
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    new_pairs = []
    count = 0
    for key, value in pairs:
        if key.lower() == "__requestverificationtoken" and value:
            new_pairs.append((key, "[SCRUBBED]"))
            count += 1
        else:
            new_pairs.append((key, value))
    if count:
        return urlunparse(parts._replace(query=urlencode(new_pairs))), count
    return url, 0


def scrub_entry(entry: dict, stats: dict) -> None:
    request = entry.get("request", {})
    response = entry.get("response", {})

    for side, section in (("request", request), ("response", response)):
        for header in section.get("headers", []) or []:
            name = header.get("name", "").lower()
            # Fixed set plus any header whose name mentions token/authorization
            # (covers __RequestVerificationToken, X-CSRF-Token, custom auth, ...).
            if (name in HEADER_SCRUB or "token" in name or "authorization" in name) and header.get(
                "value"
            ):
                header["value"] = "[SCRUBBED]"
                stats["headers"] += 1
        for cookie in section.get("cookies", []) or []:
            if cookie.get("value"):
                cookie["value"] = "[SCRUBBED]"
                stats["cookies"] += 1

    scrubbed_url, n = scrub_url(request.get("url", ""))
    if n:
        request["url"] = scrubbed_url
        stats["query_params"] += n
    for param in request.get("queryString", []) or []:
        if param.get("name", "").lower() == "__requestverificationtoken" and param.get("value"):
            param["value"] = "[SCRUBBED]"
            stats["query_params"] += 1

    post_data = request.get("postData")
    if isinstance(post_data, dict) and post_data.get("text"):
        try:
            body = json.loads(post_data["text"])
            scrubbed, n = scrub_json(body, scrub_payload=False)
            post_data["text"] = json.dumps(scrubbed)
            stats["body_values"] += n
        except ValueError:
            stats["nonjson_bodies"] += 1

    content = response.get("content")
    if isinstance(content, dict) and content.get("text"):
        text = content["text"]
        mime = (content.get("mimeType") or "").lower()
        if "json" in mime:
            try:
                body = json.loads(text)
                scrubbed, n = scrub_json(body, scrub_payload=True)
                content["text"] = json.dumps(scrubbed)
                stats["response_values"] += n
            except ValueError:
                content["text"] = "[unparseable JSON response body omitted]"
                stats["response_bodies_omitted"] += 1
        elif len(text) > MAX_TEXT_RESPONSE:
            content["text"] = (
                f"[response body omitted; mimeType={mime or 'unknown'}, "
                f"size={len(text)}]"
            )
            stats["response_bodies_omitted"] += 1
        else:
            scrubbed_text, n = CSRF_RE.subn(lambda m: m.group(1) + "[SCRUBBED]", text)
            if n:
                content["text"] = scrubbed_text
                stats["response_values"] += n


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("har_file", help="Path to the HAR file to scrub")
    parser.add_argument(
        "-o",
        "--output",
        help="Output path (default: <input>.scrubbed.har)",
    )
    args = parser.parse_args()

    try:
        with open(args.har_file, "r", encoding="utf-8", errors="replace") as fh:
            har = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read HAR file: {exc}", file=sys.stderr)
        return 1

    stats = {
        "headers": 0,
        "cookies": 0,
        "query_params": 0,
        "body_values": 0,
        "response_values": 0,
        "response_bodies_omitted": 0,
        "nonjson_bodies": 0,
    }
    entries = (har.get("log") or {}).get("entries", []) or []
    for entry in entries:
        scrub_entry(entry, stats)

    output = args.output or args.har_file.replace(".har", ".scrubbed.har")
    if output == args.har_file:
        output = args.har_file + ".scrubbed.har"
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(har, fh, indent=1)

    total = (
        stats["headers"]
        + stats["cookies"]
        + stats["query_params"]
        + stats["body_values"]
        + stats["response_values"]
        + stats["response_bodies_omitted"]
    )
    print(f"Scrubbed {total} sensitive value(s) from {len(entries)} request(s).")
    print(f"  headers (cookies/authorization/csrf): {stats['headers']}")
    print(f"  cookie entries: {stats['cookies']}")
    print(f"  query parameters: {stats['query_params']}")
    print(f"  request body values: {stats['body_values']}")
    print(f"  response body values: {stats['response_values']}")
    print(f"  response bodies omitted: {stats['response_bodies_omitted']}")
    if stats["nonjson_bodies"]:
        print(
            f"  note: {stats['nonjson_bodies']} non-JSON request body(s) were kept "
            "as-is; review them manually if in doubt."
        )
    print(f"Wrote: {output}")
    print("Share only the .scrubbed.har file - not the original.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
