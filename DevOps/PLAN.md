# Azure DevOps Ticket Analysis - First Try

Keep it small. Prove the idea on 2026 data, then improve.

## Status (2026-09-30)

**All of 2026 (Sprints 104-123) collected and analysed**: see
[reports/2026-full-year.md](reports/2026-full-year.md).
9,399 work items, **909 real tickets** (695 Requirement, 191 Bug, 23 Feature), 93% Closed,
~58% traceable to code or SQL scripts.
Biggest finding: the DW2 (501-572) -> DW3/Honda (601-672) accounting model rollout repeats the
same numbered sequence **54 steps out of 54, with no gaps** - a complete, ready-made template
for the next OEM rollout (`src/template.py`).

**Code access is now working.** The PAT was reissued with Code: Read, so all **1,503 changesets,
commits and PRs (7,458 file changes)** are downloaded with zero fetch errors.

Dashboard data ships as `data/dashboard.json` via `src/report.py` - see
[DASHBOARD.md](DASHBOARD.md). This tool does analysis only; rendering is a separate web app.

Still open: no Azure OpenAI, so labelling uses regex rules instead of AI, and `topics.yaml`
has not been reviewed yet.

## Questions the first try answers

1. How many tickets did Team PnC have in Sprints 104-123, by type and month?
2. What topics did we work on?
3. Are tickets linked to changesets or scripts well enough to explain *how* the work was done?
4. For one recurring topic, can we produce a useful "how we do it" dossier and draft checklist?

Later (not now): dashboard, knowledge search, 3-5 years of history.

## Setup

- Org `SOPCV-SNW`, project `DP_Unifi_NEW`, team `Team PnC`.
- Iteration root `DP_Unifi_NEW\Team1 PnC Admin`; sprints are matched by number, so names with
  suffixes (`Sprint 119 - Complete Frontier AI tickets`) still work.
- Work item types here are CMMI: **Requirement**, Bug, Feature, Task, plus TFVC Code Review items.
- Python 3. Azure OpenAI is optional - the rules path needs no AI.
- Auth: PAT (Work Items: Read **and Code: Read**) in `.env`, or `az login` if Azure CLI is installed.
- **Rotate the PAT that was pasted in chat.** Never put secrets in tracked files or chat.
- Data stays local in `data/` (git-ignored). No production database needed.

## Step 1 - Quick counts (no code)

Create an ADO query: Iteration Path under `Team1 PnC Admin`, Sprints 104-123, all work item types.
Add a chart by type and by month. Write the numbers down - Step 2 must match them.

## Step 2 - Collect (`collect.py`)

1. WIQL query -> ticket IDs for Sprints 104-123.
2. Fetch tickets in batches of 200 with relations: ID, type, title, description, acceptance criteria, repro steps, state, area, iteration, tags, created/closed date.
3. From relations, take changeset links (`vstfs:///VersionControl/Changeset/{id}`), Git commits and PRs. **Team PnC uses mostly TFVC** (1,757 changeset links vs 154 Git commits across 2026).
4. For each changeset: comment, author, date, list of changed files. (No diffs yet.)
5. Download **text attachments** (`.sql`, `.txt` ...). Tickets often carry the SQL script that did the work.
6. Save to `data/tickets.db` (SQLite).
7. Print: counts by type and month, and **evidence coverage** - % of tickets with a linked changeset or a text attachment.

Check: counts match Step 1. Low link coverage limits Step 4 - note it.

## Step 3 - Categorize (`categorize.py`)

Two ways to label, same `topics.yaml`:

- **Rules (no AI, works today):** `python src/categorize.py rules`. Regexes in `topics.yaml`
  assign topics and one work pattern. Consistent and explainable.
- **AI (when Azure OpenAI is available):** `propose` drafts the list, the team edits it and sets
  `reviewed: true`, then `label` labels every ticket with a one-line summary and confidence.

Either way results go to the DB and `data/tickets.csv`.

Check: a person reviews ~30 random tickets (`python src/categorize.py review`).

