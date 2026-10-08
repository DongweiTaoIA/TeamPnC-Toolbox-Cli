# ContractPrefixTest — Requirements

**Status:** Implemented; verified against FNCT on 2026-09-21
**Owner:** _TBD_
**Component:** `cli/ContractPrefixTest`

---

## 1. Purpose

Rows were recently inserted into `cfProductPrfx` to define which **contract-number prefix** is used when a new
contract number is generated. `ContractPrefixTest` is a command-line tool that verifies that configuration end to
end in a non-production environment.

Given a business-supplied test-case set (ProgramId / OEM / Lienholder → expected Prefix), the tool confirms that:

1. the prefix data was loaded correctly into `cfProductPrfx`,
2. `dbo.DPP_GenContractNumber` returns the expected prefix for every relevant combination of dealer, contract type
   and lienholder, and
3. a **real contract**, created through the same stored procedures the application uses, carries the expected
   prefix.

The goal is to replace a manual, error-prone verification pass with a repeatable, scriptable check.

## 2. Scope

### In scope

- Validation of `cfProductPrfx` data against the test-case set. (Stage 1)
- Unit testing of `dbo.DPP_GenContractNumber` prefix output across the expanded case set. (Stage 2)
- Regression testing: create a real quote and contract and verify the contract number prefix. (Stage 3)
- Automatic discovery of test dealers from `dmDealer`, including enrolling a dealer in a program when needed.
- Console and PDF reporting of results.

The test-case set is **embedded in the tool** (§5). No file or ticket input.

### Out of scope

- Production environments.
- Loading / seeding the `cfProductPrfx` data itself — the tool verifies, it does not configure.
- Any assertion on the numeric portion of the contract number beyond format (length / digits).
- Cleaning up the quotes, contracts and dealer enrolments that Stage 3 creates (see §6.5).

## 3. Command-line interface

```text
ContractPrefixTest (/F | /A) [--stage <all|n[,n...]>]
```

| Option | Required | Description |
| --- | --- | --- |
| `/F` | One of `/F`/`/A` | Run against the **FNCT** environment. |
| `/A` | One of `/F`/`/A` | Run against the **ACCP** environment. |
| `--stage` | No | Stages to run. `all` (default), or a comma-separated list such as `3` or `1,3`. Useful because Stage 2 takes several minutes. |

### Rules

- Exactly one of `/F` or `/A` must be supplied.
- Selected stages still run in order and still stop on the first failure.
- Invalid or conflicting arguments cause the tool to print usage and exit with code `2`.

## 4. Environments

| Environment | Switch | Database server |
| --- | --- | --- |
| FNCT | `/F` | `sy-ds-dealerservices-dp-unifi.dev.sgbd.ia.iafg.net` |
| ACCP | `/A` | `sql_van_test_accp.iap.iafg.net,49826` |

Connection details (database name, credentials / integrated auth) must be supplied via configuration, **not**
hard-coded in the tool. Secrets must never be committed to the repository.

## 5. Test cases

### 5.1 Test-case set (supplied by Business)

Embedded in the tool — there is no file or ticket input:

| ProgramId | OEM | Lienholder | Prefix |
| --- | --- | --- | --- |
| AU242 | HO | NA,FD,IF | HCPM |
| AU243 | AC | NA,FD,IF | ACPM |
| AU248 | HO | NA,FD,IF | HCPM |
| AU249 | HO | NA,FD,IF | HCPM |
| AU250 | AC | NA,FD,IF | ACPM |
| AU240 | HO | NA,FD,IF | HCPR |
| AU241 | AC | NA,FD,IF | ACPR |
| AU251 | HO | NA,FD,IF | HCPR |
| AU252 | AC | NA,FD,IF | ACPR |
| AU244 | HO | NA,FD,IF | HCOP |
| AU245 | AC | NA,FD,IF | ACOP |
| AU246 | HO | NA,FD,IF | HCAL |
| AU247 | AC | NA,FD,IF | ACAL |

| Column | Description | `cfProductPrfx` column |
| --- | --- | --- |
| `ProgramId` | Program identifier, max 5 chars. Also passed as `@psProgramId`. | `ProgramId` |
| `OEM` | OEM code (`HO` or `AC`). Also selects which dealers the row applies to. | `OEM` |
| `Lienholder` | Comma-separated two-character lienholder codes. | `LienHolderGroups` |
| `Prefix` | Expected four-character contract-number prefix. | `PrfxContractNum` |

### 5.2 Dealer resolution (Stage 2)

