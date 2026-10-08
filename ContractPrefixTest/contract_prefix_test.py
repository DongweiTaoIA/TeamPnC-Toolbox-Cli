"""ContractPrefixTest — verify contract-number prefix configuration.

Stage 1 validates the rows in `cfProductPrfx` against the business test-case set.
Stage 2 calls `dbo.DPP_GenContractNumber` for every expanded case and checks the
prefix of the generated contract number.
Stage 3 creates a real quote and contract per program/province and checks the
prefix of the resulting contract number. Stage 3 writes data that is not cleaned
up — never point this tool at production.

Auth is 100% Windows Integrated (`Trusted_Connection=yes`); the script handles no
passwords. Run `python contract_prefix_test.py --help` for usage.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pyodbc
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent
REPORT_DIR = SCRIPT_DIR / "reports"

PRODUCT_ID = "EW"
USER_ID = "SXR"
LANG = "E"

PROVINCES = (
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
)

CONTRACT_TYPES = (
    "AT", "AU", "GC", "HT", "MA", "MF", "MN", "MS", "NT", "PM", "PU", "PW",
    "RV", "SC", "SL", "SM", "SS", "SU", "TT", "UT", "W5",
)

# Exit codes (see Requirements.md §7).
EXIT_OK = 0
EXIT_TEST_FAILED = 1
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3

# Cap on how many individual failures are printed per stage; all are counted.
MAX_REPORTED_FAILURES = 25

# Cap on how many failure lines are carried into the PDF report.
MAX_PDF_FAILURES = 200

ENVIRONMENTS = {
    "fnct": "FNCT",
    "accp": "ACCP",
}


@dataclass(frozen=True)
class TestCase:
    program_id: str
    oem: str
    lienholders: tuple[str, ...]
    prefix: str


# Supplied by Business (Requirements.md §5.1).
TEST_CASES: tuple[TestCase, ...] = (
    TestCase("AU242", "HO", ("NA", "FD", "IF"), "HCPM"),
    TestCase("AU243", "AC", ("NA", "FD", "IF"), "ACPM"),
    TestCase("AU248", "HO", ("NA", "FD", "IF"), "HCPM"),
    TestCase("AU249", "HO", ("NA", "FD", "IF"), "HCPM"),
    TestCase("AU250", "AC", ("NA", "FD", "IF"), "ACPM"),
    TestCase("AU240", "HO", ("NA", "FD", "IF"), "HCPR"),
    TestCase("AU241", "AC", ("NA", "FD", "IF"), "ACPR"),
    TestCase("AU251", "HO", ("NA", "FD", "IF"), "HCPR"),
    TestCase("AU252", "AC", ("NA", "FD", "IF"), "ACPR"),
    TestCase("AU244", "HO", ("NA", "FD", "IF"), "HCOP"),
    TestCase("AU245", "AC", ("NA", "FD", "IF"), "ACOP"),
    TestCase("AU246", "HO", ("NA", "FD", "IF"), "HCAL"),
    TestCase("AU247", "AC", ("NA", "FD", "IF"), "ACAL"),
)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EnvConfig:
    env: str
    driver: str
    server: str
    database: str
    trust_server_cert: bool


def load_config(env: str) -> EnvConfig:
    """Read connection settings for `env` from the local .env file."""
    load_dotenv(SCRIPT_DIR / ".env")
    prefix = env.upper()

    required = {
        "server": f"{prefix}_SERVER",
        "database": f"{prefix}_DB",
    }
    values: dict[str, str] = {}
    missing: list[str] = []
    for field, var in required.items():
        value = os.getenv(var, "").strip()
        if not value:
            missing.append(var)
        values[field] = value
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables in .env: {', '.join(missing)}. "
            "Copy .env.example to .env and fill in the values."
        )

    driver = os.getenv("ODBC_DRIVER", "ODBC Driver 17 for SQL Server").strip()
    trust = os.getenv(f"{prefix}_TRUST_SERVER_CERT", "no").strip().lower() in {
        "1", "true", "yes", "y",
    }

    return EnvConfig(env=prefix, driver=driver, trust_server_cert=trust, **values)


def connect(cfg: EnvConfig) -> pyodbc.Connection:
    """Open a Windows-authenticated connection to the environment database.

    autocommit is on: Stage 2 legitimately consumes contract-number sequence
    values and is not rolled back (Requirements.md §6.3).
    """
    parts = [
        f"DRIVER={{{cfg.driver}}}",
        f"SERVER={cfg.server}",
        f"DATABASE={cfg.database}",
        "Trusted_Connection=yes",
        "Encrypt=yes",
    ]
    if cfg.trust_server_cert:
        parts.append("TrustServerCertificate=yes")
    return pyodbc.connect(";".join(parts) + ";", autocommit=True)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _clean(value: object) -> str:
    """Normalize a database value for comparison: string, trimmed, upper-cased."""
    return "" if value is None else str(value).strip().upper()


def _lienholder_set(raw: object) -> frozenset[str]:
    """Split a comma-separated lienholder group into a set of codes.

    Compared as a set so ordering and spacing differences are not failures
    (Requirements.md §6, Stage 1).
    """
    return frozenset(
        code for code in (piece.strip().upper() for piece in _clean(raw).split(","))
        if code
    )


class FailureLog:
    """Collects failures for one stage, printing only the first few."""

    def __init__(self, limit: int = MAX_REPORTED_FAILURES) -> None:
        self.limit = limit
        self.count = 0
        self.messages: list[str] = []

    def add(self, message: str) -> None:
        self.count += 1
        if len(self.messages) < MAX_PDF_FAILURES:
            self.messages.append(message)
        if self.count <= self.limit:
            print(f"    FAIL {message}")
        elif self.count == self.limit + 1:
            print(f"    ... further failures suppressed (showing first {self.limit})")


@dataclass
class StageResult:
    """Outcome of one stage, used for both the console summary and the PDF."""
    name: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    record_headers: list[str] = field(default_factory=list)
    records: list[list[str]] = field(default_factory=list)
    executed: bool = True

    @property
    def ok(self) -> bool:
        return self.failed == 0


# --------------------------------------------------------------------------- #
# Stage 1 — Data validation
# --------------------------------------------------------------------------- #

STAGE1_SQL_TEMPLATE = """
SELECT ProgramId, OEM, ProvState, ContractType, LienHolderGroups, PrfxContractNum
FROM   dbo.cfProductPrfx
WHERE  ProductId = ?
  AND  ProgramId IN ({placeholders})
