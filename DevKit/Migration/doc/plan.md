# Plan: DevKit Migration CLI (Python)

A Python CLI (`devkit-migration`, alias `dkm`) that drives an Azure DevOps Server "Migration" parent work item by walking through its 9 child Task work items across multiple days. The CLI is **resumable**, marks each child Task `Done` in ADO as it completes, and is built around a **pluggable per-task handler** registry so we can ship Task 1 fully implemented now and fill in the remaining 8 over time.

Stack: **Python 3.11+**, **Typer** (CLI), **httpx** (HTTP), **pydantic** (config/state models), **rich** (output), **uv** (packaging). PAT supplied via env var `AZDO_PAT`. State persisted as JSON under `~/.devkit/migration-<ticketId>.json`. Initial concrete handler: **Task 1 — `10 days before - Book resources`** (creates two ServiceNow requests; automation method TBD — see Decisions).

## Phases & Steps

### Phase 1 — Project scaffolding (foundation, blocks everything else)
1. Initialize `uv` project under `Migration/` with `pyproject.toml`, `src/devkit_migration/` layout, `README.md`, `.gitignore`, `.env.example`.
2. Add dependencies: `typer`, `httpx`, `pydantic`, `pydantic-settings`, `rich`, `python-dateutil`. Dev: `pytest`, `pytest-httpx`, `ruff`, `mypy`.
3. Configure `[project.scripts]` to expose `devkit-migration` and short alias `dkm`.
4. Set up `ruff` + `mypy` config in `pyproject.toml`, plus a `tasks.json`/`launch.json` in `.vscode/` for run/debug.

### Phase 2 — Azure DevOps Server client (parallel with Phase 3)
1. `client/ado.py` — thin `httpx.Client` wrapper. Base URL pattern `https://cv.websct.ia.iafg.net/tfs/{collection}/{project}/_apis/...`. Reads PAT from `AZDO_PAT`, sends `Authorization: Basic base64(":<PAT>")`. API version pinned (default `6.0`, configurable).
2. Methods: `get_work_item(id, expand="relations")`, `get_child_tasks(parent_id)` (resolves `System.LinkTypes.Hierarchy-Forward` relations), `update_task_state(id, state, *, comment=None)` (JSON-Patch `PATCH /_apis/wit/workitems/{id}?api-version=6.0`).
3. Parse migration date from parent ticket title (`Migration <Month> <Day>` → e.g. "Migration July 15") with a fallback to a custom `MigrationDate` field if present; user can override via `--date`.
4. Centralized error handling: surface 401 (bad PAT), 404 (wrong ID/project), 5xx with retry-with-backoff via `httpx.Transport`.

### Phase 3 — Task handler framework (parallel with Phase 2)
1. `core/task.py` — `TaskHandler` protocol with `task_key: str`, `display_name: str`, `automation_level: Literal["auto","semi","manual"]`, `run(context) -> TaskResult`, `is_applicable(context) -> bool` (date-gating hook).
2. `core/registry.py` — decorator-based registry `@register_task("book-resources")`. Resolves an ADO child Task title to a handler via configurable mapping (`config/tasks.yaml`) so renames in ADO don't break code.
3. `core/state.py` — pydantic model + JSON file I/O at `~/.devkit/migration-<ticketId>.json`. Tracks per-task: `status` (pending/in-progress/done/skipped/failed), `started_at`, `completed_at`, `outputs` (free-form dict, e.g. ServiceNow request numbers), `attempts`, `last_error`.
4. `core/runner.py` — orchestrates: load parent ticket → fetch children → reconcile with state file → for selected task(s): run handler → on success, update ADO state to `Done` → persist local state. Handles **ordering policy** (configurable: strict / loose / date-gated — defaults TBD per task per Decision 2).

### Phase 4 — CLI surface
1. `cli.py` Typer app with commands:
   - `dkm init <ticket-id>` — bootstrap state file, fetch & cache parent + children.
   - `dkm status [ticket-id]` — show table of tasks with state, dates, and outputs (rich table).
   - `dkm run [ticket-id] [--task <key>] [--all] [--yes] [--dry-run]` — run one named task, or all pending+applicable tasks.
   - `dkm complete [ticket-id] <task-key> [--note]` — manually mark a task done (for purely-manual steps).
   - `dkm reset [ticket-id] <task-key>` — reset a task back to pending.
   - `dkm config edit` — open the per-task config YAML in `$EDITOR`.
2. Global flags: `--ticket-url` (parses ID + collection + project from a full URL like the one shared), `--verbose`, `--no-color`.

### Phase 5 — Task 1 handler: `book-resources`
1. `handlers/book_resources.py` — implements `TaskHandler`.
2. ServiceNow client `client/snow.py` supporting two modes (selectable in config):
   - **API mode**: `POST /api/sn_sc/servicecatalog/items/{sys_id}/order_now` with templated variables; reads `SNOW_BASE_URL`, `SNOW_USER`, `SNOW_PASS` (or `SNOW_TOKEN`) from env.
   - **Browser mode**: builds a prefilled `com.glideapp.servicecatalog_cat_item_view.do?sysparm_id=<sys_id>&sysparm_variables=...` URL, opens via `webbrowser.open`, then prompts user to paste the resulting `REQ`/`RITM` numbers.
