# Dashboard data contract

This tool does **analysis only**. It renders nothing. `src/report.py` writes a single
self-describing JSON document that a separate web app fetches and displays.

> **First-try scope:** the dashboard displays Azure DevOps **ticket data only** - counts,
> types, topics, work patterns, dates, Size and Assigned To. Source-control data remains in
> raw analysis sections for possible future knowledge-base work, but it does not drive any
> current KPI or chart.

```powershell
python src/report.py                 # -> data/dashboard.json  (compact, ~450 KB)
python src/report.py --pretty        # indented, easier to read while developing
python src/report.py --split         # also -> data/dashboard/<section>.json
```

## Choosing a shape

| Need | Use |
|---|---|
| One fetch, simplest wiring | `data/dashboard.json` |
| Fast first paint | `--split`: every summary section is **under 12 KB**; only `tickets.json` (~434 KB) is large, so lazy-load it for drill-down |

All counts are already aggregated - the web app should not need to recompute anything
to draw the standard charts.

## Reference viewer

`web/viewer.html` renders all 9 charts from the JSON with Chart.js in about 100 lines. It is
a **worked example and a smoke test, not the product** - use it to confirm the data is sound
and to see how each chart type binds.

```powershell
cd cli/DevOps
python -m http.server 8000
# open http://localhost:8000/web/viewer.html
```

The separate deep ticket-content analysis has its own artifacts and does not modify these
nine charts:

- `data/deep-analysis.json` - reproducible detailed findings, ticket IDs and six new specs
- `reports/2026-deep-content-analysis.md` - written interpretation and recommendations
- `web/deep-viewer.html` - reference renderer for only the six new deep-analysis charts
- `src/deep_analysis.py` - generator; run `python src/deep_analysis.py --pretty`

(Serving over http is required; browsers block `fetch` on `file://` URLs.)

## Conventions

- Encoding UTF-8, dates are **ISO 8601 UTC** strings as returned by Azure DevOps.
- Count lists use a uniform shape so one chart component can render any of them:
  ```json
  [ { "name": "Requirement", "count": 695 } ]
  ```
- `meta.schema_version` is an integer. **Check it.** It increments on breaking changes.
- A "ticket" means a real work request - `Requirement`, `Bug`, `Feature` - as configured by
  `label_types` in `config.yaml`. Tasks and TFVC code-review items are *excluded* from ticket
  counts because they are bookkeeping rows, not requests. `totals.work_items_by_type` is the
  only place the unfiltered totals appear.

## Sections

### `charts` - start here

A ready-to-render array. **Every chart in the reference viewer is built from this section and
nothing else**, so a generic component can loop over it without knowing anything about tickets.

```json
{
  "id": "tickets_by_sprint",
  "title": "Tickets per sprint",
  "type": "bar",
  "stacked": true,
  "x_label": "Sprint",
  "y_label": "Tickets",
  "labels": ["104", "105", "106"],
  "series": [
    { "name": "Requirement", "data": [6, 96, 22] },
    { "name": "Bug",         "data": [5, 17, 11] }
  ]
}
```

| Field | Notes |
|---|---|
| `id` | stable key - use it to pick or place specific charts |
| `type` | `bar`, `line`, `area`, `pie` |
| `labels` | one entry per category on the x axis (or per slice for `pie`) |
| `series` | `{ name, data }`; `data` aligns index-for-index with `labels` |
| `stacked` | optional, `bar`/`area` |
| `horizontal` | optional hint - set when there are many long category names |
| `x_label`, `y_label` | optional axis titles |
| `total`, `percent` | `pie` only - see below |
| `note` | optional caveat; **render it, it prevents misreading the chart** |

`pie` specs always have exactly one series, and carry two extra fields:

- `percent[i]` - the share of slice `i`, already rounded to one decimal. **Render it** next to
  the slice name or in the tooltip; a pie without percentages is hard to read.
- `total` - the denominator those percentages are based on.

Where a pie is truncated the remainder is folded into an explicit `Other (N more)` slice, so
`percent` always adds up to 100 and no data is silently dropped. Note that for `topics` the
denominator is **topic mentions, not tickets** (a ticket can carry several topics), so those
percentages compare topics against each other and cannot be read as "x% of our work".

Charts currently emitted (9):

| Group | `id` |
|---|---|
| Throughput | `tickets_by_sprint` (stacked bar), `created_vs_closed` (line) |
| Composition | `ticket_types`, `work_patterns`, `topics` (pies) |
| Size | `size_distribution`, `size_by_sprint` |
| People | `tickets_by_assignee` |
| Focus | `topic_trend` (stacked area) |