"""


def _expected_prefix_rows() -> dict[tuple[str, str, str, str], TestCase]:
    """Expand the test cases to one expected row per program/province/contract type."""
    expected: dict[tuple[str, str, str, str], TestCase] = {}
    for case in TEST_CASES:
        for province in PROVINCES:
            for contract_type in CONTRACT_TYPES:
                key = (case.program_id, case.oem, province, contract_type)
                expected[key] = case
    return expected


def run_stage1(conn: pyodbc.Connection) -> StageResult:
    """Validate cfProductPrfx against the test-case set."""
    print("Stage 1 - cfProductPrfx data validation")
    result = StageResult(name="Stage 1 - cfProductPrfx data validation")

    expected = _expected_prefix_rows()
    programs = sorted({case.program_id for case in TEST_CASES})

    # Only '?' markers are interpolated; every value is passed as a bound parameter.
    placeholders = ",".join("?" for _ in programs)
    sql = STAGE1_SQL_TEMPLATE.format(placeholders=placeholders)

    with conn.cursor() as cursor:
        cursor.execute(sql, [PRODUCT_ID, *programs])
        rows = cursor.fetchall()

    failures = FailureLog()
    seen: set[tuple[str, str, str, str]] = set()
    extra = 0

    for row in rows:
        key = (_clean(row.ProgramId), _clean(row.OEM), _clean(row.ProvState),
               _clean(row.ContractType))
        case = expected.get(key)
        if case is None:
            extra += 1
            message = f"EXTRA row {'/'.join(key)} prefix={_clean(row.PrfxContractNum)}"
            if extra <= MAX_REPORTED_FAILURES:
                print(f"    {message}")
            if len(result.failures) < MAX_PDF_FAILURES:
                result.failures.append(message)
            continue

        if key in seen:
            failures.add(f"{'/'.join(key)} duplicate row")
            continue
        seen.add(key)

        actual_prefix = _clean(row.PrfxContractNum)
        if actual_prefix != case.prefix:
            failures.add(
                f"{'/'.join(key)} prefix expected={case.prefix} actual={actual_prefix}"
            )
            continue

        actual_groups = _lienholder_set(row.LienHolderGroups)
        if actual_groups != frozenset(case.lienholders):
            failures.add(
                f"{'/'.join(key)} lienholders expected={','.join(sorted(case.lienholders))} "
                f"actual={','.join(sorted(actual_groups)) or '(empty)'}"
            )

    missing = sorted(set(expected) - seen)
    for key in missing[:MAX_REPORTED_FAILURES]:
        print(f"    MISSING row {'/'.join(key)}")
    if len(missing) > MAX_REPORTED_FAILURES:
        print(f"    ... {len(missing) - MAX_REPORTED_FAILURES} more missing rows")
    for key in missing:
        if len(result.failures) >= MAX_PDF_FAILURES:
            break
        result.failures.append(f"MISSING row {'/'.join(key)}")

    result.failures.extend(failures.messages[: MAX_PDF_FAILURES - len(result.failures)])
    result.total = len(expected)
    result.failed = failures.count + len(missing) + extra
    result.passed = len(seen) - failures.count
    result.notes.append(f"Missing rows: {len(missing)}    Unexpected rows: {extra}")

    print(
        f"  total={result.total} passed={result.passed} failed={failures.count} "
        f"missing={len(missing)} extra={extra}"
    )
    return result


# --------------------------------------------------------------------------- #
# Stage 2 — DPP_GenContractNumber unit test
# --------------------------------------------------------------------------- #

DEALER_SQL = """
SELECT DealerId, ProvState, OEM
FROM   dbo.dmDealer
WHERE  OEM IN ('HO', 'AC')
  AND  DealerStat = 'A'