`DPP_GenContractNumber` takes a `@psDealerId`, which is not part of the test-case set. Stage 2 resolves dealers
itself so that every province is covered for each OEM. _(Stage 3 selects dealers differently — see §6.4, Step 2.)_

```sql
SELECT DealerId, ProvState, OEM
FROM   dmDealer
WHERE  OEM IN ('HO', 'AC')
  AND  DealerStat = 'A'
```

- Group active dealers by `OEM` + province.
- For each test case, select **one dealer per province** whose `OEM` matches the test case's `OEM`.
- Selection is deterministic (lowest `DealerId` per OEM + province) so runs are repeatable.
- If an OEM has no active dealer in a province, that province is **skipped and reported as "not covered"** — it is
  not a failure.

Measured against FNCT on 2026-09-21: 20 of the 26 OEM/province combinations have an active dealer. `HO` has none
in NT or NU; `AC` has none in NT, NU, PE or YT.

### 5.3 Expansion rules

Stage 2 expands each test-case row to:

```text
(dealers for that OEM, one per province)  ×  (all contract types, §5.4)  ×  (one lienholder, sampled)
```

The three lienholders in a group all map to the same prefix, so testing all three adds no coverage. Stage 2 picks
**one at random** from the list for each case; the chosen code is shown in any failure message.

With all 13 provinces covered this would be 13 programs × 13 dealers × 21 contract types = **3,549** cases. With
FNCT's actual dealer coverage (§5.2) it is **2,751** cases: 693 per `HO` program (11 provinces) and 567 per `AC`
program (9 provinces).

Stage 1 expands differently — no dealer, and the lienholder list stays whole — giving 273 rows per program. See
Stage 1 in §6.

### 5.4 Reference data

**Provinces (13)**

`AB`, `BC`, `MB`, `NB`, `NL`, `NS`, `NT`, `NU`, `ON`, `PE`, `QC`, `SK`, `YT`

**Contract types (21)**

`AT`, `AU`, `GC`, `HT`, `MA`, `MF`, `MN`, `MS`, `NT`, `PM`, `PU`, `PW`, `RV`, `SC`, `SL`, `SM`, `SS`, `SU`, `TT`,
`UT`, `W5`

## 6. Test stages

The three stages run in order. **If a stage fails, the run stops and the tool exits with a non-zero exit code** —
later stages are not executed.

### Stage 1 — Data validation

Verify that the rows inserted into `cfProductPrfx` match the test-case set.

```sql
CREATE TABLE [dbo].[cfProductPrfx] (
      [ProductPrfxKey]   INT IDENTITY(1,1)
    , [ProductId]        CHAR(2)       NOT NULL
    , [ProgramId]        VARCHAR(5)    NULL
    , [GPProgramId]      VARCHAR(5)
    , [CompanyId]        VARCHAR(4)    NOT NULL DEFAULT 'IAPG'
    , [ProvState]        CHAR(2)       NOT NULL
    , [ContractType]     VARCHAR(2)    NOT NULL
    , [OEM]              VARCHAR(2)    NOT NULL DEFAULT 'NA'
    , [LienHolderGroups] VARCHAR(100)  NOT NULL DEFAULT 'NA'
    , [PrfxContractNum]  VARCHAR(4)    NOT NULL
    , [SrfxContractNum]  VARCHAR(1)    NOT NULL DEFAULT ''
    , [ModDtTime]        DATETIME      NOT NULL
    , [ModLoginId]       VARCHAR(12)   NOT NULL
    , CONSTRAINT [cfProductPrfx_PK] PRIMARY KEY CLUSTERED ([ProductPrfxKey] ASC)
);
```

**Lookup keys** — rows are fetched with `ProductId = 'EW'` and `ProgramId IN (test-case programs)`, then matched
on:

| Column | Value |
| --- | --- |
| `ProgramId` | Test case `ProgramId` |
| `OEM` | Test case `OEM` |
| `ProvState` | Each province (§5.4) |
| `ContractType` | Each contract type (§5.4) |

`CompanyId` and `GPProgramId` are **not** used as lookup keys — ignore them.

**Assertions** on the matched row:

- `PrfxContractNum` equals the expected `Prefix`.
- `LienHolderGroups` equals the test case's `Lienholder` list. The column holds the whole comma-separated list in
  a **single row** (`'NA,FD,IF'`), and is compared as a set of codes so ordering or spacing differences do not
  cause false failures.
- A key that appears twice is reported as a duplicate row.

Because `ProvState` and `ContractType` are `NOT NULL`, each program needs one row per province per contract type:
**13 × 21 = 273 rows per program**, **3,549 rows** across the 13 programs.

- Report missing rows, prefix mismatches, lienholder-group mismatches, and extra rows for the programs under test.
- On any failure → stop the run.