This list is deliberately short. Sections such as `cycle_time`, `evidence`, `tags`, `links`,
`rollout` and the code hot spots in `code` are **still computed and published in full** - they
simply do not get a pre-built chart. Build your own visuals from them if you want to; the
numbers are all there. For the first-try dashboard, do not surface `code`: its focus is ticket
analysis, not source-control analysis.

If you prefer to build your own visuals, ignore `charts` and use the raw sections below - they
hold the same numbers plus extra detail.

### `meta`
Provenance and data quality. Surface these so viewers know how much to trust the page.

| Field | Meaning |
|---|---|
| `schema_version` | integer, bump = breaking change |
| `generated_at` | when this file was written |
| `last_collected_at` | when Azure DevOps was last polled |
| `source.organization` / `.project` / `.iteration_root` | where the data came from |
| `sprint_first`, `sprint_last` | sprint range covered |
| `work_items_collected` | everything downloaded, including child rows |
| `tickets` | real tickets (the headline number) |
| `ticket_types`, `child_types` | which types count as what |
| `data_quality.changes_with_errors` | changesets that failed to fetch; **0 is healthy** |
| `data_quality.low_confidence_labels` | tickets labelled under 0.6 confidence |
| `data_quality.taxonomy_reviewed` | `false` until a human reviews `topics.yaml` |

> **Show a warning banner when `taxonomy_reviewed` is `false`** - topic and work-pattern
> numbers are draft-quality until then. The other sections are unaffected.

### `totals`
`work_items_by_type`, `tickets_by_type`, `tickets_by_state` (count lists), plus scalars
`tickets_total`, `tickets_closed`, `closed_pct`, `requirement_to_bug_ratio`.
Good for headline tiles and a donut chart.

### `sprints`
One row per sprint, ordered. Bar or line chart of throughput.
```json
{ "sprint": 105, "tickets": 114, "closed": 112, "by_type": { "Requirement": 96, "Bug": 17 } }
```

### `monthly`
One row per month with `created` and `closed`. A created-vs-closed line chart shows whether
the team kept up with intake.

### `size`
The team's own **Size** estimate (`Microsoft.VSTS.Scheduling.Size`), set during refinement on
a Fibonacci-style scale. This is the only real measure of effort in the data - everything else
(check-ins, files, cycle time) is a proxy.

| Field | Notes |
|---|---|
| `tickets`, `sized`, `unsized`, `coverage_pct` | how much of the backlog carries an estimate |
| `total_points`, `median_points`, `mean_points` | totals over sized tickets only |
| `distribution` | `{ name, count }` per size value - drives `size_distribution` |
| `by_type` | per ticket type: `tickets`, `total_points`, `median_points`, `mean_points` |
| `by_sprint` | `{ sprint, points, tickets }` for tickets **closed** in that sprint |

Unsized tickets are **excluded, not counted as zero** - treating them as zero would silently
understate every total. Always show `coverage_pct` next to a size total. `by_sprint` credits
points to the delivery sprint, so it reads as throughput; open tickets contribute nothing yet,
and a sprint where sizing was skipped will read low for that reason alone rather than because
less was done.

### `cycle_time`
Elapsed days from created to closed.
- `overall` and `by_type` / `by_sprint` rows: `count`, `median_days`, `p90_days`, `mean_days`
- `histogram`: a count list bucketed `0-7 days` through `6+ months`

Medians are used because a few very old tickets skew the mean. This is **lead time**, not
hands-on effort - it includes time sitting in the backlog.

### `labels`
- `topics` - `{ name, count, by_type }`. A ticket can hold several topics, so **the counts
  intentionally sum to more than the ticket total**; do not render as a pie of the whole.
- `work_patterns` - a count list; each ticket has exactly one, so these *do* sum to the total.
- `labelled`, `low_confidence`, `mean_confidence` - quality indicators.

### `topic_trend`
`sprints` (x axis) plus `topics[] = { name, data }` for the top 10 topics, aligned to
`sprints`. Feeds a stacked area or heatmap of what the team focused on over the year.

### `tags`
Count list of the 30 most-used Azure DevOps tags. En/em dashes are folded to hyphens so
`ACCP - Ready for testing` and `ACCP – Ready for testing` count as one tag.

### `evidence`
Per ticket type: `tickets`, `with_code`, `with_attachment`, `with_either`, `coverage_pct`.
Answers "can we actually see how the work was done?"

### `people`
Ticket ownership from Azure DevOps **Assigned To**. This section does not use changesets,
commits, pull requests or source-control authors.

- `assigned` - `{ name, tickets, size_points }`, ordered by ticket count
- `tickets_total`, `tickets_assigned`, `tickets_unassigned`, `coverage_pct`

