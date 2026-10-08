"""Type a message into Teams web. Only sends when --yes is given.

Reuses the Edge profile from scripts/snow_form.py, so the existing SSO session
carries over. Without --yes the text is typed into the compose box and left
there for you to review and send by hand.

Usage::

    # type into your self-chat, do not send
    .venv\\Scripts\\python.exe scripts/teams_post.py_pw --body "This is just a test"

    # type and send
    ... --body "..." --yes

If the deep link does not land on the right conversation, the script pauses and
lets you navigate there yourself before it types.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

TEAMS_URL = "https://teams.microsoft.com/v2/"
SELF_CHAT_URL = "https://teams.microsoft.com/v2/?ctx=chat#/conversations/48:notes?ctx=chat"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = PROJECT_ROOT / ".auth" / "edge"
ARTIFACT_DIR = PROJECT_ROOT / ".artifacts"

# Teams renames these between releases, so try the stable ones first.
COMPOSE_SELECTORS = [
    'div[data-tid="ckeditor"]',
    'div[contenteditable="true"][aria-label*="Type a message"]',
    'div[role="textbox"][contenteditable="true"]',
    'div[contenteditable="true"]',
]

SEND_SELECTORS = [
    'button[data-tid="sendMessageCommand"]',
    'button[name="send"]',
    'button[aria-label*="Send"]',
]


def wait_for_splash(page: Page, timeout_ms: int = 90_000) -> None:
    """Teams leaves #loading-screen on top of the app and it swallows clicks."""
    try:
        page.locator("#loading-screen").wait_for(state="hidden", timeout=timeout_ms)
    except PlaywrightTimeout:
        print("Loading splash is still up; continuing anyway.")


def find_compose(page: Page, timeout_ms: int) -> object:
    for selector in COMPOSE_SELECTORS:
        box = page.locator(selector).last
        try:
            box.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeout:
            continue
        print(f"Compose box matched: {selector}")
        return box
    raise LookupError("no compose box found; Teams may have changed its markup")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body", required=True)
    parser.add_argument("--url", default=SELF_CHAT_URL, help="conversation to open")
    parser.add_argument("--timeout", type=int, default=20_000)
    parser.add_argument("--yes", action="store_true", help="actually send the message")
    args = parser.parse_args()

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="msedge",
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else context.new_page()

        print(f"Opening {args.url}")
        page.goto(args.url, wait_until="domcontentloaded")
        wait_for_splash(page)

        try:
            box = find_compose(page, args.timeout)
        except LookupError:
            print("\nCould not find the compose box.")
            print("Navigate to the conversation you want in the Teams window,")
            input("then press Enter here... ")
            box = find_compose(page, args.timeout)

        box.focus()
        page.keyboard.type(args.body, delay=20)
        page.wait_for_timeout(1000)
        page.screenshot(path=str(ARTIFACT_DIR / "teams-typed.png"))
        print(f"\nTyped: {args.body}")

        if args.yes:
            for selector in SEND_SELECTORS:
                button = page.locator(selector).last
                if button.count():
                    button.click()
                    break
            else:
                page.keyboard.press("Enter")
            page.wait_for_timeout(2500)
            page.screenshot(path=str(ARTIFACT_DIR / "teams-sent.png"))
            print("Sent.")
        else:
            print("NOT sent. Review it in Teams and press Enter there yourself.")

        input("\nPress Enter here to close the browser... ")
        try:
            context.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
