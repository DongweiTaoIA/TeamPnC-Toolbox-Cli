# BankInfoSync

CLI that copies bank-info records from a source SQL Server to `pvt.dmProducerBankInfo`
on a target SQL Server. Supports ACCP (testing) and PROD (production) environments.

## Behavior

For each row returned by `source_query.sql`:

1. Verify the parent producer exists in `pvt.dmProducer` (matched by `ProducerCode`).
   → status `PARENT_NOT_FOUND` if missing.
2. Look up existing bank info in `pvt.dmProducerBankInfo` by `ProductClientKey`.
   * If found → compare source vs. target columns → status `EXISTS_MATCH` or
     `EXISTS_DIFF` (differences recorded in the CSV; **no update performed**).
   * If not found → `INSERT` new row (or `INSERT_DRY_RUN` when `--dry-run`).
3. Any unexpected error on a single row → rolled back, logged, status `ERROR`.
   Processing continues with the next row.

Each row is inserted in its own transaction so one failure never aborts the batch.

## Setup

1. Install the ODBC driver for SQL Server (17 or 18) on the machine.
2. Create a virtual env and install deps:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in server/database names for both envs.

## Usage

### Test connections first (recommended before any run)

ACCP (runs as your current Windows user):
```powershell
python sync.py --env accp --test-connection
```

PROD (runs as `iap.iafg.net\LIDOT`, prompted for password by Windows):
```powershell
runas /netonly /user:iap.iafg.net\LIDOT "python sync.py --env prod --test-connection"
```

Both should print the effective `SUSER_SNAME()`, server, and database for source
and target. Do not proceed unless the effective login is what you expect.

### Dry-run (no writes)

```powershell
python sync.py --env accp --dry-run
```

Produces a CSV report showing exactly what would be inserted / skipped / diffed.

### Real run

ACCP:
```powershell
python sync.py --env accp
```

PROD:
```powershell
runas /netonly /user:iap.iafg.net\LIDOT "python sync.py --env prod"
```

## Output

* `logs/sync-<env>-<timestamp>.log` — detailed log (DEBUG).
* `reports/bankinfo-sync-<env>-<timestamp>.csv` — one row per source record:
  `producer_code, product_client_key, status, differences, error_message`.

## Notes

* Credentials: **Windows Integrated only**. No passwords are stored anywhere.
* `runas /netonly` is the recommended pattern for PROD — Windows prompts for the
  password, the process runs under that identity on network calls, and the
  script itself never sees the password.
