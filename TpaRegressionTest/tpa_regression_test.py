"""TpaRegressionTest CLI entry point."""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AZDO_ORG = "indall"
AZDO_PROJECT = "Vancouver"
AZDO_API_VERSION = "7.0"
PAT_FILENAME = ".azure_pat"
HTTP_TIMEOUT_SECONDS = 15
REQUIRED_TAG = "Ready For FNCT"
TPA_URL = "https://iagadscd01-cac-app-unitpaprocessing-02.azurewebsites.net/"
SIGN_IN_NAME_PATTERN = re.compile(r"^\s*sign\s*in\s*$", re.IGNORECASE)
BROWSER_TIMEOUT_MS = 30_000


def _script_dir() -> Path:
    """Directory of the running script or PyInstaller-frozen executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _pat_search_paths() -> list[Path]:
    paths: list[Path] = []
    env_path = os.environ.get("AZURE_DEVOPS_PAT_FILE")
    if env_path:
        paths.append(Path(env_path).expanduser())
    paths.append(Path.home() / PAT_FILENAME)
    paths.append(_script_dir() / PAT_FILENAME)
    return paths


def load_pat() -> str:
    """Read the Azure DevOps PAT from the first available local config file.

    Search order:
      1. File pointed to by ``AZURE_DEVOPS_PAT_FILE`` env var.
      2. ``~/.azure_pat``.
      3. ``.azure_pat`` next to the script / frozen executable.
    """
    tried: list[Path] = []
    for path in _pat_search_paths():
        tried.append(path)
        if path.is_file():
            token = path.read_text(encoding="utf-8").strip()
            if not token:
                raise RuntimeError(f"PAT file '{path}' is empty.")
            return token
    locations = "\n  ".join(str(p) for p in tried)
    raise RuntimeError(
        "Azure DevOps PAT not found. Create a file containing only the PAT at "
        "one of these locations:\n  " + locations
    )


def check_ticket(ticket_id: str, pat: str) -> dict | None:
    """Return the work-item JSON for ``ticket_id`` or ``None`` if it does not exist.

    Raises ``RuntimeError`` on auth/network failure. The PAT is never included in
    error messages.
    """
    url = (
        f"https://dev.azure.com/{AZDO_ORG}/{AZDO_PROJECT}"
        f"/_apis/wit/workitems/{urllib.parse.quote(ticket_id, safe='')}"
        f"?api-version={AZDO_API_VERSION}"
    )
    auth = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(
            f"Azure DevOps request failed: HTTP {exc.code} {exc.reason} for {url}"
        ) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach Azure DevOps ({exc.reason}) for {url}"
        ) from None


def _normalize_tag(tag: str) -> str:
    """Collapse internal whitespace and lower-case for tolerant tag comparison."""
    return " ".join(tag.split()).lower()


def parse_tags(raw: str | None) -> list[str]:
    """Split Azure DevOps ``System.Tags`` into a clean list, preserving order."""
    if not raw:
        return []
    return [piece.strip() for piece in raw.split(";") if piece.strip()]


def has_required_tag(tags: list[str], required: str = REQUIRED_TAG) -> bool:
    """Case- and whitespace-insensitive membership check."""
    target = _normalize_tag(required)
    return any(_normalize_tag(tag) == target for tag in tags)


def open_tpa_and_click_sign_in(url: str = TPA_URL) -> None:
    """Open ``url`` in a headed Chromium and click the **Sign in** button.

    Blocks on stdin until the user presses Enter, then closes the browser.
    Raises ``RuntimeError`` if Playwright/Chromium is missing or the button
    cannot be located within ``BROWSER_TIMEOUT_MS``.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeoutError
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -r requirements.txt "
            "&& playwright install chromium"
        ) from exc

    print(f"Opening TPA app at {url} ...")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            try:
                context = browser.new_context()
                page = context.new_page()
                page.set_default_timeout(BROWSER_TIMEOUT_MS)
                page.goto(url, wait_until="domcontentloaded")

                # Prefer the accessibility-role match; fall back to link/text.
                candidates = [
                    page.get_by_role("button", name=SIGN_IN_NAME_PATTERN),
                    page.get_by_role("link", name=SIGN_IN_NAME_PATTERN),
                    page.get_by_text(SIGN_IN_NAME_PATTERN),
                ]
                clicked = False
                for locator in candidates:
                    try:
                        locator.first.wait_for(state="visible", timeout=5_000)
                        locator.first.click()
                        clicked = True
                        break
                    except PwTimeoutError:
                        continue
                if not clicked:
                    raise RuntimeError(
                        f"Sign in button not found within {BROWSER_TIMEOUT_MS // 1000}s."
                    )

                try:
                    input(
                        "Clicked Sign in. Press Enter after you finish signing "
                        "in to close the browser..."
                    )
                except EOFError:
                    # Non-interactive stdin (CI / piped) — just close immediately.
                    pass
            finally:
                browser.close()
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a sanitized message
        raise RuntimeError(f"Browser automation failed: {exc}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="TpaRegressionTest",
        description="Run TPA regression tests for a given ticket.",
    )
    parser.add_argument(
        "-t",
        "--ticket",
        required=True,
        help="Ticket number to test.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        pat = load_pat()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        ticket = check_ticket(args.ticket, pat)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if ticket is None:
        print(
            f"Ticket {args.ticket} not found in {AZDO_ORG}/{AZDO_PROJECT}.",
            file=sys.stderr,
        )
        return 1

    fields = ticket.get("fields", {}) if isinstance(ticket, dict) else {}
    title = fields.get("System.Title", "<no title>")
    state = fields.get("System.State", "<no state>")
    print(f"Ticket {ticket.get('id', args.ticket)} found: [{state}] {title}")

    tags = parse_tags(fields.get("System.Tags"))
    if not has_required_tag(tags):
        current = ", ".join(tags) if tags else "(none)"
        print(
            f"Ticket {args.ticket} is missing required tag '{REQUIRED_TAG}'. "
            f"Current tags: {current}",
            file=sys.stderr,
        )
        return 3

    print(f"Start testing ticket {args.ticket}")

    try:
        open_tpa_and_click_sign_in()
    except RuntimeError as exc:
        print(f"error: Could not open TPA app: {exc}", file=sys.stderr)
        return 4

    return 0


if __name__ == "__main__":
    sys.exit(main())