"""

GEN_CONTRACT_NUMBER_SQL = """
SET NOCOUNT ON;
DECLARE @psNum CHAR(12);
EXEC dbo.DPP_GenContractNumber
      @psProductId    = ?
    , @psContractType = ?
    , @psDealerId     = ?
    , @psLienHolderId = ?
    , @psProgramId    = ?
    , @psGPProgramId  = NULL
    , @psNum          = @psNum OUTPUT
    , @psLang         = ?
    , @psUserId       = ?;
SELECT @psNum;
"""


def resolve_dealers(conn: pyodbc.Connection) -> dict[tuple[str, str], str]:
    """Pick one active dealer per OEM + province, lowest DealerId for repeatability."""
    with conn.cursor() as cursor:
        cursor.execute(DEALER_SQL)
        rows = cursor.fetchall()

    dealers: dict[tuple[str, str], str] = {}
    valid_provinces = set(PROVINCES)
    for row in rows:
        province = _clean(row.ProvState)
        if province not in valid_provinces:
            continue
        key = (_clean(row.OEM), province)
        dealer_id = _clean(row.DealerId)
        if not dealer_id:
            continue
        current = dealers.get(key)
        if current is None or dealer_id < current:
            dealers[key] = dealer_id
    return dealers


def _fetch_all_sets(cursor: pyodbc.Cursor) -> list[list[pyodbc.Row]]:
    """Collect every non-empty result set the batch produced.

    These procedures emit warning/validation result sets before the final SELECT,
    so the output values are in the *last* set, not the first.
    """
    sets: list[list[pyodbc.Row]] = []
    while True:
        if cursor.description is not None:
            rows = cursor.fetchall()
            if rows:
                sets.append(rows)
        if not cursor.nextset():
            return sets


def _messages(sets: list[list[pyodbc.Row]]) -> str:
    """Flatten the non-output result sets into a short diagnostic string."""
    lines = [
        " ".join(str(v) for v in row if v is not None)
        for rows in sets for row in rows
    ]
    text = " | ".join(line.strip() for line in lines if line.strip())
    return text[:300]


def _fetch_row(cursor: pyodbc.Cursor) -> pyodbc.Row | None:
    """Return the final row of the batch, skipping result sets that carry none."""
    sets = _fetch_all_sets(cursor)
    return sets[-1][-1] if sets else None


def _fetch_output(cursor: pyodbc.Cursor) -> str | None:
    """Return the first scalar of the batch's final result set."""
    row = _fetch_row(cursor)
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _check_contract_number(number: str | None, expected_prefix: str) -> str | None:
    """Return a failure reason, or None when the contract number is valid."""
    if number is None:
        return "no contract number returned"
    if len(number) != 12:
        return f"expected 12 characters, got {len(number)} ({number!r})"
    prefix, sequence = number[:4], number[4:]
    if prefix.upper() != expected_prefix:
        return f"prefix expected={expected_prefix} actual={prefix}"
    if not sequence.isdigit():
        return f"characters 5-12 are not all digits ({sequence!r})"
    return None


def run_stage2(conn: pyodbc.Connection) -> StageResult:
    """Exercise DPP_GenContractNumber across the expanded cases."""
    print("Stage 2 - DPP_GenContractNumber unit test")
    result = StageResult(name="Stage 2 - DPP_GenContractNumber unit test")

    dealers = resolve_dealers(conn)

    tested_oems = sorted({case.oem for case in TEST_CASES})
    not_covered = [
        f"{oem}/{province}"
        for oem in tested_oems
        for province in PROVINCES
        if (oem, province) not in dealers
    ]
    if not_covered:
        print(f"  not covered (no active dealer): {', '.join(not_covered)}")
        result.notes.append(f"Not covered (no active dealer): {', '.join(not_covered)}")

    # One lienholder is sampled per case rather than testing all three; the prefix
    # does not vary by lienholder within a group (Requirements.md §5.3).
    cases = [
        (case, province, dealers[(case.oem, province)], contract_type,
         random.choice(case.lienholders))
        for case in TEST_CASES
        for province in PROVINCES
        if (case.oem, province) in dealers
        for contract_type in CONTRACT_TYPES
    ]

    total = len(cases)
    print(f"  running {total} cases ...")

    failures = FailureLog()
    with conn.cursor() as cursor:
        for index, (case, province, dealer_id, contract_type, lienholder) in enumerate(cases, 1):
            label = (
                f"{case.program_id}/{case.oem}/{province}/{dealer_id}/"
                f"{contract_type}/{lienholder}"
            )
            try:
                cursor.execute(
                    GEN_CONTRACT_NUMBER_SQL,
                    [PRODUCT_ID, contract_type, dealer_id, lienholder,
                     case.program_id, LANG, USER_ID],
                )
                number = _fetch_output(cursor)
            except pyodbc.OperationalError:
                raise
            except pyodbc.Error as exc:
                failures.add(f"{label} procedure error: {exc.args[-1]}")
                continue

            reason = _check_contract_number(number, case.prefix)
            if reason is not None:
                failures.add(f"{label} {reason}")

            if index % 500 == 0:
                print(f"    {index}/{total} ...")

    result.total = total
    result.failed = failures.count
    result.passed = total - failures.count
    result.skipped = len(not_covered)
    result.failures = failures.messages
    result.notes.append(
        f"Dealers resolved: {len(dealers)} of {len(tested_oems) * len(PROVINCES)} "
        "OEM/province combinations"
    )

    print(
        f"  total={total} passed={result.passed} failed={failures.count} "
        f"skipped={len(not_covered)} provinces"
    )
    return result


