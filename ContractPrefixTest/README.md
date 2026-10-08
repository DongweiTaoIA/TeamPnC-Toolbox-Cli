# ContractPrefixTest

Verifies contract-number **prefix** configuration in FNCT / ACCP. See
[Requirements.md](Requirements.md) for the full spec.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install pyodbc python-dotenv reportlab
copy .env.example .env
```

Authentication is Windows Integrated — no passwords are stored or prompted for.

## Usage

```powershell
python contract_prefix_test.py /F   # FNCT
python contract_prefix_test.py /A   # ACCP

python contract_prefix_test.py /F --stage 3     # only stage 3
python contract_prefix_test.py /F --stage 1,3   # skip the slow stage 2
```

## Stages

| Stage | What it does |
| --- | --- |
| 1 | Compares `cfProductPrfx` rows (3,549 expected) against the business test-case set. |
| 2 | Calls `dbo.DPP_GenContractNumber` for ~2,750 expanded cases and checks the prefix. |
| 3 | Creates a real quote and contract per program/province and checks the contract number prefix. |

Each stage runs only if the previous one passed.

## Side effects

Stage 2 consumes contract-number sequence values. Stage 3 goes further and writes
real data that is **not cleaned up**: dealer program enrolments, quotes and
contracts. Never point this tool at production.

## Report

Every run writes a PDF to `reports/ContractPrefixTest-<ENV>-<timestamp>.pdf` with the
overall verdict, per-stage summary, failure detail and the test-case appendix. The
path is printed at the end of the run. `reports/` is git-ignored.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | All stages passed. |
| 1 | One or more test cases failed. |
| 2 | Invalid arguments or usage error. |
| 3 | Environment error (database unreachable, configuration missing). |