### Stage 2 — Unit test of `DPP_GenContractNumber`

The application generates contract numbers through:

```sql
CREATE PROCEDURE [dbo].[DPP_GenContractNumber]
      @psProductId      CHAR(2)
    , @psContractType   VARCHAR(2)
    , @psDealerId       VARCHAR(8)
    , @psLienHolderId   VARCHAR(2)
    , @psProgramId      VARCHAR(5) = NULL
    , @psGPProgramId    VARCHAR(5) = NULL
    , @psNum            CHAR(12) OUTPUT
    , @psLang           CHAR(1)  = 'E'
    , @psUserId         VARCHAR(12)
```

Example from the business notes, program `AU250`, dealer `BC000021` (OEM `AC`):

```sql
DECLARE @lsNum CHAR(12);
EXEC DPP_GenContractNumber 'EW', 'AU', 'BC000021', 'NA', 'AU250', NULL, @lsNum OUT, 'E', 'SXR';
PRINT @lsNum;   -- expected: 'ACPM' + 8 digits
```

#### 6.1 Parameter mapping

The tool generates the unit-test list automatically from the expanded test cases (§5.3).

| Parameter | Source |
| --- | --- |
| `@psProductId` | Fixed `'EW'` — all programs under test are EW. |
| `@psContractType` | Each contract type from §5.4. |
| `@psDealerId` | Dealer resolved per §5.2. |
| `@psLienHolderId` | One lienholder sampled at random from the test case's `Lienholder` list. |
| `@psProgramId` | Test case `ProgramId`. |
| `@psGPProgramId` | `NULL`. |
| `@psLang` | `'E'`. |
| `@psUserId` | Fixed `'SXR'`. |

#### 6.2 Assertions

For each expanded case, execute the procedure and assert:

- `@psNum` is exactly 12 characters,
- characters 1–4 equal the expected `Prefix`,
- characters 5–12 are all digits.

`SrfxContractNum` (the one-character suffix on `cfProductPrfx`) is not exercised by these tests.

#### 6.3 Side effects

The procedure consumes contract-number sequence values. This is accepted — no transaction or rollback is required
in FNCT/ACCP.

On any failure → stop the run.

### Stage 3 — Regression test: create a real contract

Stages 1 and 2 verify configuration and the number generator in isolation. Stage 3 proves the prefix end to end by
creating a **real contract** through the same stored procedures the application uses, then checking its contract
number.

In a full run Stage 3 executes only if Stages 1 and 2 pass. It can also be run on its own with `--stage 3`.

#### 6.4 Per-program workflow

For each test case (program) in §5.1:

**Step 1 — Is the program available?**

```sql
SELECT ProvState, ContractType
FROM   cfProgramDetl
WHERE  ProgramId = ?
  AND  GETDATE() BETWEEN EffectDt AND ExpiryDt
```

Each returned row is one province / contract-type combination to test. Example — `AU240` returns `AU` for AB, BC,
MB, NB, NL, NS, ON, PE and SK. **If the result is empty, log it and skip the program** — this is not a failure.

**Step 2 — Find or set up a dealer that sells the program**

```sql
SELECT p.DealerId
FROM   EW_cfDealerProgram p
JOIN   dmDealer d ON p.DealerId = d.DealerId
WHERE  p.ProgramId = ?
  AND  d.ProvState = ?
  AND  d.DealerStat = 'A'
  AND  d.OEM = ?            -- the test case's OEM
```

If a dealer is found, use it. If not, pick an active dealer for that OEM and province from `dmDealer` and enrol it.

> **Critical:** `DPP_DP153EWDealerProgram_Save` **deletes** every program the dealer holds in the same contract
> group that is *not* present in `@psProgramListXml`, along with that program's markups, funds and service roles:
>
> ```sql
> WHERE dlrPrm.DealerId = @psDealerId
>   AND dlrPrm.ProgramId NOT IN (SELECT ProgramId FROM @tblDealerProgram)
> ```
>
> Passing only the new program would therefore wipe the dealer's other `EAU` programs. The tool must first read
> the dealer's existing programs (and their service-role ids) and resubmit them unchanged alongside the new one.
> The same applies to the dealer-setup flags (`@psPremiumAdjustType`, `@psCancNotifYN`, ...), which are read from
> `EW_cfDealerSetup` and passed back rather than hard-coded.