# --------------------------------------------------------------------------- #
# Stage 3 — create a real contract
# --------------------------------------------------------------------------- #

# Vehicle used on the generated quote, per OEM.
VEHICLE = {
    "HO": ("HONDA", "CIVIC"),
    "AC": ("ACURA", "MDX"),
}

CONTRACT_GROUP = "EAU"

PROGRAM_DETAIL_SQL = """
SELECT ProvState, ContractType
FROM   dbo.cfProgramDetl
WHERE  ProgramId = ?
  AND  GETDATE() BETWEEN EffectDt AND ExpiryDt
ORDER  BY ProvState, ContractType
"""

# DPP_DP153EWDealerProgram_Save requires the dealer-program date range to fall inside
# the program's own range for the dealer's province, so the enrolment reuses it.
PROGRAM_RANGE_SQL = """
SELECT MIN(EffectDt) AS EffectDt, MAX(ExpiryDt) AS ExpiryDt
FROM   dbo.cfProgramDetl
WHERE  ProgramId = ?
  AND  ProvState = ?
"""

DEALER_PROGRAM_SQL = """
SELECT TOP 1 p.DealerId
FROM   dbo.EW_cfDealerProgram p
JOIN   dbo.dmDealer d ON p.DealerId = d.DealerId
WHERE  p.ProgramId = ?
  AND  d.ProvState = ?
  AND  d.DealerStat = 'A'
  AND  d.OEM = ?
ORDER  BY p.DealerId
"""

# Several candidates: a dealer can be rejected because its own pre-existing programs
# breach the effective-date validation, which has nothing to do with this test.
CANDIDATE_DEALER_SQL = """
SELECT TOP 10 DealerId
FROM   dbo.dmDealer
WHERE  ProvState = ?
  AND  DealerStat = 'A'
  AND  OEM = ?
ORDER  BY DealerId
"""

# DPP_DP153EWDealerProgram_Save DELETES any program in the same contract group that
# is not present in @psProgramListXml, so the dealer's existing programs (and their
# service roles) must be read back and resubmitted unchanged.
EXISTING_PROGRAMS_SQL = """
SELECT p.ProgramId, p.DlrPortalYN, p.AdminDBYN, p.ExtDealerId, p.CBRuleId,
       p.AddressType, p.EffectDt, p.ExpiryDt,
       STUFF((SELECT ';' + CAST(r.DealerRoleID AS VARCHAR(10))
              FROM   dbo.EW_cfDealerServiceRoles r
              WHERE  r.DealerProgramID = p.DealerProgramKey
              FOR XML PATH('')), 1, 1, '') AS RoleIds
FROM   dbo.EW_cfDealerProgram p
JOIN   dbo.cfProgram g ON g.ProgramId = p.ProgramId AND g.ContractGroup = ?
WHERE  p.DealerId = ?
ORDER  BY p.ProgramId
"""

DEALER_SETUP_SQL = """
SELECT PremiumAdjustType, CancNotifYN, HoldInvoiceYN, NetRemittanceYN,
       HoldDealerCompYN, AllowPrintRatesYN
FROM   dbo.EW_cfDealerSetup
WHERE  DealerId = ?
"""

ENROL_DEALER_SQL = """
SET NOCOUNT ON;
DECLARE @xml XML = CONVERT(XML, ?);
EXEC dbo.DPP_DP153EWDealerProgram_Save
      @psDealerId          = ?
    , @psContractGroup     = ?
    , @psPremiumAdjustType = ?
    , @psCancNotifYN       = ?
    , @psHoldInvoiceYN     = ?
    , @psNetRemittanceYN   = ?
    , @psHoldDealerCompYN  = ?
    , @psAllowPrintRatesYN = ?
    , @psProgramListXml    = @xml
    , @psLang              = 'E'
    , @psUserId            = ?;
"""

SAVE_QUOTE_SQL = """
SET NOCOUNT ON;
DECLARE @xml XML = CONVERT(XML, ?);
DECLARE @quoteKey INT = 0;
DECLARE @quoteNum CHAR(10) = NULL;
EXEC dbo.DPP_DP608Quote_Save
      @psQuoteXml = @xml
    , @piQuoteKey = @quoteKey OUTPUT
    , @psQuoteNum = @quoteNum OUTPUT
    , @psLang     = 'E'
    , @psUserId   = ?;
SELECT @quoteKey, @quoteNum;
"""