3. Inputs (resolved in order: CLI flags → state file → `config/tasks.yaml` → interactive prompt): migration date/time window (from parent ticket), the 2 catalog item `sys_id`s, per-item variable map. **Exact field values to be provided by user later.**
4. On success: store `{"requests": ["REQ0010234", "REQ0010235"]}` in state outputs, post a comment on the ADO task linking the requests (optional, behind a flag), set task to `Done`.

### Phase 6 — Stubs for tasks 2–9 + verification
1. Generate stub handlers for: `9-days-before-dpaccp`, `3-days-before`, `2-days-before`, `migration-day-tests`, `migration-day`, `day-after-migration`, `merge-tpa-develop-to-master`, `code-review-tpa-merge`. Each stub: prints its expected manual steps from `config/tasks.yaml`, prompts to confirm, then marks Done in ADO.
2. Smoke tests with `pytest-httpx` mocking both ADO and ServiceNow.
3. End-to-end dry run against ticket **205406** in `--dry-run` mode (no writes).

## Relevant files (to be created under `Migration/`)
- `Migration/pyproject.toml` — uv project, deps, scripts, tool configs.
- `Migration/src/devkit_migration/cli.py` — Typer entry point.
- `Migration/src/devkit_migration/client/ado.py` — ADO Server REST client (PAT, JSON-Patch updates).
- `Migration/src/devkit_migration/client/snow.py` — ServiceNow client (API + browser fallback).
- `Migration/src/devkit_migration/core/{task.py,registry.py,state.py,runner.py}` — handler framework.
- `Migration/src/devkit_migration/handlers/book_resources.py` — Task 1 implementation.
- `Migration/src/devkit_migration/handlers/_stubs.py` — Tasks 2–9 stubs.
- `Migration/config/tasks.yaml` — title→handler mapping + per-task config (catalog sys_ids, prompts, etc.).
- `Migration/.env.example` — `AZDO_PAT`, `AZDO_BASE_URL`, `AZDO_COLLECTION`, `AZDO_PROJECT`, `SNOW_BASE_URL`, `SNOW_USER`, `SNOW_PASS`.
- `Migration/tests/` — unit tests for client, registry, runner, and `book_resources`.

## Verification
1. `uv run dkm --help` shows all commands.
2. `uv run dkm init 205406 --ticket-url https://cv.websct.ia.iafg.net/tfs/SOPCV_SNW/DP_Unifi_NEW/_workitems/edit/205406` creates the state file and lists all 9 child tasks pulled from ADO.
3. `uv run dkm status 205406` prints a rich table with task names, states, and (parsed) migration date `2026-07-15`.
4. `uv run dkm run 205406 --task book-resources --dry-run` prints exactly what it would POST to ServiceNow without sending, and would not modify the ADO task state.
5. Real run of `book-resources` against a non-prod ServiceNow target creates two requests, records their numbers in the state file, and flips the matching ADO Task to `Done` (verified by re-opening the ticket in browser).
6. `uv run pytest` — unit tests green; coverage on `core/` and `client/ado.py` ≥ 80%.

## Decisions captured
- Language: **Python 3.11+**; CLI: **Typer**; packaging: **uv**.
- ADO target: on-prem **Azure DevOps Server** at `https://cv.websct.ia.iafg.net/tfs/SOPCV_SNW/DP_Unifi_NEW`. TLS certificate trusted on the dev machine — no custom CA needed.
- Auth: **PAT** via env var `AZDO_PAT`. ADO writeback: **update each child Task's State to Done** (no comment unless explicitly enabled per task).
- Tasks live as **child Task work items** linked to parent ticket via `System.LinkTypes.Hierarchy-Forward`.
- State storage: **JSON file per ticket** at `~/.devkit/migration-<ticketId>.json` (resumable across days).
- Total tasks: **9** (not 10). Initial handler ships for **Task 1 only**; tasks 2–9 are stubs.
- Migration date parsed from the parent ticket title (`Migration July 15`), overridable via `--date`.

## Further considerations (open decisions for user)
1. **ServiceNow automation mode for `book-resources`** — A (`sc_request` table API) / **B (catalog `order_now` API)** *recommended* / C (prefilled browser URL).
   - Needs: ServiceNow instance base URL, the 2 catalog item `sys_id`s (or table + field map for A), and credentials/token. Falls back to C if API access is blocked.
2. **Task ordering policy** — user said "mixed, details later". Provisional default: each task declares its own policy in `config/tasks.yaml` (`requires: [<task-keys>]` and/or `unlock_days_before: <n>`). Final per-task rules to be filled in when remaining handlers are designed.
3. **Comments on tasks** — currently disabled; enable via `--comment` flag or per-task config? Default OFF unless you want a Run summary auto-posted.
