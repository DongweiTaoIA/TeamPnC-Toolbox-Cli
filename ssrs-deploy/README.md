# ssrs-deploy

Deploy selected SSRS reports (`.rdl`) to **FNCT**, **ACCP**, or **PROD** from
`C:\repos\tfs\DP_Unifi_Resources`. Uploads only the reports you name — other
reports in the project are never touched.

## Install

```powershell
cd C:\repos\git\TeamPnC-ToolBox\cli\ssrs-deploy
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

Optional: copy `.env.example` to `.env` if `tf.exe` is not on your PATH.

## Usage

```powershell
# FNCT / ACCP — uses your current Windows login
python ssrs_deploy.py -FNCT -r DPR928E,DPR928F
python ssrs_deploy.py -ACCP -r dpr928e

# PROD — special user (password prompted if -p is omitted)
python ssrs_deploy.py -PROD -u iap.iafg.net\lidot -p <one-time> -r DPR928E,DPR928F
```

Report names are **case-insensitive**, `.rdl` is optional, whitespace tolerated.

## Options

| Flag              | Purpose                                                    |
|-------------------|------------------------------------------------------------|
| `-FNCT`/`-ACCP`/`-PROD` | Target environment (required, one only).            |
| `-r`, `--reports` | Comma-separated report list.                               |
| `-u`, `--user`    | PROD user; `domain/user` or `domain\user`.                 |
| `-p`, `--password`| PROD password. Omit to be prompted (never logged).         |
| `--dry-run`       | Show plan; no `tf get`, no upload.                         |
| `--no-pull`       | Skip `tf get` (source assumed current).                    |
| `--log-file PATH` | Also write log to a file.                                  |

## What it does

1. **Pull** — `tf.exe vc get <src> /recursive /noprompt` for the env's DPReports/Src folder.
2. **Parse** — reads `DPReports.rptproj` and picks the `Debug` PropertyGroup to get `TargetServerURL` + `TargetReportFolder` (the `Production` PropertyGroup in every branch is stale and is ignored).
3. **Resolve** — locates each requested `<name>.rdl` case-insensitively in the source folder. If any is missing, aborts before any upload.
4. **Confirm (PROD only)** — prints server, folder, and file list; requires `y`.
5. **Upload** — `ReportService2010.CreateCatalogItem(Overwrite=true)` for each `.rdl`. Only the reports you named are pushed.

## Env → source folder

| Env  | Source                                                                     |
|------|----------------------------------------------------------------------------|
| FNCT | `C:\repos\tfs\DP_Unifi_Resources\Dev\_FNCT\SSRS\DPReports\Src`              |
| ACCP | `C:\repos\tfs\DP_Unifi_Resources\Dev\_ACCP\SSRS\DPReports\Src`              |
| PROD | `C:\repos\tfs\DP_Unifi_Resources\Main\SSRS\DPReports\Src`                   |

Override in `.env` via `REPO_ROOT_DEV` / `REPO_ROOT_MAIN` if needed.

## Notes

- Subreports (`_sub.rdl`) are **not** auto-included. If your main `.rdl` calls a subreport that changed, list it too: `-r DPR201E,DPR201E_sub`.
- The tool warns (but continues) if the resolved `.rdl` is not listed in the `.rptproj` `<Report Include>` set.
- SSL warnings from the internal SSRS servers (self-signed certs) are suppressed.