QUOTE_TO_CONTRACT_SQL = """
SET NOCOUNT ON;
DECLARE @contractKey INT;
DECLARE @contractNum CHAR(12);
EXEC dbo.DPP_DP608Quote_Contract
      @piQuoteKey    = ?
    , @piQuoteSeqNum = 1
    , @piContractKey = @contractKey OUTPUT
    , @psContractNum = @contractNum OUTPUT
    , @psLang        = 'E'
    , @psUserId      = ?;
SELECT @contractKey, @contractNum;
"""

ENROL_PROGRAM_XML = (
    "<Prgm><PM>{program_id}</PM><PT>{portal}</PT><AD>{admin}</AD><EX>{ext}</EX>"
    "<CB>{cb_rule}</CB><BA>{address_type}</BA><EF>{effect}</EF><EP>{expiry}</EP>"
    "<DR>{roles}</DR></Prgm>"
)

DEALER_SETUP_DEFAULTS = ("L", "N", "N", "N", "N", "N")
XML_DATE = "%Y-%m-%dT%H:%M:%S"
FOREVER = "9999-12-31T00:00:00"

# Fixed sample quote; only the marked fields vary per case (Requirements.md §6.4).
QUOTE_XML = (
    "<Quote><K></K><P>EW</P><D>{dealer_id}</D><QDT>{today}</QDT><DDT>{today}</DDT>"
    "<EDT>{today}</EDT><VP>0</VP><KM>100</KM><ML>62</ML><CG>{contract_group}</CG>"
    "<CT>{contract_type}</CT><PG>{program_id}||S</PG><VIN/><VC>NE</VC>"
    "<MK>{make}</MK><MD>{model}</MD><YR>{year}</YR><AC>N</AC><DW>F</DW><FL>G</FL>"
    "<CY>4</CY><WT>36</WT><WTS>0</WTS><WKM>60000</WKM><WDT>{today}</WDT><CC>1</CC>"
    "<LN>Test</LN><FN>Test</FN><PH1>6041111111</PH1><PS>{prov_state}</PS><ISA>N</ISA>"
    "<IDTR>N</IDTR><CKEY>0</CKEY><US>N</US><MEM>0</MEM><MEK>0</MEK><ECC>0</ECC>"
    "<EHP>0</EHP><EDS>N</EDS><EYR>0</EYR><EHRS>0</EHRS><LF>0</LF><QS>U</QS>"
    "<Details><QuoteDetl><SN>0</SN><Covg><CT>BA</CT><T>36</T><KM>80000</KM><D>100</D>"
    "<BR>779</BR><DB>0</DB><PTA>0</PTA><R>779</R><M>0</M><MA>0</MA><T1T>GST</T1T>"
    "<T1V>0.05</T1V><T2V>0</T2V><RT1>38.95</RT1><RT2>0</RT2><LT1>38.95</LT1>"
    "<LT2>0</LT2><PT1>0</PT1><PT2>0</PT2></Covg><CK>0</CK><OptionCovgs>"
    "<OptionCovg><OID>EWRA</OID><OC>0</OC></OptionCovg>"
    "<OptionCovg><OID>EWSG</OID><OC>0</OC></OptionCovg></OptionCovgs>"
    "<Surcharges/></QuoteDetl></Details></Quote>"
)


def _program_availability(conn: pyodbc.Connection, program_id: str) -> list[tuple[str, str]]:
    with conn.cursor() as cursor:
        cursor.execute(PROGRAM_DETAIL_SQL, [program_id])
        return [(_clean(r.ProvState), _clean(r.ContractType)) for r in cursor.fetchall()]


def _find_enrolled_dealer(
    conn: pyodbc.Connection, program_id: str, province: str, oem: str
) -> str | None:
    with conn.cursor() as cursor:
        cursor.execute(DEALER_PROGRAM_SQL, [program_id, province, oem])
        row = cursor.fetchone()
    return _clean(row[0]) if row else None


