"""Step 4: write a "how we do it" dossier for one topic or work pattern.

    python src/dossier.py "Program setup"

AI first writes a note per ticket from the ticket text and code diffs, then combines the notes into
dossiers/<name>.md. The dossier is a DRAFT until an engineer reviews it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import ROOT, Ado, Llm, change_refs_for, clip, load_config, mask, require_db, slug

PROMPT_VERSION = "note-v1"
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".dll", ".exe", ".pdb", ".xlsx",
            ".xls", ".docx", ".doc", ".bin", ".snk", ".pfx", ".resx"}

NOTE_SYSTEM = """You explain how a software team implemented one Azure DevOps ticket, using the ticket text,
linked changes, code diffs and attached files (often SQL setup scripts) provided.
Be concrete: name files, database objects, config keys, screens.
Only state what the evidence shows; prefix anything inferred with "Likely:". Return JSON:
{"request": str, "how_done": str, "steps": [str], "touched": [str], "tests_or_verification": str,
 "problems_or_followups": str, "reusable_parts": str, "evidence": [str]}
evidence lists IDs like "ticket 123", "changeset 4567", "commit abc1234", "attachment setup.sql"."""

DOSSIER_SYSTEM = """You write an internal engineering dossier: how our team usually does "{name}" work,
built from per-ticket notes. Audience: an engineer picking up the next similar ticket.
Write Markdown with exactly these sections:
## Summary  (what this work is, how often we did it, typical size)
## Typical steps  (numbered, in order)
## Files, tables and config usually touched
## Variations  (what differs between tickets, and which values are parameters)
## Common problems and related bugs
## Draft checklist  (a "- [ ]" list someone can copy into the next ticket)
## Draft template  (ticket description template and/or a parameterised script outline, if the evidence supports it)
## Source tickets  (one line each: ticket ID - one-line summary)
Rules: cite ticket/changeset IDs in brackets, e.g. [ticket 123, changeset 4567], for every factual claim.
Mark anything inferred as "(guess)". Do not name or rank individual people. Do not invent file names."""


def find_tickets(db, name: str, limit: int) -> list:
    rows = db.execute(
        """SELECT t.*, l.topics, l.work_pattern, l.summary FROM tickets t JOIN labels l ON l.ticket_id = t.id
           ORDER BY COALESCE(t.closed, t.created) DESC""").fetchall()
    key = name.lower()
    matches = [r for r in rows
               if (r["work_pattern"] or "").lower() == key or key in [x.lower() for x in json.loads(r["topics"])]]
    return matches[:limit]


def file_diff(ado: Ado, db, change, path: str, change_type: str, max_chars: int) -> str | None:
    cached = db.execute("SELECT diff FROM diffs WHERE kind = ? AND ref = ? AND path = ?",
                        (change["kind"], change["ref"], path)).fetchone()
    if cached:
        return cached["diff"]
    if "delete" in change_type.lower():
        diff = "(file deleted)"
    else:
        new = ado.file_text(change["kind"], change["repo"], path, change["version"])
        old = None if "add" in change_type.lower() else \
            ado.file_text(change["kind"], change["repo"], path, change["base_version"])
        if new is None:
            diff = "(content not available or binary)"
        else:
            lines = difflib.unified_diff((old or "").splitlines(), new.splitlines(),
                                         "before", "after", lineterm="", n=3)
            diff = clip(mask("\n".join(lines)), max_chars) or "(no text changes)"
    db.execute("INSERT OR REPLACE INTO diffs VALUES (?, ?, ?, ?)", (change["kind"], change["ref"], path, diff))
    db.commit()
    return diff


def ticket_evidence(ado: Ado, db, t, dcfg: dict) -> str:
    parts = [
        f"Ticket {t['id']} [{t['type']}] {mask(t['title'])}  (state {t['state']}, sprint {t['sprint']})",
        f"AI summary: {t['summary']}",
        f"Description: {clip(mask(t['description']), 2000)}",
    ]
    if t["acceptance"]:
        parts.append(f"Acceptance criteria: {clip(mask(t['acceptance']), 1000)}")
    if t["repro"]:
        parts.append(f"Repro steps: {clip(mask(t['repro']), 1000)}")
    budget = dcfg["max_diff_chars_per_ticket"]
    att = db.execute(
        """SELECT name, text FROM attachments WHERE text IS NOT NULL AND ticket_id IN
           (SELECT ? UNION SELECT child_id FROM children WHERE parent_id = ?)""",
        (t["id"], t["id"])).fetchall()
    for a in att[:dcfg.get("max_attachments", 6)]:
        body = clip(mask(a["text"]), dcfg.get("max_attachment_chars", 4000))
        parts.append(f"\n--- attachment {a['name']}:\n{body}")
    changes = change_refs_for(db, t["id"])
    if not changes and not att:
        parts.append("No linked changesets/commits and no text attachments.")
    for c in changes:
        label = f"changeset {c['ref']}" if c["kind"] == "tfvc" else f"{c['kind']} {c['ref']}"
        files = db.execute("SELECT path, change_type FROM change_files WHERE kind = ? AND ref = ?",
                           (c["kind"], c["ref"])).fetchall()
        parts.append(f"\n--- {label} ({(c['date'] or '')[:10]}): {clip(mask(c['comment']), 400)}")
        parts.append("Files: " + ", ".join(f"{f['path']} ({f['change_type']})" for f in files[:40]))
        if ado is None or budget <= 0:
            continue
        text_files = [f for f in files if not any(f["path"].lower().endswith(e) for e in SKIP_EXT)]
        for f in text_files[:dcfg["max_files_per_change"]]:
            if budget <= 0:
                parts.append("(diff budget used up)")
                break
            diff = file_diff(ado, db, c, f["path"], f["change_type"], dcfg["max_diff_chars_per_file"])
            parts.append(f"Diff {f['path']}:\n{diff}")
            budget -= len(diff or "")
    return "\n".join(parts)


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help='work pattern or topic from topics.yaml, e.g. "Program setup"')
    ap.add_argument("--max-tickets", type=int, default=cfg["dossier"]["max_tickets"])
    ap.add_argument("--no-diffs", action="store_true", help="use file lists only, no code diffs")
    ap.add_argument("--renote", action="store_true", help="rebuild cached per-ticket notes")
    args = ap.parse_args()

    db = require_db()
    tickets = find_tickets(db, args.name, args.max_tickets)
    if not tickets:
        raise SystemExit(f'No labelled tickets with topic or work pattern "{args.name}". '
                         "Check names in topics.yaml and run `python src/categorize.py label`.")
    print(f"{len(tickets)} tickets match \"{args.name}\".")

    llm = Llm(cfg)
    ado = None if args.no_diffs else Ado(cfg)
    notes: dict[int, dict] = {}
    todo = []
    for t in tickets:
        cached = db.execute("SELECT rev, note FROM ticket_notes WHERE ticket_id = ? AND prompt_version = ?",
                            (t["id"], PROMPT_VERSION)).fetchone()
        if cached and cached["rev"] == t["rev"] and not args.renote:
            notes[t["id"]] = json.loads(cached["note"])
        else:
            todo.append(t)

    print(f"Gathering evidence for {len(todo)} tickets ...")
    evidence = {t["id"]: ticket_evidence(ado, db, t, cfg["dossier"]) for t in todo}
    revs = {t["id"]: t["rev"] for t in todo}
    with ThreadPoolExecutor(max_workers=llm.workers) as pool:
        futures = {pool.submit(llm.json, NOTE_SYSTEM, ev): tid for tid, ev in evidence.items()}
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                notes[tid] = fut.result()
            except Exception as exc:
                print(f"  ! #{tid}: {exc}")
                continue
            db.execute("INSERT OR REPLACE INTO ticket_notes VALUES (?, ?, ?, ?)",
                       (tid, revs[tid], PROMPT_VERSION, json.dumps(notes[tid])))
    db.commit()

    linked = sum(1 for t in tickets if change_refs_for(db, t["id"]))
    notes_text = "\n\n".join(
        f"### Ticket {t['id']} [{t['type']}] {mask(t['title'])}\n{json.dumps(notes[t['id']], ensure_ascii=False)}"
        for t in tickets if t["id"] in notes)
    body = llm.text(DOSSIER_SYSTEM.replace("{name}", args.name), notes_text)

    out_dir = ROOT / "dossiers"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{slug(args.name)}.md"
    sources = "\n".join(f"- [{t['id']}]({t['url']}) {t['title']}" for t in tickets)
    header = (
        f"# {args.name} - how we do it\n\n"
        f"> **DRAFT - AI generated, needs engineer review before use.**\n"
        f"> Built {dt.date.today()} from {len(tickets)} tickets ({linked} with linked code changes), "
        f"model `{llm.model}`{', file lists only' if args.no_diffs else ''}.\n"
        f"> Reviewed by: _name, date_\n\n"
    )
    path.write_text(header + body.strip() + "\n\n## Ticket links\n\n" + sources + "\n", encoding="utf-8")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
