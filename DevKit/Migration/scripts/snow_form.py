"""Open the ServiceNow catalog item in Edge and fill the form. Never submits.

There is deliberately no code path that clicks Request or Save as Draft. The
browser is left open at the end so the form can be reviewed and submitted by
hand.

The first run opens a blank Edge profile under ``.auth/edge`` and waits for you
to complete SSO. That profile is reused afterwards, so later runs land straight
on the form.

Usage::

    .venv\\Scripts\\python.exe scripts/snow_form.py --description "This is a test"

Every field is addressed by its visible label (see ``FIELD_LABELS``), so a
ServiceNow upgrade that renames internal variables will not break it. A label
change will, and that is a one-line edit here.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

INSTANCE = "https://iaaesf.service-now.com"
CATALOG_ITEM_SYS_ID = "53f7a515db98a0543694dd0968961972"  # Generic request to an IT team
ITEM_URL = f"{INSTANCE}/sp?id=sc_cat_item&sys_id={CATALOG_ITEM_SYS_ID}"

FIELD_LABELS = {
    "service_type": "Select required service type",
    "description": "Enter a requirement description",
    "team": "Select the team required to carry out this request",
    "required_date": "Select approximative required date",
}

DRAFT_BUTTON = "Save as Draft"
SUBMIT_BUTTON = "Request"  # never clicked; named so the intent is auditable

# Backend variable names, read off the rendered form (see .artifacts/form-fields.json).
FIELD_IDS = {
    "description": "#sp_formfield_description_of_work_to_be_done",
    "required_date": "#sp_formfield_requested_date",
}

# Select2 hides the real control, so these are driven through their visible proxy input.
SELECT2_ARIA = {
    "service_type": "Select required service type",
    "team": "Select the team required to carry out this request",
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = PROJECT_ROOT / ".auth" / "edge"
ARTIFACT_DIR = PROJECT_ROOT / ".artifacts"

DUMP_FIELDS_JS = """
() => [...document.querySelectorAll('input, textarea, select')]
  .filter(e => e.type !== 'hidden' && (e.id || e.name))
  .map(e => ({
    id: e.id,
    name: e.name,
    tag: e.tagName,
    type: e.type,
    ariaLabel: e.getAttribute('aria-label') || '',
    value: e.value,
    options: e.tagName === 'SELECT' ? [...e.options].map(o => ({ value: o.value, text: o.text })) : null,
  }))
"""


def wait_for_form(page: Page, timeout_ms: int) -> None:
    """Wait on the rendered label text; the inputs only exist once Angular has run."""
    label = FIELD_LABELS["description"]
    try:
        page.get_by_text(label, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeout:
        print("\nThe form is not visible yet -- finish signing in to ServiceNow in the")
        print("browser window, navigate to the catalog item, then come back here.")
        input("Press Enter once the form is on screen... ")
        page.get_by_text(label, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(1500)


def select2_pick(page: Page, aria_prefix: str, text: str, *, search: bool) -> bool:
    """Open a Select2 control and choose an option.

    The aria-labelled input is Select2's offscreen focus helper and cannot be
    clicked; the real target is the ``a.select2-choice`` anchor in its container.
    """
    focusser = page.locator(f'input[aria-label^="{aria_prefix}"]').first
    container = focusser.locator(
        'xpath=ancestor::*[contains(@class,"select2-container")][1]'
    )
    container.locator("a.select2-choice").first.click(timeout=15_000)

    drop = page.locator("#select2-drop")
    drop.wait_for(state="visible", timeout=10_000)

    if search:
        drop.locator("input.select2-input").first.fill(text)
        page.wait_for_timeout(2500)

    try:
        drop.locator(".select2-result-label").filter(has_text=text).first.click(timeout=8000)
    except Exception:
        page.keyboard.press("Escape")
        return False
    return True


def fill_form(page: Page, values: dict[str, str]) -> None:
    if service_type := values.get("service_type"):
        ok = select2_pick(page, SELECT2_ARIA["service_type"], service_type, search=False)
        print(f"  service type  -> {service_type}{'' if ok else '  [FAILED - set it yourself]'}")

    if description := values.get("description"):
        page.locator(FIELD_IDS["description"]).fill(description)
        print(f"  description   -> {description}")

    if team := values.get("team"):
        ok = select2_pick(page, SELECT2_ARIA["team"], team, search=True)
        print(f"  team          -> {team}{'' if ok else '  [FAILED - pick it yourself]'}")

    if required_date := values.get("required_date"):
        page.locator(FIELD_IDS["required_date"]).fill(required_date)
        page.keyboard.press("Escape")
        print(f"  required date -> {required_date}")


def save_as_draft(page: Page) -> None:
    button = page.get_by_role("button", name=DRAFT_BUTTON)
    if not button.count():
        button = page.get_by_text(DRAFT_BUTTON, exact=True)
    button.first.click(timeout=10_000)
    page.wait_for_timeout(3000)
    print(f"Clicked {DRAFT_BUTTON!r}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-type", default="System Management")
    parser.add_argument("--description", default="This is a test - do not action")
    parser.add_argument("--team", default="Ops. Centre")
    parser.add_argument("--date", dest="required_date", default="2026-10-06")
    parser.add_argument("--timeout", type=int, default=60_000, help="form wait in ms")
    parser.add_argument("--dump-only", action="store_true", help="inspect fields, fill nothing")
    parser.add_argument("--save-draft", action="store_true", help=f"click {DRAFT_BUTTON!r}")
    args = parser.parse_args()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="msedge",
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else context.new_page()

        print(f"Opening {ITEM_URL}")
        page.goto(ITEM_URL, wait_until="domcontentloaded")
        wait_for_form(page, args.timeout)

        fields: list[dict[str, Any]] = page.evaluate(DUMP_FIELDS_JS)
        fields_path = ARTIFACT_DIR / "form-fields.json"
        fields_path.write_text(json.dumps(fields, indent=2), encoding="utf-8")
        print(f"\nWrote {len(fields)} form controls to {fields_path}")

        if not args.dump_only:
            print("\nFilling:")
            fill_form(
                page,
                {
                    "service_type": args.service_type,
                    "description": args.description,
                    "team": args.team,
                    "required_date": args.required_date,
                },
            )
            shot = ARTIFACT_DIR / "form-filled.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"\nScreenshot: {shot}")

            if args.save_draft:
                save_as_draft(page)
                page.screenshot(path=str(ARTIFACT_DIR / "after-draft.png"), full_page=True)

        print(f"\n{SUBMIT_BUTTON!r} was NOT clicked. Review the form in the browser.")
        input("Press Enter here to close the browser... ")
        try:
            context.close()
        except Exception:
            pass  # the window may already have been closed by hand

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
