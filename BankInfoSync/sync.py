"""BankInfoSync — copy bank-info records from a source SQL Server to
`pvt.dmProducerBankInfo` on a target SQL Server.

Auth is 100% Windows Integrated (`Trusted_Connection=yes`). For PROD, launch via
`runas /netonly /user:iap.iafg.net\\LIDOT "python sync.py --env prod ..."` so the
process runs under the correct AD identity; the script itself handles no passwords.

Run `python sync.py --help` for options.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pyodbc
from dotenv import load_dotenv


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"
REPORT_DIR = SCRIPT_DIR / "reports"

# Source query lives next to the script and is treated as trusted config,
# authored by the developer running the tool. The path is intentionally NOT
# exposed as a CLI argument to keep it out of the tainted input surface.
SOURCE_QUERY_PATH = SCRIPT_DIR / "source_query.sql"

TARGET_SCHEMA = "pvt"
TARGET_TABLE = "dmProducerBankInfo"
PARENT_TABLE = "dmProducer"

# Strict identifier pattern for SQL Server object/column names built from source
# query metadata. Prevents injection when interpolating column names.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

# CSV formula-injection prefixes (OWASP): a leading character in this set
# causes Excel / LibreOffice to evaluate the cell as a formula.
_CSV_UNSAFE_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# Columns produced by the source query that are NOT inserted into the target
# table (they are used for lookups / logging only).
SOURCE_ONLY_COLUMNS = {"ProducerCode"}

# The parent-existence check assumes pvt.dmProducer has a `ProducerCode` column.
PARENT_LOOKUP_COLUMN = "ProducerCode"

# Column in pvt.dmProducerBankInfo used to detect an existing row.
TARGET_KEY_COLUMN = "ProductClientKey"

# --- Generate-SQL mode ------------------------------------------------------
# Source column carrying the dealer id used to look up a producer.
DEALER_ID_COLUMN = "DealerId"
# Column in pvt.dmProducer that matches the source DealerId.
PRODUCER_DEALER_ID_COLUMN = "ProducerId"
# Column in pvt.dmProducer whose value we feed into TARGET_KEY_COLUMN.
PRODUCER_CLIENT_KEY_COLUMN = "ClientKey"

ENV_CHOICES = ("accp", "prod")

# Row-level status codes emitted to the CSV report.
STATUS_INSERTED = "INSERTED"
STATUS_INSERT_DRY_RUN = "INSERT_DRY_RUN"
STATUS_EXISTS_MATCH = "EXISTS_MATCH"
STATUS_EXISTS_DIFF = "EXISTS_DIFF"
STATUS_PARENT_NOT_FOUND = "PARENT_NOT_FOUND"
STATUS_ERROR = "ERROR"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EnvConfig:
    env: str
    driver: str
    source_server: str
    source_db: str
    target_server: str
    target_db: str
    trust_server_cert: bool


def load_config(env: str) -> EnvConfig:
    load_dotenv(SCRIPT_DIR / ".env")
    prefix = env.upper()
    required = {
        "source_server": f"{prefix}_SOURCE_SERVER",
        "source_db":     f"{prefix}_SOURCE_DB",
        "target_server": f"{prefix}_TARGET_SERVER",
        "target_db":     f"{prefix}_TARGET_DB",
    }
    values: dict[str, str] = {}
    missing: list[str] = []
    for field, var in required.items():
        val = os.getenv(var, "").strip()
        if not val:
            missing.append(var)
        values[field] = val
    if missing:
        raise SystemExit(
            f"Missing required environment variables in .env: {', '.join(missing)}"
        )

    driver = os.getenv("ODBC_DRIVER", "ODBC Driver 17 for SQL Server").strip()
    trust = os.getenv(f"{prefix}_TRUST_SERVER_CERT", "no").strip().lower() in {
        "1", "true", "yes", "y",
    }

    return EnvConfig(
        env=env,
        driver=driver,
        trust_server_cert=trust,
        **values,
    )


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def setup_logging(env: str, run_id: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"sync-{env}-{run_id}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Wipe any existing handlers so re-runs inside the same interpreter don't stack.
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    return log_path


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #

def _conn_str(cfg: EnvConfig, server: str, database: str) -> str:
    parts = [
        f"DRIVER={{{cfg.driver}}}",
        f"SERVER={server}",
        f"DATABASE={database}",
        "Trusted_Connection=yes",
        "Encrypt=yes",
    ]
    if cfg.trust_server_cert:
        parts.append("TrustServerCertificate=yes")
    return ";".join(parts) + ";"


def open_source(cfg: EnvConfig) -> pyodbc.Connection:
    conn = pyodbc.connect(_conn_str(cfg, cfg.source_server, cfg.source_db), autocommit=False)
    return conn


def open_target(cfg: EnvConfig) -> pyodbc.Connection:
    conn = pyodbc.connect(_conn_str(cfg, cfg.target_server, cfg.target_db), autocommit=False)
    return conn


# --------------------------------------------------------------------------- #
# --test-connection mode
# --------------------------------------------------------------------------- #

def _probe(label: str, opener) -> bool:
    log = logging.getLogger()
    log.info("--- %s ---", label)
    start = time.perf_counter()
    try:
        conn = opener()
    except Exception as exc:  # pragma: no cover - reported to user
        elapsed = (time.perf_counter() - start) * 1000
        log.error("%s FAIL after %.0f ms: %s", label, elapsed, exc)
        return False

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT @@SERVERNAME, DB_NAME(), SUSER_SNAME(), @@VERSION"
        )
        server, database, login, version = cur.fetchone()
        elapsed = (time.perf_counter() - start) * 1000
        log.info("%s PASS (%.0f ms)", label, elapsed)
        log.info("    server  : %s", server)
        log.info("    database: %s", database)
        log.info("    login   : %s", login)
        # @@VERSION is multi-line; keep only the first line at INFO, full at DEBUG.
        first_line = str(version).splitlines()[0] if version else ""
        log.info("    version : %s", first_line)
        log.debug("    full version:\n%s", version)
        return True
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        log.error("%s FAIL during probe after %.0f ms: %s", label, elapsed, exc)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_test_connection(cfg: EnvConfig) -> int:
    log = logging.getLogger()
    log.info("Testing connections for env=%s", cfg.env.upper())
    log.info("Driver: %s", cfg.driver)
    src_ok = _probe(
        f"SOURCE  ({cfg.source_server} / {cfg.source_db})",
        lambda: open_source(cfg),
    )
    tgt_ok = _probe(
        f"TARGET  ({cfg.target_server} / {cfg.target_db})",
        lambda: open_target(cfg),
    )
    log.info("")
    if src_ok and tgt_ok:
        log.info("Connection test: ALL PASS")
        return 0
    log.error("Connection test: FAILED (source=%s target=%s)",
              "OK" if src_ok else "FAIL",
              "OK" if tgt_ok else "FAIL")
    return 1


# --------------------------------------------------------------------------- #
# Source loading
# --------------------------------------------------------------------------- #

def load_source_rows(
    conn: pyodbc.Connection,
    required: set[str] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    log = logging.getLogger()
    # SOURCE_QUERY_PATH is a compile-time constant path next to the script;
    # the file's contents are trusted config authored by the developer.
    # utf-8-sig transparently strips a BOM if the file has one.
    sql = SOURCE_QUERY_PATH.read_text(encoding="utf-8-sig")
    # SET NOCOUNT ON avoids "description is None" on drivers that surface
    # DONE_IN_PROC row-count messages from subqueries before the real SELECT.
    exec_sql = "SET NOCOUNT ON;\n" + sql
    log.info("Executing source query: %s", SOURCE_QUERY_PATH)
    cur = conn.cursor()
    cur.execute(exec_sql)
    # Advance past any leading result-less result sets, just in case.
    while cur.description is None:
        if not cur.nextset():
            raise SystemExit(
                "Source query executed but returned no result set. "
                "Ensure source_query.sql ends with a SELECT."
            )
    columns = [d[0] for d in cur.description]
    rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    log.info("Source query returned %d row(s); columns: %s", len(rows), columns)
    _validate_source_columns(columns, required=required)
    return columns, rows


def _validate_identifier(name: str) -> str:
    """Ensure `name` is a safe SQL identifier before it is interpolated into a
    query. Anything unusual is rejected with SystemExit so a malformed source
    query can never smuggle SQL through a column-name position."""
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise SystemExit(
            f"Refusing to use unsafe column identifier from source query: {name!r}. "
            f"Column names must match {_IDENTIFIER_RE.pattern}."
        )
    return name


def _validate_source_columns(
    columns: list[str],
    required: set[str] | None = None,
) -> None:
    for col in columns:
        _validate_identifier(col)
    if required is None:
        required = {PARENT_LOOKUP_COLUMN, TARGET_KEY_COLUMN}
    missing = required - set(columns)
    if missing:
        raise SystemExit(
            f"Source query is missing required column(s): {', '.join(sorted(missing))}. "
            f"See source_query.sql for requirements."
        )


# --------------------------------------------------------------------------- #
# Per-row sync
# --------------------------------------------------------------------------- #

def _parent_exists(cur: pyodbc.Cursor, producer_code: Any) -> bool:
    cur.execute(
        f"SELECT 1 FROM {TARGET_SCHEMA}.{PARENT_TABLE} "
        f"WHERE {PARENT_LOOKUP_COLUMN} = ?",
        producer_code,
    )
    return cur.fetchone() is not None


def _fetch_existing(cur: pyodbc.Cursor, insert_columns: list[str],
                    product_client_key: Any) -> dict[str, Any] | None:
    # Each identifier was validated in _validate_source_columns() before we
    # reached here, so bracket-quoting them is safe.
    col_list = ", ".join(f"[{_validate_identifier(c)}]" for c in insert_columns)
    cur.execute(
        f"SELECT {col_list} FROM {TARGET_SCHEMA}.{TARGET_TABLE} "
        f"WHERE {TARGET_KEY_COLUMN} = ?",
        product_client_key,
    )
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(insert_columns, row))


def _diff(source: dict[str, Any], existing: dict[str, Any],
          columns: list[str]) -> list[str]:
    diffs: list[str] = []
    for col in columns:
        s = source.get(col)
        t = existing.get(col)
        if _normalize(s) != _normalize(t):
            diffs.append(f"{col}: source={s!r} target={t!r}")
    return diffs


def _normalize(v: Any) -> Any:
    # Treat trailing whitespace on strings as insignificant — SQL Server char/varchar
    # comparisons commonly ignore trailing spaces, and reporting those as diffs
    # would be noisy.
    if isinstance(v, str):
        return v.rstrip()
    return v


def _insert(cur: pyodbc.Cursor, row: dict[str, Any],
            insert_columns: list[str]) -> None:
    # Identifiers validated in _validate_source_columns(); values are always
    # passed as parameters.
    col_list = ", ".join(f"[{_validate_identifier(c)}]" for c in insert_columns)
    placeholders = ", ".join("?" for _ in insert_columns)
    values = [row[c] for c in insert_columns]
    cur.execute(
        f"INSERT INTO {TARGET_SCHEMA}.{TARGET_TABLE} ({col_list}) "
        f"VALUES ({placeholders})",
        *values,
    )


@dataclass
class RowResult:
    producer_code: Any
    product_client_key: Any
    status: str
    differences: str = ""
    error_message: str = ""


def process_rows(target: pyodbc.Connection, source_columns: list[str],
                 rows: list[dict[str, Any]], dry_run: bool) -> list[RowResult]:
    log = logging.getLogger()
    insert_columns = [c for c in source_columns if c not in SOURCE_ONLY_COLUMNS]
    log.info("Insert columns (%d): %s", len(insert_columns), insert_columns)

    results: list[RowResult] = []
    for idx, row in enumerate(rows, start=1):
        producer_code = row.get(PARENT_LOOKUP_COLUMN)
        product_client_key = row.get(TARGET_KEY_COLUMN)
        log.debug("[%d/%d] ProducerCode=%r ProductClientKey=%r",
                  idx, len(rows), producer_code, product_client_key)

        try:
            cur = target.cursor()

            if not _parent_exists(cur, producer_code):
                log.warning("[%d/%d] PARENT_NOT_FOUND ProducerCode=%r",
                            idx, len(rows), producer_code)
                results.append(RowResult(
                    producer_code, product_client_key,
                    STATUS_PARENT_NOT_FOUND,
                    error_message=(
                        f"No {TARGET_SCHEMA}.{PARENT_TABLE} row with "
                        f"{PARENT_LOOKUP_COLUMN}={producer_code!r}"
                    ),
                ))
                continue

            existing = _fetch_existing(cur, insert_columns, product_client_key)
            if existing is not None:
                diffs = _diff(row, existing, insert_columns)
                if diffs:
                    log.info("[%d/%d] EXISTS_DIFF ProductClientKey=%r diffs=%d",
                             idx, len(rows), product_client_key, len(diffs))
                    for d in diffs:
                        log.debug("    %s", d)
                    results.append(RowResult(
                        producer_code, product_client_key,
                        STATUS_EXISTS_DIFF, differences=" | ".join(diffs),
                    ))
                else:
                    log.info("[%d/%d] EXISTS_MATCH ProductClientKey=%r",
                             idx, len(rows), product_client_key)
                    results.append(RowResult(
                        producer_code, product_client_key, STATUS_EXISTS_MATCH,
                    ))
                continue

            # Not found — insert (or would-insert).
            if dry_run:
                log.info("[%d/%d] INSERT_DRY_RUN ProductClientKey=%r",
                         idx, len(rows), product_client_key)
                results.append(RowResult(
                    producer_code, product_client_key, STATUS_INSERT_DRY_RUN,
                ))
                continue

            _insert(cur, row, insert_columns)
            target.commit()
            log.info("[%d/%d] INSERTED ProductClientKey=%r",
                     idx, len(rows), product_client_key)
            results.append(RowResult(
                producer_code, product_client_key, STATUS_INSERTED,
            ))

        except Exception as exc:
            try:
                target.rollback()
            except Exception:
                pass
            log.exception("[%d/%d] ERROR ProductClientKey=%r: %s",
                          idx, len(rows), product_client_key, exc)
            results.append(RowResult(
                producer_code, product_client_key,
                STATUS_ERROR, error_message=str(exc),
            ))

    return results


# --------------------------------------------------------------------------- #
# --generate-sql mode: build INSERT statements without executing them
# --------------------------------------------------------------------------- #

def _sql_literal(v: Any) -> str:
    """Format a Python value as a T-SQL literal for logging / paste-ready SQL.

    Not used to execute anything — real inserts always go through parameterized
    queries. Kept conservative on purpose: single quotes are escaped by doubling.
    """
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "'"
    if isinstance(v, date):
        return "'" + v.strftime("%Y-%m-%d") + "'"
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    # Fallback: treat as string, escape single quotes.
    s = str(v).replace("'", "''")
    return "N'" + s + "'"


def run_generate_sql(
    target: pyodbc.Connection,
    source_columns: list[str],
    rows: list[dict[str, Any]],
    sql_path: Path,
    missing_log_path: Path,
    env: str,
) -> int:
    """For each source row, look up the producer in pvt.dmProducer by DealerId,
    then either:
      - write an INSERT statement to `sql_path` (exactly one producer match), or
      - write an entry to `missing_log_path` (zero / many matches, NULL key, or
        lookup error).
    The generated .sql wraps every INSERT in a single SET XACT_ABORT / BEGIN
    TRY-CATCH transaction so it commits atomically or rolls back on any error.
    Never writes to the target.
    """
    log = logging.getLogger()

    excluded = {DEALER_ID_COLUMN} | SOURCE_ONLY_COLUMNS
    insert_columns = [c for c in source_columns if c not in excluded]

    log.info("Generate-SQL mode (no writes will be performed).")
    log.info("Lookup: %s.%s WHERE %s = <source.%s>",
             TARGET_SCHEMA, PARENT_TABLE,
             PRODUCER_DEALER_ID_COLUMN, DEALER_ID_COLUMN)
    log.info("Producer key column pulled from %s.%s: %s -> target column %s",
             TARGET_SCHEMA, PARENT_TABLE,
             PRODUCER_CLIENT_KEY_COLUMN, TARGET_KEY_COLUMN)
    log.info("INSERT columns (%d): %s", len(insert_columns), insert_columns)
    log.info("SQL script output  : %s", sql_path)
    log.info("Missing-data log   : %s", missing_log_path)

    # Validate every identifier we're about to interpolate. Source columns were
    # already validated in _validate_source_columns; re-check the target-side
    # identifiers explicitly so this function is safe in isolation.
    for name in (
        PARENT_LOOKUP_COLUMN, TARGET_KEY_COLUMN,
        DEALER_ID_COLUMN, PRODUCER_DEALER_ID_COLUMN,
        PRODUCER_CLIENT_KEY_COLUMN,
    ):
        _validate_identifier(name)

    lookup_sql = (
        f"SELECT [{PRODUCER_CLIENT_KEY_COLUMN}] "
        f"FROM {TARGET_SCHEMA}.{PARENT_TABLE} "
        f"WHERE [{PRODUCER_DEALER_ID_COLUMN}] = ?"
    )

    insert_statements: list[str] = []
    missing_lines: list[str] = []

    n_generated = 0
    n_not_found = 0
    n_ambiguous = 0
    n_error = 0
    n_null_key = 0

    for idx, row in enumerate(rows, start=1):
        dealer_id = row.get(DEALER_ID_COLUMN)
        try:
            cur = target.cursor()
            cur.execute(lookup_sql, dealer_id)
            matches = cur.fetchall()
        except Exception as exc:
            n_error += 1
            missing_lines.append(
                f"[LOOKUP_ERROR] DealerId={dealer_id!r} error={exc}"
            )
            log.error("[%d/%d] LOOKUP_ERROR DealerId=%r: %s",
                      idx, len(rows), dealer_id, exc)
            continue

        if len(matches) == 0:
            n_not_found += 1
            missing_lines.append(
                f"[PRODUCER_NOT_FOUND] DealerId={dealer_id!r} "
                f"(no rows in {TARGET_SCHEMA}.{PARENT_TABLE} where "
                f"{PRODUCER_DEALER_ID_COLUMN}={dealer_id!r})"
            )
            continue
        if len(matches) > 1:
            n_ambiguous += 1
            missing_lines.append(
                f"[PRODUCER_AMBIGUOUS] DealerId={dealer_id!r} "
                f"found {len(matches)} rows in {TARGET_SCHEMA}.{PARENT_TABLE}"
            )
            continue

        producer_client_key = matches[0][0]
        if producer_client_key is None:
            n_null_key += 1
            missing_lines.append(
                f"[PRODUCER_KEY_NULL] DealerId={dealer_id!r} "
                f"({PRODUCER_CLIENT_KEY_COLUMN} is NULL on the matched "
                f"{TARGET_SCHEMA}.{PARENT_TABLE} row)"
            )
            continue

        # Build the INSERT: ProductClientKey first, then bank fields.
        all_cols = [TARGET_KEY_COLUMN] + insert_columns
        all_vals = [producer_client_key] + [row[c] for c in insert_columns]
        col_list = ", ".join(f"[{c}]" for c in all_cols)
        val_list = ", ".join(_sql_literal(v) for v in all_vals)
        insert_statements.append(
            f"    INSERT INTO {TARGET_SCHEMA}.{TARGET_TABLE} "
            f"({col_list}) VALUES ({val_list});  -- DealerId={dealer_id}"
        )
        n_generated += 1

    # --- Write .sql script ---------------------------------------------------
    sql_path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_lines = [
        "-- BankInfoSync generated INSERT script",
        f"-- Env          : {env.upper()}",
        f"-- Generated at : {generated_at}",
        f"-- Source rows  : {len(rows)}",
        f"-- Inserts      : {n_generated}",
        f"-- Skipped      : {n_not_found + n_ambiguous + n_null_key + n_error} "
        f"(see companion .log)",
        f"--   not found  : {n_not_found}",
        f"--   ambiguous  : {n_ambiguous}",
        f"--   null key   : {n_null_key}",
        f"--   lookup err : {n_error}",
        "--",
        "-- Review before running. Wrapped in a single TRY/CATCH transaction:",
        "-- either all inserts commit, or every insert is rolled back.",
        "",
        "SET XACT_ABORT ON;",
        "SET NOCOUNT ON;",
        "",
        "BEGIN TRY",
        "    BEGIN TRANSACTION;",
        "",
    ]
    footer_lines = [
        "",
        "    COMMIT TRANSACTION;",
        f"    PRINT 'BankInfoSync: committed {n_generated} row(s) into "
        f"{TARGET_SCHEMA}.{TARGET_TABLE}.';",
        "END TRY",
        "BEGIN CATCH",
        "    IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;",
        "    DECLARE @ErrMsg  NVARCHAR(4000) = ERROR_MESSAGE(),",
        "            @ErrNum  INT            = ERROR_NUMBER(),",
        "            @ErrLine INT            = ERROR_LINE(),",
        "            @ErrProc SYSNAME        = ISNULL(ERROR_PROCEDURE(), N'<batch>');",
        "    RAISERROR('BankInfoSync failed in %s at line %d (Msg %d): %s',",
        "              16, 1, @ErrProc, @ErrLine, @ErrNum, @ErrMsg);",
        "END CATCH;",
        "",
    ]
    with sql_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(header_lines) + "\n")
        if insert_statements:
            f.write("\n".join(insert_statements) + "\n")
        else:
            f.write("    -- No INSERT statements generated. "
                    "See companion missing-data log.\n")
        f.write("\n".join(footer_lines))

    # --- Write missing-data log ---------------------------------------------
    missing_log_path.parent.mkdir(parents=True, exist_ok=True)
    with missing_log_path.open("w", encoding="utf-8") as f:
        f.write("# BankInfoSync missing-data log\n")
        f.write(f"# Env               : {env.upper()}\n")
        f.write(f"# Generated at      : {generated_at}\n")
        f.write(f"# Total source rows : {len(rows)}\n")
        f.write(f"# Missing / skipped : {len(missing_lines)}\n")
        f.write(f"#   PRODUCER_NOT_FOUND : {n_not_found}\n")
        f.write(f"#   PRODUCER_AMBIGUOUS : {n_ambiguous}\n")
        f.write(f"#   PRODUCER_KEY_NULL  : {n_null_key}\n")
        f.write(f"#   LOOKUP_ERROR       : {n_error}\n")
        f.write("\n")
        for line in missing_lines:
            f.write(line + "\n")

    log.info("")
    log.info("========== GENERATE-SQL SUMMARY ==========")
    log.info("Total source rows      : %d", len(rows))
    log.info("Generated INSERTs      : %d", n_generated)
    log.info("Producer not found     : %d", n_not_found)
    log.info("Producer ambiguous     : %d", n_ambiguous)
    log.info("Producer key was NULL  : %d", n_null_key)
    log.info("Lookup errors          : %d", n_error)
    log.info("SQL script written     : %s", sql_path)
    log.info("Missing-data log       : %s", missing_log_path)
    log.info("==========================================")

    # Non-zero exit only if nothing was generated at all.
    return 0 if n_generated > 0 else 3


# --------------------------------------------------------------------------- #
# Report / summary
# --------------------------------------------------------------------------- #

REPORT_COLUMNS = (
    "producer_code",
    "product_client_key",
    "status",
    "differences",
    "error_message",
)


def _csv_safe(value: Any) -> str:
    """Prevent CSV formula injection (OWASP) by prefixing any value that starts
    with a spreadsheet formula trigger character with a single quote."""
    text = "" if value is None else str(value)
    if text.startswith(_CSV_UNSAFE_PREFIXES):
        return "'" + text
    return text


def write_report(path: Path, results: list[RowResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(REPORT_COLUMNS)
        for r in results:
            writer.writerow([
                _csv_safe(r.producer_code),
                _csv_safe(r.product_client_key),
                _csv_safe(r.status),
                _csv_safe(r.differences),
                _csv_safe(r.error_message),
            ])


def log_summary(results: list[RowResult], elapsed_s: float) -> None:
    log = logging.getLogger()
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    log.info("")
    log.info("========== SUMMARY ==========")
    log.info("Total source rows: %d", len(results))
    for status in (
        STATUS_INSERTED, STATUS_INSERT_DRY_RUN,
        STATUS_EXISTS_MATCH, STATUS_EXISTS_DIFF,
        STATUS_PARENT_NOT_FOUND, STATUS_ERROR,
    ):
        if status in counts:
            log.info("  %-18s %d", status, counts[status])
    log.info("Elapsed: %.2f s", elapsed_s)
    log.info("=============================")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy bank-info records from a source SQL Server to "
            f"{TARGET_SCHEMA}.{TARGET_TABLE} on a target SQL Server."
        ),
    )
    parser.add_argument(
        "--env",
        choices=ENV_CHOICES,
        help="Target environment. If omitted, you will be prompted.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not insert; report what would happen.",
    )
    parser.add_argument(
        "--test-connection",
        action="store_true",
        help="Open source and target connections, print server/db/login, then exit.",
    )
    parser.add_argument(
        "--generate-sql",
        action="store_true",
        help=(
            "Do not insert. For each source row, look up pvt.dmProducer by "
            "DealerId; on exactly one match, log a ready-to-paste INSERT "
            "statement for pvt.dmProducerBankInfo. Zero or many matches are "
            "logged as errors."
        ),
    )
    return parser.parse_args(argv)


def prompt_for_env() -> str:
    while True:
        raw = input(f"Select environment [{'/'.join(ENV_CHOICES)}]: ").strip().lower()
        if raw in ENV_CHOICES:
            return raw
        print(f"  Invalid choice. Enter one of: {', '.join(ENV_CHOICES)}")


def _sanitize_env(raw: str) -> str:
    """Launder the CLI-supplied environment value through a hard-coded branch so
    every downstream use is a compile-time string constant. This both prevents
    any injection via the value and gives static analyzers (Snyk / CodeQL)
    a clear allow-list sanitizer they can recognize."""
    if raw == "accp":
        return "accp"
    if raw == "prod":
        return "prod"
    raise SystemExit(
        f"Invalid environment: {raw!r}. Must be one of {ENV_CHOICES}."
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.generate_sql and (args.dry_run or args.test_connection):
        raise SystemExit(
            "--generate-sql cannot be combined with --dry-run or --test-connection."
        )
    env = _sanitize_env(args.env or prompt_for_env())
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")

    log_path = setup_logging(env, run_id)
    log = logging.getLogger()
    log.info(
        "BankInfoSync starting (env=%s, dry_run=%s, test_connection=%s, generate_sql=%s)",
        env.upper(), args.dry_run, args.test_connection, args.generate_sql,
    )
    log.info("Log file: %s", log_path)

    cfg = load_config(env)
    log.debug("Config: %s", cfg)

    if args.test_connection:
        return run_test_connection(cfg)

    if not SOURCE_QUERY_PATH.is_file():
        log.error("Source query file not found: %s", SOURCE_QUERY_PATH)
        return 2

    if args.generate_sql:
        sql_path = REPORT_DIR / f"bankinfo-insert-{env}-{run_id}.sql"
        missing_log_path = REPORT_DIR / f"bankinfo-missing-{env}-{run_id}.log"
        start = time.perf_counter()
        try:
            with open_source(cfg) as src, open_target(cfg) as tgt:
                src_columns, rows = load_source_rows(
                    src, required={DEALER_ID_COLUMN},
                )
                if not rows:
                    log.warning("No source rows returned — nothing to do.")
                    return 0
                rc = run_generate_sql(
                    tgt, src_columns, rows,
                    sql_path=sql_path,
                    missing_log_path=missing_log_path,
                    env=env,
                )
        except SystemExit:
            raise
        except Exception:
            log.exception("Fatal error during generate-sql run")
            return 1
        log.info("Elapsed: %.2f s", time.perf_counter() - start)
        return rc

    report_path = REPORT_DIR / f"bankinfo-sync-{env}-{run_id}.csv"

    start = time.perf_counter()
    try:
        with open_source(cfg) as src, open_target(cfg) as tgt:
            src_columns, rows = load_source_rows(src)
            if not rows:
                log.warning("No source rows returned — nothing to do.")
                write_report(report_path, [])
                log.info("Empty report written: %s", report_path)
                return 0
            results = process_rows(tgt, src_columns, rows, dry_run=args.dry_run)
    except SystemExit:
        raise
    except Exception:
        log.exception("Fatal error during sync run")
        return 1

    write_report(report_path, results)
    log.info("Report written: %s", report_path)
    log_summary(results, time.perf_counter() - start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