```sql
-- existing programs, rebuilt into the XML unchanged
SELECT p.ProgramId, p.DlrPortalYN, p.AdminDBYN, p.ExtDealerId, p.CBRuleId,
       p.AddressType, p.EffectDt, p.ExpiryDt,
       STUFF((SELECT ';' + CAST(r.DealerRoleID AS VARCHAR(10))
              FROM   EW_cfDealerServiceRoles r
              WHERE  r.DealerProgramID = p.DealerProgramKey
              FOR XML PATH('')), 1, 1, '') AS RoleIds
FROM   EW_cfDealerProgram p
JOIN   cfProgram g ON g.ProgramId = p.ProgramId AND g.ContractGroup = 'EAU'
WHERE  p.DealerId = ?
```

The program list is then `<Prgms>{existing programs}{new program}</Prgms>`. The new program element uses
`PT=Y`, `AD=Y`, `CB=CB001`, `BA=Pr`, and takes its **date range from the program itself**:

```sql
SELECT MIN(EffectDt) AS EffectDt, MAX(ExpiryDt) AS ExpiryDt
FROM   cfProgramDetl
WHERE  ProgramId = ? AND ProvState = ?
```

> The procedure rejects any dealer program whose range is not *inside* the program's range for the dealer's
> province, so `EP` must not be `9999-12-31` unless the program itself runs that long:
>
> ```sql
> AND (NOT prm.EffectDt BETWEEN dtl.EffectDt AND dtl.ExpiryDt OR
>      NOT prm.ExpiryDt  BETWEEN dtl.EffectDt AND dtl.ExpiryDt)
> ```

```sql
EXEC DPP_DP153EWDealerProgram_Save
      @psDealerId            = '{DealerId}'
    , @psContractGroup       = 'EAU'
    , @psPremiumAdjustType   = '{existing}'
    , @psCancNotifYN         = '{existing}'
    , @psHoldInvoiceYN       = '{existing}'
    , @psNetRemittanceYN     = '{existing}'
    , @psHoldDealerCompYN    = '{existing}'
    , @psAllowPrintRatesYN   = '{existing}'
    , @psProgramListXml      = @p9
    , @psLang                = 'E'
    , @psUserId              = 'SXR';
```

After the save the tool re-runs the Step 2 query to confirm the enrolment took, **and** re-counts the dealer's
programs in the contract group. If the count did not grow by one, programs were lost: the run aborts that
combination rather than continuing.

The validation above also applies to the dealer's *existing* programs, and some dealers hold rows whose stored
dates fall outside their program's range for that province — pre-existing data that has nothing to do with this
test. Such a dealer can never be saved, so the tool tries up to **10 candidate dealers** per province and uses the
first that enrols cleanly. If none succeed, or no dealer exists for that OEM and province, the combination is
recorded as **not covered**.

**Step 3 — Create a quote**

`DPP_DP608Quote_Save` takes the quote as XML. The tool substitutes the dealer, program, contract type, province,
dates and the vehicle make/model; all other values are fixed sample data.

| Placeholder | Source |
| --- | --- |
| `<D>` | Dealer id from Step 2 |
| `<PG>` | `{ProgramId}\|\|S` |
| `<CT>` | Contract type from Step 1 |
| `<PS>` | Province from Step 1 |
| `<QDT>` `<DDT>` `<EDT>` `<WDT>` | Today |
| `<MK>` `<MD>` | `HONDA` / `CIVIC` when OEM is `HO`; `ACURA` / `MDX` when OEM is `AC` |

```sql
EXEC DPP_DP608Quote_Save
      @psQuoteXml  = @p1
    , @piQuoteKey  = @p2 OUTPUT   -- 0 for a new quote
    , @psQuoteNum  = @p3 OUTPUT
    , @psLang      = 'E'
    , @psUserId    = 'SXR';
```

On success the procedure returns a new **quote key**.

> **Reading the output:** this procedure (and `DPP_DP608Quote_Contract`) emits validation/warning result sets
> *before* the output values — for example "WARNING: User is responsible for ensuring the accuracy of the
> information ...". The caller must walk to the **last** result set to read the key and number, and should surface
> the earlier sets as diagnostics when the output is missing.

**Step 4 — Convert the quote to a contract**

```sql
EXEC DPP_DP608Quote_Contract
      @piQuoteKey     = {quoteKey}
    , @piQuoteSeqNum  = 1
    , @piContractKey  = @p3 OUTPUT
    , @psContractNum  = @p4 OUTPUT
    , @psLang         = 'E'
    , @psUserId       = 'SXR';
```

**Step 5 — Assert**

`@psContractNum` must start with the test case's expected `Prefix`, followed by 8 digits (12 characters total).

#### 6.5 Side effects

Stage 3 writes real data to the target environment and **nothing is cleaned up**:

- dealer program enrolments (`EW_cfDealerProgram`) for dealers that were not already selling the program,
- quotes,
- contracts, and the contract-number sequence values they consume.

For this reason Stage 3 must never be pointed at production, and the team should expect test contracts to
accumulate in FNCT / ACCP.

#### 6.6 Volume

One contract per program per available province / contract-type combination. Measured against FNCT on
2026-09-21: **41 contracts created, 4 combinations not covered**. All 13 programs are currently effective, and
every one is restricted to contract type `AU` only — so the 21 contract types of Stages 1 and 2 collapse to a
single type here.

The 4 uncovered combinations are `AU241`/`AU243` in PE (no active Acura dealer) and in SK (only two active Acura
dealers, both carrying the pre-existing date problem described in Step 2).

## 7. Reporting and exit codes

- Progress is written to the console as each stage runs.
- A summary per stage reports total / passed / failed / skipped counts.
- Failures list the full case detail (ProgramId, OEM, Province, DealerId, ContractType, Lienholder), the expected
  prefix and the actual value. The console prints the first **25** failures per stage and the PDF carries up to
  **200**; the true count is always shown in the summary.
- Provinces with no eligible dealer are listed separately as "not covered".
- Every run writes a **PDF report** to `reports/ContractPrefixTest-<ENV>-<timestamp>.pdf`, and prints its path.
  It contains:
  - a PASSED / FAILED verdict, environment, server, database, start/finish times and duration,
  - the per-stage summary table, with **NOT RUN** for a stage blocked by an earlier failure,
  - per-stage notes and failure detail,
  - for Stage 3, a table of every contract created: program, OEM, province, contract type, dealer, quote key,
    contract number and result,
  - an appendix listing the test-case set.
- `reports/` is git-ignored.

| Exit code | Meaning |
| --- | --- |
| `0` | All selected stages passed. |
| `1` | One or more test cases failed. |
| `2` | Invalid arguments or usage error. |
| `3` | Environment error (database unreachable, configuration missing). |

## 8. Non-functional requirements

- **Implementation:** Python 3, run from the command line. No GUI and no installer — a single script.
  External dependencies: `pyodbc`, `python-dotenv`, and `reportlab` for the PDF report.
- **Authentication:** Windows Integrated (`Trusted_Connection=yes`). The tool handles no passwords.
- Connection settings are read from a git-ignored `.env` beside the script (`<ENV>_SERVER`, `<ENV>_DB`,
  `<ENV>_TRUST_SERVER_CERT`, `ODBC_DRIVER`); `.env.example` is the template. No credentials in source control.
- The tool must never be pointed at a production environment.
- Stage 2 issues roughly **2,750** procedure calls and takes a few minutes; `--stage` exists so the other stages
  can be exercised without waiting for it.
- Stages 1 and 2 are re-runnable and produce the same pass/fail outcome on repeat runs. (Stage 2 varies which
  lienholder it samples, but not the pass/fail result.) Stage 3 creates new data on every run.

## 9. Files

| File | Purpose |
| --- | --- |
| `contract_prefix_test.py` | The tool. |
| `.env.example` | Connection settings template; copy to `.env`. |
| `Requirements.md` | This document. |
| `README.md` | Setup and usage. |
| `reports/` | Generated PDF reports (git-ignored). |

```powershell
pip install pyodbc python-dotenv reportlab
copy .env.example .env
python contract_prefix_test.py /F              # all stages, FNCT
python contract_prefix_test.py /F --stage 3    # stage 3 only
```

## 10. Open questions

1. Are the fixed quote values (`<CG>EAU`, the coverage block, term, premium, tax rates) valid for every province
   and contract type? They have been proven for contract type `AU` in AB, BC, MB, NB, NL, NS, ON, QC and SK; other
   combinations are untested because no currently effective program uses them.
2. Should Stage 3 spread contracts across dealers instead of always reusing the first eligible one?
3. Two Saskatchewan Acura dealers hold `EW_cfDealerProgram` rows whose dates fall outside their program's range
   for SK, which makes them unsaveable through DP153. Is that pre-existing data worth correcting? It blocks
   `AU241` / `AU243` coverage in SK and would also block normal dealer maintenance.

### Resolved

- **Does `@psProgramListXml` replace or merge?** It **replaces** within the contract group — confirmed by reading
  the procedure body (§6.4, Step 2). The tool preserves existing programs and verifies none were lost.
- **What does `@piQuoteKey` take for a new quote?** `0`. Confirmed by 41 successful contract creations.
- **Does the prefix depend on the lienholder?** No — all codes in a group share one `cfProductPrfx` row, so
  Stage 2 samples one (§5.3).