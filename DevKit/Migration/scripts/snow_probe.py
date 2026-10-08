"""Probe a ServiceNow catalog item and optionally submit a single order.

Standard library only, so it runs before the uv project exists.

Default mode is read-only: it prints each variable's backend name, type,
mandatory flag, reference table and choice values. Those backend names are what
``order_now`` expects -- the form labels will not work.

Usage
-----
Discover the variables (read-only)::

    python scripts/snow_probe.py --user first.last@example.com

Parse a response you saved from the browser, no credentials needed::

    python scripts/snow_probe.py --from-file doc/snow-item.json

Submit exactly one request (prompts for confirmation)::

    python scripts/snow_probe.py --user first.last@example.com --order \
        --var service_type=information_request \
        --var "description=TEST via API - please ignore" \
        --var assignment_group=35427da71b2bc8508d340d43cd4bcbd2 \
        --var required_date=2026-10-06

The password is read from the ``SNOW_PASS`` environment variable if set,
otherwise prompted for without echo. It is never written to disk or logged.

The target host is not free-form: it is looked up in ``ALLOWED_INSTANCES``, so a
typo or a hostile argument cannot redirect credentials elsewhere.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ALLOWED_INSTANCES = {
    "iaaesf": "https://iaaesf.service-now.com",
}
DEFAULT_INSTANCE = "iaaesf"
DEFAULT_ITEM_SYS_ID = "53f7a515db98a0543694dd0968961972"  # Generic request to an IT team

SYS_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SnowError(RuntimeError):
    pass


def validate_sys_id(raw: str) -> str:
    if not SYS_ID_RE.match(raw):
        raise SystemExit("sys_id must be 32 lowercase hex characters")
    return raw


def validate_input_path(raw: str) -> Path:
    path = Path(raw).resolve()
    if not path.is_relative_to(PROJECT_ROOT):
        raise SystemExit(f"--from-file must point inside {PROJECT_ROOT}")
    return path


def _request(
    url: str, auth_header: str, *, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        method="POST" if payload is not None else "GET",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": auth_header,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - host is a constant from ALLOWED_INSTANCES
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        hints = {
            401: "Basic auth is likely disabled on this instance; you need an OAuth client.",
            403: "Authenticated, but the account lacks rights on this catalog item.",
        }
        hint = hints.get(exc.code, exc.reason)
        raise SnowError(f"ServiceNow returned {exc.code}. {hint}") from exc
    except urllib.error.URLError as exc:
        raise SnowError(f"Could not reach {url}: {exc.reason}") from exc


def _auth_header(user: str) -> str:
    password = os.environ.get("SNOW_PASS") or getpass.getpass(f"Password for {user}: ")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def show_variables(item: dict[str, Any]) -> None:
    print(f"\nCatalog item : {item.get('name')}")
    print(f"sys_id       : {item.get('sys_id')}\n")

    variables = [v for v in item.get("variables", []) if v.get("name")]
    if not variables:
        print("No variables returned. The response may be a different shape than expected.")
        return

    for var in sorted(variables, key=lambda v: v.get("order") or 0):
        print(f"  name      : {var['name']}")
        print(f"  label     : {var.get('label')}")
        print(f"  type      : {var.get('type')}")
        print(f"  mandatory : {bool(var.get('mandatory'))}")
        if var.get("reference"):
            print(f"  reference : {var['reference']}")
        for choice in var.get("choices") or []:
            print(f"  choice    : {choice.get('value')!r} = {choice.get('label')}")
        print()


def submit_order(
    base_url: str, item_sys_id: str, auth_header: str, variables: dict[str, str]
) -> None:
    payload = {"sysparm_quantity": "1", "variables": variables}

    print("\nPayload to be submitted:")
    print(json.dumps(payload, indent=2))
    print(f"\nTarget: {base_url} catalog item {item_sys_id}")

    if input("\nCreate ONE ServiceNow request? [y/N] ").strip().lower() != "y":
        print("Aborted; nothing was submitted.")
        return

    url = f"{base_url}/api/sn_sc/servicecatalog/items/{item_sys_id}/order_now"
    result = _request(url, auth_header, payload=payload)["result"]

    print(f"\nCreated {result.get('request_number')}")
    print("A 200 does not mean the variables were accepted -- open the request and check:")
    print(f"{base_url}/sp?id=ticket&table=sc_request&sys_id={result.get('sys_id')}")


def parse_vars(pairs: list[str]) -> dict[str, str]:
    variables: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            raise SystemExit(f"--var expects NAME=VALUE, got {pair!r}")
        variables[name] = value
    return variables


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--instance", default=DEFAULT_INSTANCE, choices=sorted(ALLOWED_INSTANCES)
    )
    parser.add_argument("--item", default=DEFAULT_ITEM_SYS_ID, help="catalog item sys_id")
    parser.add_argument("--user", help="ServiceNow username; omit when using --from-file")
    parser.add_argument("--from-file", help="parse a catalog item response saved from the browser")
    parser.add_argument("--order", action="store_true", help="submit one request")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    args = parser.parse_args()

    base_url = ALLOWED_INSTANCES[args.instance]
    item_sys_id = validate_sys_id(args.item)

    try:
        if args.from_file:
            if args.order:
                raise SystemExit("--from-file is read-only; drop --order")
            with validate_input_path(args.from_file).open(encoding="utf-8") as fh:
                show_variables(json.load(fh)["result"])
            return 0

        if not args.user:
            raise SystemExit("--user is required unless --from-file is given")

        auth_header = _auth_header(args.user)

        if args.order:
            variables = parse_vars(args.var)
            if not variables:
                raise SystemExit("--order requires at least one --var; run without --order first")
            submit_order(base_url, item_sys_id, auth_header, variables)
        else:
            url = f"{base_url}/api/sn_sc/servicecatalog/items/{item_sys_id}"
            show_variables(_request(url, auth_header)["result"])
    except SnowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
