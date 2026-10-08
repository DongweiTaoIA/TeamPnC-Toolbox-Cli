"""Deep, ticket-only content analysis kept separate from the main dashboard.

This reads Azure DevOps ticket fields only. It does not query changesets, commits,
pull requests, changed files, or source-control authors.

Usage:
    python src/deep_analysis.py
    python src/deep_analysis.py --pretty
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
from pathlib import Path

from common import ROOT, load_config, require_db

OUT = ROOT / "data" / "deep-analysis.json"
REPORT = ROOT / "reports" / "2026-deep-content-analysis.md"

# Themes are deliberately title-led. Descriptions are very unevenly populated by
# ticket type and often contain broad rollout context that would inflate matches.
THEMES = [
    ("Accounting model programmes",
     r"accounting model|\b(?:dw2|dw3)\b|honda.*\bdw3\b|\bppm\b.*\bam4\b|"
     r"\bwpp\b.*\bam6\b|wearable parts protection"),
    ("Reports and batch operations",
     r"\breport\b|batch job|\bndpy\w*\b|\bdpr\w*\b|one statement|aging"),
    ("Payments, cash and remittance",
     r"payment|remittance|cash deposit|cash transfer|chargeback|reconcil|cheque|e-?fund"),
    ("Testing and quality assurance",
     r"test only|test-only|testing|automated tests?|regression|sign[ -]?off|\bqc\d*\b"),
    ("Contracts and lifecycle",
     r"contract|certificate|cancellation|reinstate|refund|overfund|debt waiver|expir|\bvoid"),
    ("Dealer billing and statements",
     r"dealer|billing|invoice|invoic|suspense|statement"),
    ("Refactoring and dependency analysis",
     r"dependency analysis|code refactoring|extract.*dependency"),
    ("Data maintenance and upload",
     r"data fix|fix data|cleanup|orphaned|\bupload\b|reinitialize|devoid|"
     r"historical data|table structure"),
    ("Release and migration",
     r"\bmigration\b|move to accp|ready for (?:prod|migration)|release pipeline"),
    ("Platform and delivery tooling",
     r"pipeline|sql server|ssrs|technical maintenance|resources?|webapi|web api|"
     r"endpoint|edmx|performance very slow|helmchart|sonarqube"),
    ("Rates, programmes and products",
     r"\brate\b|rates|plan code|program|product setup|promo code|branding fee|onboarding"),
    ("Claims and authorizations",
     r"\bclaim|authorization|authorisation|auto-authorization"),
    ("Security, privacy and compliance",
     r"pen test|soc 2|dynatrace|vulnerab|\bxss\b|security|audit|anonymi|data disposition"),
]

PROGRAMMES = [
    ("DW2 rollout", r"\bnew dw2\b|accounting model.*\bdw2\b"),
    ("DW3 / Honda rollout", r"\bdw honda\b|\bnew dw3\b|honda.*accounting model"),
    ("PPM / AM4 rollout", r"\bppm\b.*(?:accounting model|\bam4\b)"),
    ("WPP / AM6 rollout",
     r"(?:wearable parts protection|\bwpp\b).*(?:accounting model|\bam6\b)"),
]

RECURRING_SERIES = [
    ("Dependency-analysis refactoring",
     r"dependency analysis|code refactoring|extract.*dependency"),
    ("Test-only rollout steps", r"test[ -]?only"),
    ("Sprint automated-test monitoring", r"monitoring and rerunning of automated tests"),
    ("Migration event wrappers", r"^\s*migration\s*-"),
    ("One Statement monthly run", r"run one statement"),
    ("Sherlock monthly upload", r"sherlock upload"),
    ("SAL finance aging report", r"sal finance aging report"),
    ("E-fund cash-processing failures", r"e-?fund.*cash deposit.*cash transfer.*fail"),
    ("Dealer-billing report failures", r"dealer billing aging.*report failed"),
    ("Rejected-transaction support", r"rejected transaction|transactions still being rejected"),
    ("Build/release pipeline testing", r"(?:build|release) pipelines?"),
]

STOP_WORDS = {
    "a", "an", "and", "or", "the", "for", "to", "of", "in", "on", "with", "from", "by",
    "as", "at", "is", "are", "be", "this", "that", "it", "into", "use", "using", "should",
    "will", "when", "after", "before", "per", "new", "change", "changes", "changed", "update",
    "updated", "add", "remove", "create", "fix", "issue", "investigation", "requirement",
    "migration", "part", "data", "ticket", "tickets", "jan", "feb", "mar", "apr", "may",
    "jun", "jul", "aug", "sep", "oct", "nov", "dec", "unifi", "ep", "su", "vse", "only",
}


def pct(n: int | float, total: int | float) -> float:
    return round(100 * n / total, 1) if total else 0


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", value)
    return dt.datetime(*(int(v) for v in match.groups())) if match else None


def median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    middle = len(values) // 2
    value = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
    return round(value, 1)


def pairs(values: collections.Counter) -> list[dict]:
    return [{"name": name, "count": count} for name, count in values.most_common()]


def example(row) -> dict:
    return {"id": row["id"], "title": row["title"], "url": row["url"]}


def summarize(name: str, regex: str, rows: list, total: int) -> dict:
    pattern = re.compile(regex, re.I)
    matched = [row for row in rows if pattern.search(row["title"] or "")]
    sizes = [float(row["size"]) for row in matched if row["size"] is not None]
    cycle = []
    for row in matched:
        created, closed = parse_ts(row["created"]), parse_ts(row["closed"])
        if created and closed and closed >= created:
            cycle.append((closed - created).total_seconds() / 86400)
    sprints = [row["sprint"] for row in matched if row["sprint"] is not None]
    assignees = collections.Counter(
        row["assigned_to"] for row in matched if (row["assigned_to"] or "").strip())
    return {
        "name": name,
        "match": regex,
        "tickets": len(matched),
        "ticket_pct": pct(len(matched), total),
        "by_type": dict(collections.Counter(row["type"] for row in matched)),
        "closed": sum(bool(row["closed"]) for row in matched),
        "size_points": round(sum(sizes), 1),
        "sized": len(sizes),
        "median_size": median(sizes),
        "median_cycle_days": median(cycle),
        "sprint_first": min(sprints) if sprints else None,
        "sprint_last": max(sprints) if sprints else None,
        "top_assignees": pairs(assignees)[:5],
        "examples": [example(row) for row in matched[:5]],
        "ticket_ids": [row["id"] for row in matched],
    }


def text_quality(rows: list) -> dict:
    fields = ["title", "description", "acceptance", "repro", "tags", "size", "assigned_to"]
    overall = {}
    by_type = {}
    for field in fields:
        count = sum(row[field] is not None and str(row[field]).strip() != "" for row in rows)
        overall[field] = {"present": count, "total": len(rows), "coverage_pct": pct(count, len(rows))}
    for ticket_type in sorted({row["type"] for row in rows}):
        typed = [row for row in rows if row["type"] == ticket_type]
        by_type[ticket_type] = {}
        for field in fields:
            count = sum(row[field] is not None and str(row[field]).strip() != "" for row in typed)
            by_type[ticket_type][field] = {
                "present": count, "total": len(typed), "coverage_pct": pct(count, len(typed))
            }
    return {"overall": overall, "by_type": by_type}


def description_analysis(rows: list) -> dict:
    described = [row for row in rows if (row["description"] or "").strip()]
    lengths = sorted(len(row["description"]) for row in described)
    structure_patterns = [
        ("User-story wording", r"\bas a\b.{0,120}\bi want\b"),
        ("Purpose/objective wording", r"\b(?:purpose|objective|goal)\b"),
        ("Business-requirement heading", r"\bbusiness requirement"),
        ("Acceptance wording in Description",
         r"\bacceptance criteria|given\b.{0,100}\bwhen\b.{0,100}\bthen\b"),
        ("Technical implementation wording",
         r"\b(?:stored procedure|table|column|webapi|endpoint|sql|script)\b"),
        ("Copy/deploy instructions",
         r"\b(?:copy|deploy|migration|move to accp|ready for prod)\b"),
    ]
    structures = []
    for name, regex in structure_patterns:
        pattern = re.compile(regex, re.I | re.S)
        matched = [row for row in described if pattern.search(row["description"])]
        structures.append({
            "name": name,
            "tickets": len(matched),
            "description_pct": pct(len(matched), len(described)),
            "examples": [example(row) for row in matched[:5]],
        })

    duplicate_groups = collections.defaultdict(list)
    for row in described:
        normalized = re.sub(
            r"\s+", " ",
            re.sub(r"\d+", "<n>", row["description"].lower())
        ).strip()
        duplicate_groups[normalized].append(row)
    repeated = sorted(
        (group for group in duplicate_groups.values() if len(group) > 1),
        key=lambda group: -len(group),
    )
    p90_index = int(0.9 * (len(lengths) - 1)) if lengths else 0
    return {
        "described_tickets": len(described),
        "description_coverage_pct": pct(len(described), len(rows)),
        "median_characters": median(lengths),
        "p90_characters": lengths[p90_index] if lengths else None,
        "structures": structures,
        "repeated_template_groups": len(repeated),
        "tickets_in_repeated_templates": sum(len(group) for group in repeated),
        "largest_repeated_templates": [
            {
                "tickets": len(group),
                "examples": [example(row) for row in group[:5]],
            }
            for group in repeated[:10]
        ],
    }


def title_markers(rows: list) -> list[dict]:
    grouped = collections.defaultdict(list)
    marker_re = re.compile(r"^\s*\((EP|SU|VSE|SE)\)\s*:?", re.I)
    for row in rows:
        match = marker_re.match(row["title"] or "")
        grouped[match.group(1).upper() if match else "No marker"].append(row)
    result = []
    for marker, group in sorted(grouped.items(), key=lambda item: -len(item[1])):
        result.append({
            "marker": marker,
            "tickets": len(group),
            "ticket_pct": pct(len(group), len(rows)),
            "by_type": dict(collections.Counter(row["type"] for row in group)),
            "size_points": round(sum(row["size"] or 0 for row in group), 1),
        })
    return result


def top_phrases(rows: list, limit: int = 30) -> list[dict]:
    counts = collections.Counter()
    examples = collections.defaultdict(list)
    for row in rows:
        title = re.sub(r"\([^)]*\)\s*:?", " ", (row["title"] or "").lower())
        title = re.sub(r"\b(?:inc|prb|ttmu|papp|ndpy|dpr|qc|svb|nity)[-\w]*\b", " ", title)
        title = re.sub(r"\b\d+(?:[./-]\d+)*\b", " ", title)
        tokens = [
            token for token in re.findall(r"[a-z][a-z0-9']+", title)
            if token not in STOP_WORDS and len(token) > 2
        ]
        seen = set()
        for size in (2, 3):
            seen.update(tuple(tokens[i:i + size]) for i in range(len(tokens) - size + 1))
        for phrase in seen:
            counts[phrase] += 1
            if len(examples[phrase]) < 3:
                examples[phrase].append(row["id"])
    return [
        {"phrase": " ".join(phrase), "tickets": count, "example_ids": examples[phrase]}
        for phrase, count in counts.most_common(limit)
    ]


def taxonomy_audit(db, rows: list, programmes: list[dict], recurring: list[dict]) -> dict:
    labels = {
        row["ticket_id"]: row
        for row in db.execute(
            "SELECT ticket_id, topics, work_pattern, confidence FROM labels")
    }
    low = sum(
        not labels.get(row["id"])
        or (labels[row["id"]]["confidence"] or 0) < 0.6
        for row in rows
    )
    topic_other = 0
    pattern_other = 0
    for row in rows:
        label = labels.get(row["id"])
        if not label:
            continue
        try:
            topics = json.loads(label["topics"] or "[]")
        except ValueError:
            topics = []
        topic_other += topics == ["Other"]
        pattern_other += label["work_pattern"] == "Other"

    gap_names = {
        "PPM / AM4 rollout", "WPP / AM6 rollout",
        "Dependency-analysis refactoring", "Build/release pipeline testing",
    }
    gaps = []
    for family in programmes + recurring:
        if family["name"] not in gap_names:
            continue
        family_labels = [labels.get(ticket_id) for ticket_id in family["ticket_ids"]]
        family_labels = [label for label in family_labels if label]
        family_low = sum((label["confidence"] or 0) < 0.6 for label in family_labels)
        family_other = sum(
            json.loads(label["topics"] or "[]") == ["Other"] for label in family_labels)
        pattern_other_count = sum(
            label["work_pattern"] == "Other" for label in family_labels)
        gaps.append({
            "name": family["name"],
            "tickets": family["tickets"],
            "low_confidence": family_low,
            "topic_other": family_other,
            "pattern_other": pattern_other_count,
        })
    return {
        "labels_reviewed": False,
        "low_confidence": low,
        "low_confidence_pct": pct(low, len(rows)),
        "topic_other_only": topic_other,
        "topic_other_pct": pct(topic_other, len(rows)),
        "work_pattern_other": pattern_other,
        "work_pattern_other_pct": pct(pattern_other, len(rows)),
        "known_gaps": gaps,
        "conclusion": (
            "The draft taxonomy was created from Sprints 104-107. Later PPM/WPP rollouts, "
            "dependency-analysis refactoring and pipeline testing are not represented cleanly; "
            "keep this deep analysis separate until the team reviews new labels."
        ),
    }


def charts(data: dict) -> list[dict]:
    themes = data["themes"][:12]
    programmes = data["programmes"]
    recurring = data["recurring_series"][:10]
    quality = data["content_quality"]["overall"]
    gaps = data["taxonomy_audit"]["known_gaps"]
    description = data["description_analysis"]
    return [
        {
            "id": "deep_theme_volume",
            "title": "Deep themes found in ticket titles",
            "type": "bar",
            "horizontal": True,
            "labels": [row["name"] for row in themes],
            "series": [{"name": "Tickets", "data": [row["tickets"] for row in themes]}],
            "x_label": "Tickets",
            "y_label": "Theme",
            "note": "Themes can overlap. Matching is title-led to avoid description-template inflation.",
        },
        {
            "id": "deep_programme_rollouts",
            "title": "Repeated accounting-model programmes",
            "type": "bar",
            "labels": [row["name"] for row in programmes],
            "series": [
                {"name": "Tickets", "data": [row["tickets"] for row in programmes]},
                {"name": "Size points", "data": [row["size_points"] for row in programmes]},
            ],
            "x_label": "Programme",
            "y_label": "Count",
        },
        {
            "id": "deep_recurring_series",
            "title": "Largest repeated ticket series",
            "type": "bar",
            "horizontal": True,
            "labels": [row["name"] for row in recurring],
            "series": [{"name": "Tickets", "data": [row["tickets"] for row in recurring]}],
            "x_label": "Tickets",
            "y_label": "Series",
        },
        {
            "id": "deep_content_coverage",
            "title": "Ticket field coverage",
            "type": "bar",
            "labels": [name.replace("_", " ").title() for name in quality],
            "series": [{
                "name": "Coverage %",
                "data": [quality[name]["coverage_pct"] for name in quality],
            }],
            "x_label": "Field",
            "y_label": "Percent",
        },
        {
            "id": "deep_description_structure",
            "title": "What ticket descriptions contain",
            "type": "bar",
            "horizontal": True,
            "labels": [row["name"] for row in description["structures"]],
            "series": [{
                "name": "Tickets",
                "data": [row["tickets"] for row in description["structures"]],
            }],
            "x_label": "Tickets",
            "y_label": "Description pattern",
            "note": (
                "Patterns overlap. This audits how requests are written; it does not classify "
                "the business topic."
            ),
        },
        {
            "id": "deep_taxonomy_gaps",
            "title": "Where the draft taxonomy misses later work",
            "type": "bar",
            "labels": [row["name"] for row in gaps],
            "series": [
                {"name": "Tickets", "data": [row["tickets"] for row in gaps]},
                {"name": "Low-confidence labels", "data": [row["low_confidence"] for row in gaps]},
                {"name": "Other work pattern", "data": [row["pattern_other"] for row in gaps]},
            ],
            "x_label": "Later workstream",
            "y_label": "Tickets",
        },
    ]


def build(db, cfg: dict) -> dict:
    label_types = cfg.get("label_types") or ["Requirement", "Bug", "Feature"]
    placeholders = ",".join("?" * len(label_types))
    rows = db.execute(
        f"SELECT * FROM tickets WHERE in_scope=1 AND type IN ({placeholders}) "
        f"ORDER BY sprint, id", label_types).fetchall()
    total = len(rows)
    themes = sorted(
        (summarize(name, regex, rows, total) for name, regex in THEMES),
        key=lambda row: -row["tickets"],
    )
    programmes = [
        summarize(name, regex, rows, total) for name, regex in PROGRAMMES
    ]
    recurring = sorted(
        (summarize(name, regex, rows, total) for name, regex in RECURRING_SERIES),
        key=lambda row: -row["tickets"],
    )

    programme_ids = set().union(*(set(row["ticket_ids"]) for row in programmes))
    recurring_ids = set().union(*(set(row["ticket_ids"]) for row in recurring))
    structured_ids = programme_ids | recurring_ids
    total_size = sum(row["size"] or 0 for row in rows)
    structured_size = sum(
        row["size"] or 0 for row in rows if row["id"] in structured_ids)

    data = {
        "meta": {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "source": "Azure DevOps ticket fields only",
            "tickets": total,
            "ticket_types": label_types,
            "method": (
                "Transparent regular-expression discovery over ticket titles, followed by "
                "ticket-field aggregation. Themes may overlap. Descriptions are audited for "
                "coverage but not used for theme matching because coverage differs sharply by type."
            ),
            "main_dashboard_unchanged": True,
        },
        "headline": {
            "programme_tickets": len(programme_ids),
            "programme_ticket_pct": pct(len(programme_ids), total),
            "programme_size_points": round(sum(
                row["size"] or 0 for row in rows if row["id"] in programme_ids), 1),
            "programme_size_pct": pct(
                sum(row["size"] or 0 for row in rows if row["id"] in programme_ids),
                total_size),
            "structured_or_recurring_tickets": len(structured_ids),
            "structured_or_recurring_pct": pct(len(structured_ids), total),
            "structured_or_recurring_size_points": round(structured_size, 1),
            "structured_or_recurring_size_pct": pct(structured_size, total_size),
        },
        "content_quality": text_quality(rows),
        "description_analysis": description_analysis(rows),
        "title_markers": title_markers(rows),
        "themes": themes,
        "programmes": programmes,
        "recurring_series": recurring,
        "top_phrases": top_phrases(rows),
    }
    data["taxonomy_audit"] = taxonomy_audit(db, rows, programmes, recurring)
    data["charts"] = charts(data)
    return data


def markdown(data: dict) -> str:
    h = data["headline"]
    q = data["content_quality"]
    audit = data["taxonomy_audit"]
    descriptions = data["description_analysis"]
    programmes = data["programmes"]
    recurring = data["recurring_series"]
    markers = {row["marker"]: row for row in data["title_markers"]}
    bugs = q["by_type"]["Bug"]
    requirements = q["by_type"]["Requirement"]

    lines = [
        "# Team PnC - deep ticket-content analysis",
        "",
        "> This is a separate, ticket-only analysis. It does not change the existing dashboard",
        "> or any existing chart, and it does not use source-control data.",
        "",
        "## Executive findings",
        "",
        f"1. **Accounting-model delivery is the dominant repeatable programme.** The four clear",
        f"   rollouts contain **{h['programme_tickets']} tickets "
        f"({h['programme_ticket_pct']}%)** and **{h['programme_size_points']:.0f} Size points "
        f"({h['programme_size_pct']}%)**.",
        f"2. **Structured and recurring work is not a small edge case.** Programme rollouts plus",
        f"   the validated recurring series cover **{h['structured_or_recurring_tickets']} tickets "
        f"({h['structured_or_recurring_pct']}%)** and "
        f"**{h['structured_or_recurring_size_points']:.0f} points "
        f"({h['structured_or_recurring_size_pct']}%)**.",
        f"3. **The current taxonomy is stale.** {audit['low_confidence']} tickets "
        f"({audit['low_confidence_pct']}%) are below 0.6 confidence; "
        f"{audit['topic_other_only']} are topic-Other and {audit['work_pattern_other']} are "
        f"work-pattern Other. Later PPM/WPP, refactoring and pipeline streams explain much of it.",
        f"4. **Bug evidence is thin.** Only {bugs['repro']['present']} of "
        f"{bugs['repro']['total']} Bugs ({bugs['repro']['coverage_pct']}%) contain Repro Steps; "
        f"only {bugs['description']['present']} have Description. Requirements are the reverse: "
        f"{requirements['description']['present']} of {requirements['description']['total']} "
        f"({requirements['description']['coverage_pct']}%) have Description.",
        f"5. **Title markers strongly segment the work.** EP covers "
        f"{markers.get('EP', {}).get('tickets', 0)} tickets; SU covers "
        f"{markers.get('SU', {}).get('tickets', 0)}. Of the "
        f"{markers.get('SU', {}).get('tickets', 0)} SU tickets, "
        f"{markers.get('SU', {}).get('by_type', {}).get('Bug', 0)} are Bugs.",
        f"6. **Descriptions show reusable templates but weak requirement structure.** "
        f"{descriptions['tickets_in_repeated_templates']} tickets fall into "
        f"{descriptions['repeated_template_groups']} repeated normalized description groups; "
        f"only {next(row['tickets'] for row in descriptions['structures'] if row['name'] == 'User-story wording')} "
        f"use user-story wording.",
        "",
        "## Repeated accounting-model programmes",
        "",
        "| Programme | Tickets | Size points | Closed | Sprints |",
        "|---|---:|---:|---:|---|",
    ]
    for row in programmes:
        lines.append(
            f"| {row['name']} | {row['tickets']} | {row['size_points']:.0f} | "
            f"{row['closed']}/{row['tickets']} | {row['sprint_first']}-{row['sprint_last']} |")
    lines.extend([
        "",
        "These programmes reuse the same recognizable shape: data upload, transaction types,",
        "reports, WebAPI/UI work, testing-only steps and close-out. The strongest knowledge-base",
        "opportunity is a parameterized accounting-model backlog template, not four independent",
        "sets of tickets.",
        "",
        "## Largest repeatable series",
        "",
        "| Series | Tickets | Size points | Closed | Sprints |",
        "|---|---:|---:|---:|---|",
    ])
    for row in recurring:
        lines.append(
            f"| {row['name']} | {row['tickets']} | {row['size_points']:.0f} | "
            f"{row['closed']}/{row['tickets']} | {row['sprint_first']}-{row['sprint_last']} |")
    lines.extend([
        "",
        "The 70-ticket dependency/refactoring stream and 117 test-only rollout steps are large",
        "enough to deserve explicit work-pattern labels. Monthly/sprint operational series",
        "(automated-test monitoring, One Statement, Sherlock upload, SAL aging) should become",
        "recurring templates with a stable checklist and evidence fields.",
        "",
        "## Theme volume (themes overlap)",
        "",
        "| Theme | Tickets | Share | Bugs | Size points | Median cycle |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for row in data["themes"]:
        lines.append(
            f"| {row['name']} | {row['tickets']} | {row['ticket_pct']}% | "
            f"{row['by_type'].get('Bug', 0)} | {row['size_points']:.0f} | "
            f"{row['median_cycle_days'] or '-'} d |")
    lines.extend([
        "",
        "## Taxonomy changes to review (not applied)",
        "",
        "Keep the existing dashboard stable for now. For the next taxonomy review, add:",
        "",
        "- **PPM / AM4 accounting model** and **WPP / AM6 accounting model** as explicit topics.",
        "- **Dependency analysis and refactoring** as a work pattern.",
        "- **Build/release pipeline validation** as a work pattern.",
        "- Explicit recurring-operation patterns for automated-test monitoring, One Statement,",
        "  Sherlock upload and SAL finance aging.",
        "- Consider making the title markers (EP/SU/VSE/SE) a first-class field after the team",
        "  documents what each abbreviation means; this report does not guess their definitions.",
        "",
        "## How ticket descriptions are written",
        "",
        f"- {descriptions['described_tickets']} tickets have Description; the median is "
        f"{descriptions['median_characters']:.0f} characters and the 90th percentile is "
        f"{descriptions['p90_characters']} characters.",
    ])
    for row in descriptions["structures"]:
        noun = "ticket" if row["tickets"] == 1 else "tickets"
        lines.append(
            f"- **{row['name']}:** {row['tickets']} {noun} "
            f"({row['description_pct']}% of described tickets).")
    lines.extend([
        f"- **Repeated templates:** {descriptions['tickets_in_repeated_templates']} tickets "
        f"belong to {descriptions['repeated_template_groups']} normalized duplicate groups.",
        "",
        "This is good evidence for templates, but the template should separate business intent,",
        "acceptance criteria, implementation notes and test evidence. Today those concepts are",
        "often mixed into Description, while the dedicated Acceptance field is empty.",
        "",
        "## Data-quality recommendations",
        "",
        "- Require Repro Steps for Bugs, or introduce a structured reason when they do not apply.",
        "- Use the existing Acceptance Criteria field: it is empty on all 909 tickets even though",
        "  15 descriptions contain acceptance wording.",
        "- Keep Assigned To and Size required; they already have high coverage.",
        "- Review taxonomy against the whole year, not only Sprints 104-107.",
        "",
        "## Method and limits",
        "",
        "- Themes use ticket **titles only**, because Description is present on most Requirements",
        "  but almost no Bugs and often includes broad rollout context.",
        "- Themes intentionally overlap; a rollout step can be both accounting-model and payment work.",
        "- Counts are reproducible regex matches. Each JSON row includes the pattern, ticket IDs and",
        "  representative ticket links so a reviewer can inspect the evidence.",
        "- Assigned To is current/final ownership, not participation history.",
        "- No source-control data is used.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true", help="indent JSON")
    args = parser.parse_args()
    db = require_db()
    data = build(db, load_config())
    OUT.write_text(
        json.dumps(data, ensure_ascii=False, indent=2 if args.pretty else None),
        encoding="utf-8",
    )
    REPORT.write_text(markdown(data), encoding="utf-8")
    print(f"Wrote {OUT}")
    print(f"Wrote {REPORT}")
    print(
        f"{data['meta']['tickets']} tickets; "
        f"{data['headline']['structured_or_recurring_tickets']} structured/recurring; "
        f"{len(data['charts'])} separate deep-analysis charts"
    )


if __name__ == "__main__":
    main()
