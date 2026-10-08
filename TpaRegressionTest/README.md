# TpaRegressionTest

A simple Python command-line tool that runs a TPA regression test for a given ticket.
Before running the test, the tool:

1. verifies the ticket exists in Azure DevOps (`indall/Vancouver`),
2. checks the ticket carries the `Ready For FNCT` tag, and
3. opens the TPA web app in a real browser and clicks **Sign in** so you can
   complete the SSO flow.

## Requirements

- Python 3.10+
- An Azure DevOps Personal Access Token (PAT) with **Work Items: Read** scope.
- Playwright + Chromium (installed in *Setup* below).

## Setup

```powershell
python -m venv .venv
. .venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

## Configuration

Create a file containing **only** the PAT (no quotes, no `Bearer ` prefix, no
trailing newline matters — whitespace is stripped). The tool looks in this order:

1. The path in the `AZURE_DEVOPS_PAT_FILE` environment variable, if set.
2. `~/.azure_pat` (the user home directory).
3. `.azure_pat` next to the script or the built `TpaRegressionTest.exe`.

PowerShell example:

```powershell
Set-Content -Path "$HOME\.azure_pat" -Value "<your-PAT-here>" -NoNewline
```

> The repository's `.gitignore` excludes `.azure_pat`. **Never commit your PAT.**
> If a PAT is ever exposed (e.g. pasted into chat, a log, or a screenshot),
> revoke it immediately in Azure DevOps and generate a new one.

## Run from source

```powershell
python tpa_regression_test.py -t 1997687
```

Expected output for an existing ticket that is ready for FNCT:

```
Ticket 1997687 found: [Active] <ticket title>
Start testing ticket 1997687
Opening TPA app at https://iagadscd01-cac-app-unitpaprocessing-02.azurewebsites.net/ ...
Clicked Sign in. Press Enter after you finish signing in to close the browser...
```

A Chromium window opens automatically. Complete the Azure AD sign-in (and MFA
if required), then return to the terminal and press **Enter** to close the
browser and exit `0`.

### Exit codes

| Code | Meaning                                                |
| ---- | ------------------------------------------------------ |
| 0    | Ticket gates passed; TPA app opened and Sign in clicked. |
| 1    | Ticket not found in `indall/Vancouver`.                |
| 2    | Configuration error (missing PAT) or network/auth failure during the ticket check. |
| 3    | Ticket exists but is missing the required `Ready For FNCT` tag. |
| 4    | Browser automation failed (Playwright/Chromium missing, or Sign in button not found). |

## Build a standalone `TpaRegressionTest.exe`

Install [PyInstaller](https://pyinstaller.org/) and build a single-file executable:

```powershell
pip install pyinstaller
pyinstaller --onefile --name TpaRegressionTest tpa_regression_test.py
```

The executable will be created at `dist\TpaRegressionTest.exe`. Run it like so:

```powershell
.\dist\TpaRegressionTest.exe -t 123
```

> Note: PyInstaller bundles the Python code only. The host running
> `TpaRegressionTest.exe` must still have Playwright + Chromium installed
> (`pip install playwright && playwright install chromium`) for the sign-in
> step. Bundling Chromium into the exe is out of scope.