def _xml_escape(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _existing_programs(conn: pyodbc.Connection, dealer_id: str) -> list[str]:
    """Render the dealer's current contract-group programs as <Prgm> elements."""
    with conn.cursor() as cursor:
        cursor.execute(EXISTING_PROGRAMS_SQL, [CONTRACT_GROUP, dealer_id])
        rows = cursor.fetchall()

    elements = []
    for row in rows:
        elements.append(ENROL_PROGRAM_XML.format(
            program_id=_xml_escape(row.ProgramId),
            portal=_xml_escape(row.DlrPortalYN) or "N",
            admin=_xml_escape(row.AdminDBYN) or "N",
            ext=_xml_escape(row.ExtDealerId),
            cb_rule=_xml_escape(row.CBRuleId),
            address_type=_xml_escape(row.AddressType),
            effect=row.EffectDt.strftime(XML_DATE) if row.EffectDt else "",
            expiry=row.ExpiryDt.strftime(XML_DATE) if row.ExpiryDt else FOREVER,
            roles=_xml_escape(row.RoleIds),
        ))
    return elements


def _dealer_setup(conn: pyodbc.Connection, dealer_id: str) -> tuple[str, ...]:
    """Current dealer setup flags, so the save does not overwrite them."""
    with conn.cursor() as cursor:
        cursor.execute(DEALER_SETUP_SQL, [dealer_id])
        row = cursor.fetchone()
    if row is None:
        return DEALER_SETUP_DEFAULTS
    return tuple((_clean(value) or default)
                 for value, default in zip(row, DEALER_SETUP_DEFAULTS))


def _enrol_dealer(
    conn: pyodbc.Connection, program_id: str, province: str, oem: str
) -> tuple[str | None, str | None]:
    """Enrol an active dealer in the program. Returns (dealer_id, error)."""
    with conn.cursor() as cursor:
        cursor.execute(PROGRAM_RANGE_SQL, [program_id, province])
        span = cursor.fetchone()
    if span is None or span.EffectDt is None:
        return None, f"no {program_id} program detail for {province}"
    effect = span.EffectDt.strftime(XML_DATE)
    expiry = span.ExpiryDt.strftime(XML_DATE)

    with conn.cursor() as cursor:
        cursor.execute(CANDIDATE_DEALER_SQL, [province, oem])
        candidates = [_clean(row[0]) for row in cursor.fetchall()]
    if not candidates:
        return None, f"no active {oem} dealer in {province}"

    last_error = ""
    for dealer_id in candidates:
        existing = _existing_programs(conn, dealer_id)
        new_program = ENROL_PROGRAM_XML.format(
            program_id=program_id, portal="Y", admin="Y", ext="", cb_rule="CB001",
            address_type="Pr", effect=effect, expiry=expiry, roles="",
        )
        xml = f"<Prgms>{''.join(existing)}{new_program}</Prgms>"
        setup = _dealer_setup(conn, dealer_id)

        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    ENROL_DEALER_SQL,
                    [xml, dealer_id, CONTRACT_GROUP, *setup, USER_ID],
                )
                sets = _fetch_all_sets(cursor)
        except pyodbc.OperationalError:
            raise
        except pyodbc.Error as exc:
            last_error = f"{dealer_id}: {exc.args[-1]}"
            continue

        if _find_enrolled_dealer(conn, program_id, province, oem) is None:
            last_error = f"{dealer_id}: {_messages(sets)}"
            continue

        # The save deletes programs missing from the XML; verify none were lost.
        remaining = len(_existing_programs(conn, dealer_id))
        if remaining < len(existing) + 1:
            return None, (f"dealer {dealer_id} lost programs during enrolment "
                          f"({len(existing)} before, {remaining} after) - aborting")
        return dealer_id, None

    return None, f"no dealer could be enrolled ({len(candidates)} tried) - {last_error}"


def _create_contract(
    conn: pyodbc.Connection, case: TestCase, dealer_id: str, province: str,
    contract_type: str, today: str, year: int,
) -> tuple[str | None, int | None, str | None]:
    """Create a quote then a contract. Returns (contract_num, quote_key, error)."""
    make, model = VEHICLE[case.oem]
    xml = QUOTE_XML.format(
        dealer_id=dealer_id, today=today, contract_group=CONTRACT_GROUP,
        contract_type=contract_type, program_id=case.program_id,
        make=make, model=model, year=year, prov_state=province,
    )

    try:
        with conn.cursor() as cursor:
            cursor.execute(SAVE_QUOTE_SQL, [xml, USER_ID])
            sets = _fetch_all_sets(cursor)
    except pyodbc.OperationalError:
        raise
    except pyodbc.Error as exc:
        return None, None, f"quote save failed: {exc.args[-1]}"

    row = sets[-1][-1] if sets else None
    if row is None or row[0] is None or int(row[0]) == 0:
        return None, None, f"quote save returned no quote key: {_messages(sets[:-1])}"
    quote_key = int(row[0])

    try:
        with conn.cursor() as cursor:
            cursor.execute(QUOTE_TO_CONTRACT_SQL, [quote_key, USER_ID])
            sets = _fetch_all_sets(cursor)
    except pyodbc.OperationalError:
        raise
    except pyodbc.Error as exc:
        return None, quote_key, f"contract create failed: {exc.args[-1]}"

    row = sets[-1][-1] if sets else None
    if row is None or row[1] is None:
        return None, quote_key, (
            f"contract create returned no contract number: {_messages(sets[:-1])}")
    return str(row[1]), quote_key, None