Each ticket belongs to at most one person and is counted once. `size_points` is the sum of the
team-entered Size values on those same tickets; unsized tickets contribute to `tickets` but
not to `size_points`. Assigned To is the ticket's **current/final owner**, not a history of
everyone who participated.

### `code`
What the linked changesets touched.
- `changes_by_kind` - `{ kind, changes, files }` for `tfvc`, `git_commit`, `pr`
- `changes_total`, `files_total`, `distinct_files`
- `duplicate_prs_excluded` - how many pull requests were dropped as duplicates (see below)
- `top_authors` (25) - `{ name, tickets, size_points, count, files, by_kind }`, ordered by `tickets`
- `author_coverage` - how much of the backlog the per-author numbers speak for:
  `tickets_total`, `tickets_with_author`, `coverage_pct`, `size_points_covered`,
  `shared_tickets`
- `top_files` (50), `top_folders` (40), `file_extensions` (30)

`top_folders` rolls paths up to three segments, which is the useful granularity for a
"hot areas" chart.

> **Pull requests are de-duplicated.** Azure DevOps links both a pull request and its merge
> commit to a ticket, which is the same work recorded twice. The commit is kept as the
> canonical change and the pull request is dropped from every code count, including
> `change_files`-derived folder and extension totals.

> **`top_authors` is retained as raw code analysis, but does not drive a dashboard chart.**
> Prefer `tickets` - the
> number of distinct tickets a person checked code into, counting each ticket once however
> many check-ins it took - with `size_points` (the team's Size estimate for those tickets)
> alongside. `count` (check-ins) and `files` are kept
> for detail but are poor measures on their own: someone who commits little and often
> outranks someone who ships the same work in fewer, larger check-ins. `by_kind` shows the
> TFVC/Git split, which is the main source of that distortion.
>
> Two limits are built into these numbers and are reported in `author_coverage`. Only about
> **28% of tickets have an identifiable code author**, because analysis, testing and sign-off
> leave no changeset - so this is a view of coding work, not of all work. And where several
> people worked one ticket, **each is credited the full size**, so `size_points` overlaps
> between people and the bars must not be summed. Keep the `note`.

> **Author identities are merged** using `author_aliases` in `config.yaml`. Git takes each
> developer's name from their local git config, so the same person can appear as both
> `Tao, Dongwei` and `dongwei`. The shipped list is a **guess based on first names and needs
> review**; add or correct entries there and re-run `report.py`.

### `links`
`links_by_kind`, `text_attachments_by_extension`, `text_attachments`. Shows the TFVC-vs-Git mix.

### `rollout`
The repeated DW accounting model sequence - the knowledge-base payoff.
- `matched_steps`, `dw2_steps`, `dw3_steps`, `phases`
- `steps[]` - each with `step`, `phase`, `description`, and `dw2` / `dw3` objects
  (`id`, `sprint`, `state`)

Render as a phase-grouped table. Each `id` links to
`https://dev.azure.com/{org}/{project}/_workitems/edit/{id}`.

### `tickets`
One slim row per ticket for drill-down, search and CSV export. The only large section.

```json
{
  "id": 180186, "type": "Bug", "state": "Closed", "sprint": 104,
  "title": "...", "created": "...", "closed": "...",
  "url": "https://dev.azure.com/.../_workitems/edit/180186",
  "tags": ["Data", "Urgent"],
  "assigned_to": "Tao, Dongwei",
  "size": 3, "priority": 2, "severity": "3 - Medium",
  "topics": ["Contracts and cancellations"], "work_pattern": "Data fix script",
  "confidence": 0.33, "code_links": 3, "text_attachments": 1
}
```

`assigned_to` is `null` for an unassigned ticket. `size` is `null` where the team did not
estimate the ticket. `severity` is only set on Bugs.

`url` is ready to use as an external link. `code_links` counts changesets, commits and PRs on
the ticket **and its child tasks**, so it matches what `evidence` reports.

## Refreshing

The JSON is a static artifact. Regenerate it after collecting:

```powershell
python src/collect.py          # pull new sprints from Azure DevOps
python src/categorize.py rules --draft
python src/report.py
```

Serve `data/dashboard.json` from any static host or copy it into the web app's assets. There is
no database connection and no API for the dashboard to call - by design, so the dashboard never
needs Azure DevOps credentials.

## Caveats to display

1. Topic and work-pattern labels are regex-derived and **draft** until `topics.yaml` is reviewed
   (`meta.data_quality.taxonomy_reviewed`).
2. Ticket counts, sprint and monthly trends, evidence coverage, code stats and the rollout
   section do **not** depend on those labels and are reliable now.
3. Scope is the configured `iteration_root` only. Other teams' area paths are not included.
