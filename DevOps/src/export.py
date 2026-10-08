"""Export collected data for analysis without Azure OpenAI.

    python src/export.py                     # summary + CSVs + ticket bundles
    python src/export.py --topic "county"    # bundle only tickets matching a keyword

Writes:
  data/summary.md              counts by type, sprint, month, state, evidence coverage
  data/tickets.csv             one row per ticket (labels included if present)
  data/changes.csv             linked changesets/commits and their files
  data/bundles/*.md            ticket text + evidence, split into readable chunks

The bundles are plain Markdown so a person - or an AI assistant in chat - can read them and
produce topics, summaries and dossiers without any API key.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter

from common import DATA, clip, load_config, mask, require_db, slug

BUNDLE_CHARS = 60_000


def scope_clause() -> str:
    """SQL fragment: the ticket itself plus its child tasks."""
    return "(SELECT ? UNION SELECT child_id FROM children WHERE parent_id = ?)"


def code_links(db, ticket_id: int) -> list:
    """Code links for a ticket (with changeset/commit details when they could be downloaded)."""
    return db.execute(
        """SELECT l.kind, l.ref, l.name, c.comment, c.author, c.date, c.file_count
           FROM links l LEFT JOIN changes c ON c.kind = l.kind AND c.ref = l.ref
           WHERE l.kind IN ('tfvc','git_commit','pr') AND l.ticket_id IN """ + scope_clause()
        + " ORDER BY l.kind, l.ref", (ticket_id, ticket_id)).fetchall()


def ticket_rows(db, cfg, keyword: str | None) -> list:
    types = cfg.get("label_types") or []
    rows = db.execute("SELECT * FROM tickets WHERE in_scope = 1 ORDER BY sprint, id").fetchall()
    if types:
        rows = [r for r in rows if r["type"] in types]
    if keyword:
        k = keyword.lower()
        rows = [r for r in rows
                if k in (r["title"] or "").lower() or k in (r["description"] or "").lower()
                or k in (r["tags"] or "").lower()]
    return rows


def write_summary(db, cfg) -> None:
    tickets = db.execute("SELECT * FROM tickets WHERE in_scope = 1").fetchall()
    out = ["# Collected data summary", "", f"Tickets in scope: **{len(tickets)}**", ""]

    def table(title: str, counter: Counter, by_key: bool = False) -> None:
        out.extend([f"## {title}", "", "| value | tickets |", "|---|---|"])
        items = sorted(counter.items()) if by_key else counter.most_common()
        out.extend(f"| {k} | {v} |" for k, v in items)
        out.append("")

    table("By work item type", Counter(t["type"] for t in tickets))
    table("By sprint", Counter(t["sprint"] for t in tickets), by_key=True)
    table("Created by month", Counter((t["created"] or "")[:7] or "?" for t in tickets), by_key=True)
    table("Closed by month", Counter((t["closed"] or "")[:7] or "still open" for t in tickets), by_key=True)
    table("By state", Counter(t["state"] for t in tickets))
    table("By area path", Counter(t["area"] for t in tickets))

    out.extend(["## Evidence coverage", "",
                "| type | tickets | with code | with attachments | with either |", "|---|---|---|---|---|"])
    by_type: dict[str, list[int]] = {}
    for t in tickets:
        by_type.setdefault(t["type"], []).append(t["id"])
    for ttype, ids in sorted(by_type.items()):
        code = attach = either = 0
        for tid in ids:
            has_code = db.execute(
                "SELECT 1 FROM links WHERE kind IN ('tfvc','git_commit','pr') AND ticket_id IN "
                + scope_clause() + " LIMIT 1", (tid, tid)).fetchone()
            has_att = db.execute(
                "SELECT 1 FROM attachments WHERE text IS NOT NULL AND ticket_id IN "
                + scope_clause() + " LIMIT 1", (tid, tid)).fetchone()
            code += bool(has_code)
            attach += bool(has_att)
            either += bool(has_code or has_att)
        out.append(f"| {ttype} | {len(ids)} | {code} | {attach} | {either} |")
    out.append("")

    att = db.execute("SELECT name FROM attachments WHERE text IS NOT NULL").fetchall()
    if att:
        table("Text attachments by extension",
              Counter("." + a["name"].rsplit(".", 1)[-1].lower() for a in att if "." in a["name"]))

    words = Counter()
    stop = set("the a an and or for to of in on with is are be by from at as new old not this that "
               "should will can need issue error fix update add change when after before it its into "
               "we us our you your please all any if then than but also has have was were".split())
    for t in tickets:
        for w in (t["title"] or "").lower().replace("/", " ").replace("-", " ").split():
            w = "".join(c for c in w if c.isalnum())
            if len(w) > 2 and w not in stop and not w.isdigit():
                words[w] += 1
    table("Most common title words (rough topic hint)", Counter(dict(words.most_common(60))))

    (DATA / "summary.md").write_text("\n".join(out), encoding="utf-8")
    print(f"Wrote {DATA / 'summary.md'}")


def write_csvs(db) -> None:
    rows = db.execute(
        """SELECT t.id, t.type, t.state, t.sprint, t.created, t.closed, t.area, t.iteration, t.tags,
                  t.title, t.parent_id, t.assigned_to, t.size, t.priority, t.severity,
                  l.topics, l.work_pattern, l.summary, t.url
           FROM tickets t LEFT JOIN labels l ON l.ticket_id = t.id
           WHERE t.in_scope = 1 ORDER BY t.sprint, t.id""").fetchall()
    with open(DATA / "tickets.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "type", "state", "sprint", "created", "closed", "area", "iteration", "tags",
                    "title", "parent_id", "assigned_to", "size", "priority", "severity",
                    "topics", "work_pattern", "summary",
                    "n_changes", "n_attachments", "url"])
        for r in rows:
            n_att = db.execute(
                "SELECT COUNT(*) FROM attachments WHERE text IS NOT NULL AND ticket_id IN "
                + scope_clause(), (r["id"], r["id"])).fetchone()[0]
            topics = "; ".join(json.loads(r["topics"])) if r["topics"] else ""
            w.writerow([r["id"], r["type"], r["state"], r["sprint"], (r["created"] or "")[:10],
                        (r["closed"] or "")[:10], r["area"], r["iteration"], r["tags"], r["title"],
                        r["parent_id"], r["assigned_to"], r["size"], r["priority"], r["severity"],
                        topics, r["work_pattern"], r["summary"],
                        len(code_links(db, r["id"])), n_att, r["url"]])
    print(f"Wrote {DATA / 'tickets.csv'} ({len(rows)} rows)")

    changes = db.execute("SELECT * FROM changes ORDER BY date").fetchall()
    with open(DATA / "changes.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["kind", "ref", "date", "author", "comment", "file_count", "tickets", "files"])
        for c in changes:
            tickets = [str(r["ticket_id"]) for r in db.execute(
                "SELECT ticket_id FROM links WHERE kind = ? AND ref = ?", (c["kind"], c["ref"]))]
            files = [r["path"] for r in db.execute(
                "SELECT path FROM change_files WHERE kind = ? AND ref = ? LIMIT 50", (c["kind"], c["ref"]))]
            w.writerow([c["kind"], c["ref"], (c["date"] or "")[:10], c["author"],
                        clip(c["comment"], 500), c["file_count"], "; ".join(tickets), "; ".join(files)])
    print(f"Wrote {DATA / 'changes.csv'} ({len(changes)} rows)")


def ticket_block(db, t, att_chars: int) -> str:
    parts = [f"### Ticket {t['id']} - {mask(t['title'])}",
             f"Type: {t['type']} | State: {t['state']} | Sprint: {t['sprint']} | "
             f"Created: {(t['created'] or '')[:10]} | Closed: {(t['closed'] or '')[:10] or 'open'}",
             f"Area: {t['area']} | Tags: {t['tags'] or '-'}"]
    if t["description"]:
        parts.append(f"**Description:** {clip(mask(t['description']), 1200)}")
    if t["acceptance"]:
        parts.append(f"**Acceptance:** {clip(mask(t['acceptance']), 600)}")
    if t["repro"]:
        parts.append(f"**Repro:** {clip(mask(t['repro']), 600)}")
    tasks = db.execute("SELECT id, title, state FROM tickets WHERE parent_id = ? LIMIT 20",
                       (t["id"],)).fetchall()
    if tasks:
        parts.append("**Child tasks:** " + "; ".join(f"{r['id']} {mask(r['title'])}" for r in tasks))
    for c in code_links(db, t["id"]):
        files = [r["path"] for r in db.execute(
            "SELECT path FROM change_files WHERE kind = ? AND ref = ? LIMIT 25", (c["kind"], c["ref"]))]
        label = f"changeset {c['ref']}" if c["kind"] == "tfvc" else f"{c['kind']} {c['ref']}"
        detail = f": {clip(mask(c['comment']), 300)}" if c["comment"] else " (details need Code: Read access)"
        parts.append(f"**Code {label}**{detail}" + (f"\nFiles: {', '.join(files)}" if files else ""))
    for a in db.execute(
            "SELECT name, text FROM attachments WHERE text IS NOT NULL AND ticket_id IN "
            + scope_clause() + " LIMIT 6", (t["id"], t["id"])).fetchall():
        parts.append(f"**Attachment {a['name']}:**\n```\n{clip(mask(a['text']), att_chars)}\n```")
    return "\n\n".join(parts)


def write_bundles(db, rows, att_chars: int, name: str) -> None:
    out_dir = DATA / "bundles"
    out_dir.mkdir(exist_ok=True)
    for old in out_dir.glob(f"{name}_*.md"):
        old.unlink()
    part, buf, size = 1, [], 0
    written = 0

    def flush() -> None:
        nonlocal part, buf, size, written
        if not buf:
            return
        path = out_dir / f"{name}_{part:02d}.md"
        path.write_text(f"# {name} - part {part}\n\n" + "\n\n---\n\n".join(buf), encoding="utf-8")
        print(f"Wrote {path} ({len(buf)} tickets)")
        written += len(buf)
        part, buf, size = part + 1, [], 0

    for t in rows:
        block = ticket_block(db, t, att_chars)
        if size + len(block) > BUNDLE_CHARS and buf:
            flush()
        buf.append(block)
        size += len(block)
    flush()
    print(f"{written} tickets bundled.")


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topic", help="only bundle tickets whose title/description/tags contain this word")
    ap.add_argument("--types", nargs="*", help="override work item types to bundle")
    ap.add_argument("--limit", type=int, default=0, help="bundle at most N tickets")
    ap.add_argument("--attachment-chars", type=int, default=2500, help="attachment text per ticket")
    ap.add_argument("--no-bundles", action="store_true", help="summary and CSVs only")
    args = ap.parse_args()

    db = require_db()
    if args.types:
        cfg["label_types"] = args.types
    write_summary(db, cfg)
    write_csvs(db)
    if args.no_bundles:
        return
    rows = ticket_rows(db, cfg, args.topic)
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print("No tickets matched; nothing bundled.")
        return
    write_bundles(db, rows, args.attachment_chars, slug(args.topic) if args.topic else "tickets")


if __name__ == "__main__":
    main()