def run_stage3(conn: pyodbc.Connection) -> StageResult:
    """Create a real contract per program/province and check its prefix."""
    print("Stage 3 - contract creation regression test")
    result = StageResult(name="Stage 3 - contract creation regression test")

    now = datetime.now()
    today = now.strftime("%d-%b-%Y")          # quote XML uses the app's date format
    year = now.year
    failures = FailureLog()
    skipped: list[str] = []
    enrolled: list[str] = []
    records: list[list[str]] = []
    created = 0

    for case in TEST_CASES:
        combos = _program_availability(conn, case.program_id)
        if not combos:
            note = f"{case.program_id}: not effective today - skipped"
            print(f"    SKIP {note}")
            skipped.append(note)
            continue

        for province, contract_type in combos:
            label = f"{case.program_id}/{case.oem}/{province}/{contract_type}"

            dealer_id = _find_enrolled_dealer(conn, case.program_id, province, case.oem)
            if dealer_id is None:
                dealer_id, error = _enrol_dealer(
                    conn, case.program_id, province, case.oem)
                if dealer_id is None:
                    print(f"    SKIP {label} {error}")
                    skipped.append(f"{label} {error}")
                    continue
                enrolled.append(f"{label} dealer {dealer_id}")
                print(f"    enrolled dealer {dealer_id} for {label}")

            number, quote_key, error = _create_contract(
                conn, case, dealer_id, province, contract_type, today, year)
            if error is not None:
                failures.add(f"{label}/{dealer_id} {error}")
                continue

            created += 1
            reason = _check_contract_number(number, case.prefix)
            records.append([
                case.program_id, case.oem, province, contract_type, dealer_id,
                str(quote_key), (number or "-").strip(),
                "PASS" if reason is None else "FAIL",
            ])
            if reason is not None:
                failures.add(f"{label}/{dealer_id} quote={quote_key} {reason}")
            else:
                print(f"    OK   {label}/{dealer_id} -> {number}")

    result.total = created + failures.count
    result.failed = failures.count
    result.passed = result.total - failures.count
    result.skipped = len(skipped)
    result.failures = failures.messages
    result.record_headers = [
        "Program", "OEM", "Prov", "Type", "Dealer", "Quote", "Contract number", "Result",
    ]
    result.records = records
    result.notes.append(f"Contracts created: {created}")
    if enrolled:
        result.notes.append(f"Dealers enrolled for this run: {len(enrolled)}")
    for line in skipped[:MAX_PDF_FAILURES]:
        result.notes.append(f"Skipped: {line}")

    print(
        f"  total={result.total} passed={result.passed} failed={failures.count} "
        f"skipped={len(skipped)}"
    )
    return result


# --------------------------------------------------------------------------- #
# PDF report
# --------------------------------------------------------------------------- #

_GRID = TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8E8E8")),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
])


def _label_value_table(rows: list[tuple[str, str]]) -> Table:
    table = Table([[k, v] for k, v in rows], colWidths=[1.6 * inch, 5.2 * inch])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#E8E8E8")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return table