## Step 4 - Analyse and write dossiers

**Without Azure OpenAI (today):** `python src/export.py` writes `data/summary.md`,
`data/tickets.csv`, `data/changes.csv` and `data/bundles/*.md`. The bundles are readable
Markdown, so a person - or an AI assistant in a chat session - can analyse them and write the
report and dossiers by hand. Current output: `reports/2026-full-year.md`.

**With Azure OpenAI:** `python src/dossier.py "<topic or work pattern>"` gathers tickets plus
their changesets, diffs and attached scripts and drafts `dossiers/<name>.md` with typical steps,
files and tables touched, common problems, a checklist and source links.

Either way, an engineer reviews and corrects the result before the team relies on it.

## Step 5 - Review and decide next

- Is evidence coverage good enough? (Full 2026: ~58%.)
- Were the topics and the dossier useful?

Then choose: more dossiers, a dashboard (Power BI or Streamlit), Copilot / Azure DevOps MCP server for Q&A, or a 3-5 year backfill.

## Ground rules

- Read-only. Never write to Azure DevOps.
- Counts come from queries, not AI.
- AI output cites ticket/changeset IDs; unsupported claims are marked as guesses.
- Emails, phone numbers, long numbers and secrets are masked before any text is sent to AI.
- Templates need engineer approval before use.
- No ranking of individuals.

## Files

```
cli/DevOps/
  PLAN.md
  config.yaml        org, project, iteration root, sprint range, limits
  .env.example       copy to .env for secrets (git-ignored)
  requirements.txt
  .gitignore         data/, .env
  src/
    common.py        config, Azure DevOps client, SQLite, masking, Azure OpenAI
    collect.py       Step 2 - download tickets, changesets, attachments
    categorize.py    Step 3 - rules or AI labelling
    export.py        Step 4 - offline export for analysis without AI
    dossier.py       Step 4 - AI dossier (needs Azure OpenAI)
    template.py      Step 4 - generate a rollout backlog from a proven numbered sequence
    report.py        Step 5 - dashboard JSON for an external web app
  topics.yaml        topics and work patterns, edited by the team
  DASHBOARD.md       data contract for the dashboard web app
  reports/           written analysis
  templates/         generated backlogs for repeat rollouts
  web/viewer.html    reference viewer (smoke test, not the product)
  data/              local only: tickets.db, *.csv, summary.md, dashboard.json, bundles/
  dossiers/
```

## How to run

Run from `cli/DevOps`:

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env        # fill in a PAT (Work Items: Read + Code: Read)

python src/collect.py --first 104 --last 107   # pilot: a few sprints
python src/collect.py                          # Sprints 104-123 (re-runs skip what is cached)

python src/categorize.py rules     # label with topics.yaml regexes, no AI
python src/export.py               # summary.md, CSVs and readable ticket bundles
python src/categorize.py review    # data/review_sample.csv for a person to check

python src/template.py --name "DW4 Toyota" --base 700   # next rollout backlog, no AI needed
python src/report.py               # data/dashboard.json for the dashboard web app

# Only when Azure OpenAI is available:
python src/categorize.py propose
python src/categorize.py label
python src/dossier.py "Numbered migration step"
```

Useful flags: `--no-changes` / `--no-attachments` (skip downloads), `--report-only`,
`--refresh` (re-fetch), and `python src/export.py --topic "honda"` to bundle one subject.

Data is stored in SQLite (`data/tickets.db`): a single local file, no server. It keeps the ticket -> task -> changeset -> file links and lets re-runs skip data and AI calls already done. CSV exports are for people and Excel/Power BI.

## Open questions

- Is `Team1 PnC Admin` the right iteration/area path for the whole team? (All 1,961 collected
  items share that one area path, so it looks right.)
- Who can issue a PAT with **Code: Read** so changeset contents can be downloaded?
- Is Azure OpenAI available to the team, and if so which endpoint and deployment?
- Who reviews `topics.yaml` and the first dossier?
- Should Tasks and Code Review items stay out of the headline "how many tickets" number?