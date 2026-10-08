# Requirement: Check Azure DevOps Ticket Before Regression Test

## Goal

Before starting a regression test, the CLI must verify that the supplied ticket:

1. exists as a work item in Azure DevOps (`indall/Vancouver`),
2. is gated as ready for functional testing by carrying the tag
   **`Ready For FNCT`**, **and**
3. once both checks pass, open the TPA web app in a real browser and click the
   **Sign in** button so the engineer can complete authentication and proceed
   with the test.

Any failure of the first two checks must stop the run immediately with a clear,
actionable error. Browser launch failure must not silently fall through.

## User story

> As a TPA engineer, when I run `TpaRegressionTest -t <ticket>`, I want the tool
> to confirm the ticket exists in `indall/Vancouver` **and** carries the
> `Ready For FNCT` tag, then automatically open the TPA app and click
> **Sign in** for me, so I can complete the SSO flow and start the regression
> test without copy-pasting URLs or hunting for the button.

## Scope

- Organization: `indall` (hard-coded)
- Project: `Vancouver` (hard-coded)
- Endpoint: `GET https://dev.azure.com/{org}/{project}/_apis/wit/workitems/{id}?api-version=7.0`
- Auth: HTTP Basic with empty username and PAT as password
  (`Authorization: Basic base64(":" + PAT)`)
- Tag source: `fields["System.Tags"]` from the same work-item response
  (semicolon-separated string, may be missing/empty).
- Required tag: `Ready For FNCT` (hard-coded constant).
- TPA app URL:
  `https://iagadscd01-cac-app-unitpaprocessing-02.azurewebsites.net/`
  (hard-coded constant).
- Browser automation: Playwright (Chromium), headed mode. The engineer
  completes Azure AD / Entra ID SSO (including MFA if enforced) manually in the
  opened browser window.

## Functional requirements

1. The CLI continues to accept the ticket number via `-t` / `--ticket`. No new
   required arguments.
2. The PAT is loaded from a local file. Search order:
   1. Path in the environment variable `AZURE_DEVOPS_PAT_FILE`, if set.
   2. `~/.azure_pat`.
   3. `.azure_pat` next to the script or the built `TpaRegressionTest.exe`.
3. Whitespace around the token is stripped. An empty file is treated as missing.
4. The tool calls the Azure DevOps REST API with a 15-second timeout.
5. On HTTP 200, the tool prints a one-line summary using `id`,
   `fields.System.Title`, and `fields.System.State`.
6. The tool then evaluates the work item's tags:
   1. Read `fields["System.Tags"]` from the response. If absent, treat as the
      empty string.
   2. Split on `;`, strip whitespace from each entry, drop empties.
   3. The required tag `Ready For FNCT` must be present using a
      **case-insensitive, whitespace-insensitive** match (so `ready for fnct`
      and `Ready  For  FNCT` both qualify).
7. If the required tag is present, the tool prints the existing
   `Start testing ticket {id}` line.
8. If the required tag is **absent**, the tool prints
   `Ticket {id} is missing required tag 'Ready For FNCT'. Current tags: <list or '(none)'>`
   to stderr and exits 3. The regression test is **not** started and the
   browser is **not** opened.
9. After the success line in step 7, the tool launches a Chromium browser via
   Playwright in **headed** mode, navigates to the TPA URL, and clicks the
   **Sign in** button.
   1. Button selector: accessibility role `button` with name matching
      `^\s*sign\s*in\s*$` (case-insensitive). If no `button` is found, fall
      back to the same name match on role `link` and then any visible element.
   2. Wait up to 30 s for the page to load and the button to appear.
   3. After the click, leave the browser window open and block the CLI on a
      stdin prompt: `Press Enter after you finish signing in to close the
      browser...`. When the user presses Enter, the browser closes and the tool
      exits 0.
10. If the browser launch, navigation, or click fails (e.g. Playwright or
    Chromium not installed, network error, button not found within timeout),
    the tool prints a clear error to stderr and exits 4. The earlier success
    output (steps 5–7) is **not** rolled back.
11. On HTTP 404, the tool prints `Ticket {id} not found in indall/Vancouver.` to
    stderr and exits 1.
12. On any other HTTP error, timeout, or network failure during the Azure
    DevOps call, the tool prints a sanitized error to stderr and exits 2. The
    PAT must never appear in any message, log, or exception.
13. If no PAT file can be located or it is empty, the tool prints an error
    listing the searched paths to stderr and exits 2.