def write_pdf_report(
    cfg: EnvConfig,
    results: list[StageResult],
    started: datetime,
    finished: datetime,
    overall_ok: bool,
) -> Path:
    """Render the run results to a timestamped PDF and return its path."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / (
        f"ContractPrefixTest-{cfg.env}-{started.strftime('%Y%m%d-%H%M%S')}.pdf"
    )

    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    mono = ParagraphStyle(
        "Mono", parent=body, fontName="Courier", fontSize=7.5, leading=9.5,
    )
    verdict_style = ParagraphStyle(
        "Verdict", parent=styles["Heading2"],
        textColor=colors.HexColor("#1A7F37") if overall_ok else colors.HexColor("#B42318"),
    )

    story: list = [
        Paragraph("ContractPrefixTest — Test Report", styles["Title"]),
        Paragraph("PASSED" if overall_ok else "FAILED", verdict_style),
        Spacer(1, 6),
        _label_value_table([
            ("Environment", cfg.env),
            ("Server", cfg.server),
            ("Database", cfg.database),
            ("Started", started.strftime("%Y-%m-%d %H:%M:%S")),
            ("Finished", finished.strftime("%Y-%m-%d %H:%M:%S")),
            ("Duration", f"{(finished - started).total_seconds():.1f} s"),
        ]),
        Spacer(1, 14),
        Paragraph("Summary", styles["Heading2"]),
    ]

    summary = [["Stage", "Total", "Passed", "Failed", "Skipped", "Result"]]
    for res in results:
        if res.executed:
            summary.append([
                res.name, str(res.total), str(res.passed), str(res.failed),
                str(res.skipped), "PASS" if res.ok else "FAIL",
            ])
        else:
            summary.append([res.name, "-", "-", "-", "-", "NOT RUN"])
    table = Table(summary, colWidths=[3.0 * inch, 0.7 * inch, 0.7 * inch, 0.7 * inch,
                                      0.7 * inch, 0.8 * inch])
    table.setStyle(_GRID)
    story.extend([table, Spacer(1, 14)])

    for res in results:
        story.append(Paragraph(res.name, styles["Heading2"]))
        if not res.executed:
            story.append(Paragraph("Not run - a previous stage failed.", body))
            story.append(Spacer(1, 12))
            continue
        for note in res.notes:
            story.append(Paragraph(note, body))
        if res.failures:
            story.append(Spacer(1, 4))
            story.append(Paragraph(
                f"Failures (showing {len(res.failures)} of {res.failed}):", body))
            for line in res.failures:
                story.append(Paragraph(line.replace("&", "&amp;").replace("<", "&lt;"), mono))
        else:
            story.append(Paragraph("No failures.", body))

        if res.records:
            story.append(Spacer(1, 8))
            story.append(Paragraph(f"Contracts created ({len(res.records)}):", body))
            story.append(Spacer(1, 4))
            record_table = Table(
                [res.record_headers] + res.records,
                colWidths=[0.75 * inch, 0.5 * inch, 0.5 * inch, 0.5 * inch,
                           0.95 * inch, 0.8 * inch, 1.4 * inch, 0.6 * inch],
                repeatRows=1,
            )
            record_table.setStyle(_GRID)
            story.append(record_table)

        story.append(Spacer(1, 12))

    story.append(PageBreak())
    story.append(Paragraph("Appendix — Test cases", styles["Heading2"]))
    story.append(Paragraph(
        f"Each program is tested against one active dealer per province for its OEM, "
        f"all {len(CONTRACT_TYPES)} contract types, and one lienholder sampled from its "
        f"group.", body))
    story.append(Spacer(1, 6))
    case_rows = [["ProgramId", "OEM", "Lienholders", "Expected prefix"]]
    for case in TEST_CASES:
        case_rows.append([case.program_id, case.oem, ",".join(case.lienholders), case.prefix])
    case_table = Table(case_rows, colWidths=[1.2 * inch, 0.8 * inch, 1.6 * inch, 1.4 * inch])
    case_table.setStyle(_GRID)
    story.append(case_table)

    SimpleDocTemplate(
        str(path), pagesize=letter,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title=f"ContractPrefixTest {cfg.env}",
    ).build(story)
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ContractPrefixTest",
        description="Verify contract-number prefix configuration in FNCT or ACCP.",
        prefix_chars="-/",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("/F", dest="env", action="store_const", const="fnct",
                       help="Run against the FNCT environment.")
    group.add_argument("/A", dest="env", action="store_const", const="accp",
                       help="Run against the ACCP environment.")
    parser.add_argument(
        "--stage", dest="stages", default="all",
        help="Stages to run: 'all' (default) or a comma-separated list, e.g. 3 or 1,3.",
    )
    return parser


def parse_stages(value: str) -> set[int]:
    """Parse the --stage value into a set of stage numbers."""
    if value.strip().lower() == "all":
        return {1, 2, 3}
    stages = set()
    for piece in value.split(","):
        piece = piece.strip()
        if piece not in {"1", "2", "3"}:
            raise ValueError(f"invalid stage '{piece}' (expected 1, 2, 3 or all)")
        stages.add(int(piece))
    if not stages:
        raise ValueError("no stages selected")
    return stages


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 for usage errors and 0 for --help; keep those codes.
        return int(exc.code or EXIT_OK)

    try:
        selected = parse_stages(args.stages)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        cfg = load_config(args.env)
    except EnvironmentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT

    print(f"Environment: {ENVIRONMENTS[args.env]}  server={cfg.server}  db={cfg.database}")
    if selected != {1, 2, 3}:
        print(f"Stages selected: {','.join(str(s) for s in sorted(selected))}")

    try:
        conn = connect(cfg)
    except pyodbc.Error as exc:
        print(f"error: cannot connect to {cfg.server}: {exc.args[-1]}", file=sys.stderr)
        return EXIT_ENVIRONMENT

    stages = [
        (1, "Stage 1 - cfProductPrfx data validation", run_stage1),
        (2, "Stage 2 - DPP_GenContractNumber unit test", run_stage2),
        (3, "Stage 3 - contract creation regression test", run_stage3),
    ]

    started = datetime.now()
    results: list[StageResult] = []
    stopped = False
    try:
        for number, name, runner in stages:
            if number not in selected:
                continue
            if stopped:
                results.append(StageResult(name=name, executed=False))
                continue
            result = runner(conn)
            results.append(result)
            if not result.ok:
                print(f"Stage {number} failed - stopping.", file=sys.stderr)
                stopped = True
    except pyodbc.OperationalError as exc:
        print(f"error: database connection lost: {exc.args[-1]}", file=sys.stderr)
        return EXIT_ENVIRONMENT
    except pyodbc.Error as exc:
        print(f"error: database error: {exc.args[-1]}", file=sys.stderr)
        return EXIT_ENVIRONMENT
    finally:
        conn.close()

    executed = [r for r in results if r.executed]
    overall_ok = all(r.ok for r in executed) and len(executed) == len(results)

    report = write_pdf_report(cfg, results, started, datetime.now(), overall_ok)
    print(f"Report: {report}")

    if not overall_ok:
        return EXIT_TEST_FAILED
    print("All selected stages passed.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