## Non-functional requirements

- The ticket-check path (steps 1–8) uses only the Python standard library.
- The browser step (step 9) depends on **Playwright** plus the Chromium browser
  it provisions. This is a documented runtime dependency installed via
  `pip install -r requirements.txt` followed by `playwright install chromium`.
  It is **not** bundled into a PyInstaller one-file build by default; using the
  built `TpaRegressionTest.exe` requires the same Playwright + Chromium
  install on the host.
- Compatible with Python 3.10+.
- No retries, caching, or background work.

## Security requirements

- The PAT is never committed. `.azure_pat` must be listed in `.gitignore`.
- The PAT is never echoed to stdout, stderr, logs, or exception messages.
- Only the request URL and HTTP status are surfaced on failure.
- If a PAT is ever exposed (chat, log, screenshot, screen share), it must be
  revoked in Azure DevOps and regenerated.
- The tool does **not** collect, store, or transmit the engineer's TPA / Azure
  AD credentials. Sign-in happens entirely inside the user-controlled browser
  window opened by Playwright.

## Exit codes

| Code | Meaning                                                          |
| ---- | ---------------------------------------------------------------- |
| 0    | Ticket gates passed; TPA app opened, Sign in clicked, browser closed normally. |
| 1    | Ticket not found in `indall/Vancouver`.                          |
| 2    | Configuration error (missing/empty PAT) or network/auth failure during the ticket check. |
| 3    | Ticket exists but is missing the required `Ready For FNCT` tag.  |
| 4    | Browser automation failed (Playwright/Chromium missing, navigation error, Sign in button not found). |

## Output examples

Success:

```
Ticket 1997687 found: [Active] (SU) TPA Regression Test App - Vibe Coding
Start testing ticket 1997687
Opening TPA app at https://iagadscd01-cac-app-unitpaprocessing-02.azurewebsites.net/ ...
Clicked Sign in. Press Enter after you finish signing in to close the browser...
```

Not found (stderr):

```
Ticket 999999999 not found in indall/Vancouver.
```

Missing required tag (stderr):

```
Ticket 1997687 is missing required tag 'Ready For FNCT'. Current tags: In Progress, Needs Review
```

Missing required tag, no tags at all (stderr):

```
Ticket 1997687 is missing required tag 'Ready For FNCT'. Current tags: (none)
```

Browser automation failure (stderr):

```
error: Could not open TPA app: Sign in button not found within 30s.
```

Missing PAT (stderr):

```
error: Azure DevOps PAT not found. Create a file containing only the PAT at one of these locations:
  C:\Users\<user>\.azure_pat
  C:\repos\...\TpaRegressionTest\.azure_pat
```

## Acceptance criteria

- [x] Existing ticket (e.g. `1997687`) prints summary line.
- [x] Ticket carrying `Ready For FNCT` prints `Start testing ticket {id}` and
      proceeds to the browser step.
- [ ] Browser step opens Chromium, navigates to the TPA URL, clicks **Sign in**,
      and blocks on a stdin prompt until the user presses Enter, then exits 0.
- [ ] Browser launch failure (Playwright missing or button not found) exits 4
      with a clear error.
- [ ] Ticket without `Ready For FNCT` prints the missing-tag error on stderr
      (showing the current tag list or `(none)`) and exits 3 **without**
      opening a browser.
- [ ] Tag match is case- and whitespace-insensitive
      (e.g. `ready for fnct`, `Ready  For  FNCT` are accepted).
- [x] Non-existent ticket (e.g. `999999999`) prints "not found" on stderr and
      exits 1.
- [x] Missing PAT file prints clear error listing search paths and exits 2.
- [ ] Invalid/expired PAT prints sanitized auth error and exits 2 without
      echoing the token.
- [x] `.azure_pat` is listed in `.gitignore`.
- [ ] Runtime Python dependencies are declared in `requirements.txt`
      (currently: `playwright`).

## Out of scope

- Validating ticket fields beyond existence and the `Ready For FNCT` tag.
- Supporting other Azure DevOps organizations, projects, configurable tag
  names, or configurable TPA URLs.
- Implementing the actual regression-test logic (still a placeholder print).
- Retry logic, response caching, or rate-limit handling.
- Adding, removing, or otherwise mutating tags on the work item.
- Automating the Azure AD / Entra ID sign-in itself (no credential capture,
  no MFA bypass). The user signs in manually in the opened browser window.
- Bundling Chromium into a PyInstaller one-file build.
