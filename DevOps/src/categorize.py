"""Step 3: label tickets by topic and work pattern.

    python src/categorize.py rules       # no AI: apply the regexes in topics.yaml
    python src/categorize.py propose     # AI proposes topics (needs Azure OpenAI)
    python src/categorize.py label       # AI labels tickets (needs Azure OpenAI)
    python src/categorize.py review      # random sample for a person to check
    python src/categorize.py export      # data/tickets.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import random
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

from common import DATA, ROOT, Llm, change_refs_for, clip, load_config, mask, require_db

TOPICS_FILE = ROOT / "topics.yaml"
PROPOSED_FILE = ROOT / "topics.proposed.yaml"


def label_tickets(db, cfg) -> list:
    types = cfg.get("label_types") or []
    rows = db.execute("SELECT * FROM tickets WHERE in_scope = 1 ORDER BY id").fetchall()
    return [r for r in rows if not types or r["type"] in types]


def ticket_brief(t, limit: int = 300) -> str:
    desc = mask(t["description"] or t["repro"] or "").replace("\n", " ")
    return f"#{t['id']} [{t['type']}] {mask(t['title'])} | tags: {t['tags'] or '-'} | {clip(desc, limit)}"


# ---------- propose ----------

PROPOSE_SYSTEM = """You analyse Azure DevOps tickets of one software team.
Propose two independent label sets:
- topics: business/product areas the work is about (e.g. "County mode"). 10-15 items.
- work_patterns: the kind of work done (e.g. "Program setup", "Bug fix", "Report change"). 6-12 items.
Use names the team would recognise from the tickets. Definitions must be one sentence and specific
enough that two people would label the same ticket the same way. Include "Other" in both lists.
Return JSON: {"topics": [{"name", "definition", "example_ids": [int]}],
              "work_patterns": [{"name", "definition", "example_ids": [int]}]}"""


def cmd_propose(args, cfg, db) -> None:
    tickets = label_tickets(db, cfg)
    if not tickets:
        raise SystemExit("No tickets to analyse. Check label_types in config.yaml.")
    random.seed(42)
    sample = random.sample(tickets, min(args.sample, len(tickets)))
    user = "Tickets:\n" + "\n".join(ticket_brief(t) for t in sample)
    result = Llm(cfg).json(PROPOSE_SYSTEM, user)

    doc = {
        "reviewed": False,
        "topics": [{"name": x["name"], "definition": x.get("definition", "")} for x in result.get("topics", [])],
        "work_patterns": [{"name": x["name"], "definition": x.get("definition", "")}
                          for x in result.get("work_patterns", [])],
    }
    target = PROPOSED_FILE if TOPICS_FILE.exists() else TOPICS_FILE
    header = (
        "# AI-proposed labels. Edit names/definitions, merge or add items, then set reviewed: true.\n"
        f"# Proposed from {len(sample)} of {len(tickets)} tickets on {dt.date.today()}.\n"
    )
    target.write_text(header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110),
                      encoding="utf-8")
    print(f"Wrote {target.name}. Review it with the team, then set `reviewed: true` in topics.yaml.")
    for kind in ("topics", "work_patterns"):
        print(f"\n{kind}:")
        for x in result.get(kind, []):
            print(f"  - {x['name']}: {x.get('definition', '')}  e.g. {x.get('example_ids', [])[:3]}")


# ---------- label ----------

LABEL_SYSTEM = """You label one Azure DevOps ticket using ONLY the allowed labels below.
topics: 1-3 names from TOPICS (most relevant first). work_pattern: exactly one name from WORK PATTERNS.
summary: one sentence describing what was requested/fixed, based only on the ticket and its changes.
confidence: 0-1, how clearly the evidence supports the labels. Use "Other" when nothing fits.
Return JSON: {"topics": [str], "work_pattern": str, "summary": str, "confidence": number}

TOPICS:
{topics}

WORK PATTERNS:
{patterns}"""


def load_taxonomy(require_reviewed: bool = True) -> tuple[dict, str]:
    if not TOPICS_FILE.exists():
        raise SystemExit("topics.yaml not found. Run `python src/categorize.py propose` first.")
    raw = TOPICS_FILE.read_text(encoding="utf-8")
    tax = yaml.safe_load(raw)
    if require_reviewed and not tax.get("reviewed"):
        raise SystemExit("topics.yaml is not reviewed yet. Edit it with the team, then set `reviewed: true`.")
    return tax, hashlib.sha1(raw.encode()).hexdigest()[:12]


def cmd_rules(args, cfg, db) -> None:
    """Label with the regexes in topics.yaml - no Azure OpenAI needed."""
    tax, tax_hash = load_taxonomy(require_reviewed=not args.draft)
    compiled = {kind: [(x["name"], [re.compile(p, re.I) for p in (x.get("match") or [])])
                       for x in tax[kind]] for kind in ("topics", "work_patterns")}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    tickets = label_tickets(db, cfg)
    for t in tickets:
        hay = " ".join(filter(None, [t["title"], t["tags"], (t["description"] or "")[:1500],
                                     (t["acceptance"] or "")[:500], (t["repro"] or "")[:500]]))
        hits = {kind: [(name, sum(1 for r in pats if r.search(hay)))
                       for name, pats in compiled[kind]] for kind in compiled}
        topics = [n for n, c in sorted(hits["topics"], key=lambda x: -x[1]) if c][:3] or ["Other"]
        # Work pattern is single-valued, so the first matching entry in topics.yaml wins:
        # list them from most specific to most general.
        pattern = next((n for n, c in hits["work_patterns"] if c), "Other")
        matched = sum(1 for _, c in hits["topics"] if c)
        db.execute("INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (t["id"], t["rev"], json.dumps(topics), pattern,
                    "", round(min(matched, 3) / 3, 2), tax_hash, "rules", now))
    db.commit()
    print(f"Labelled {len(tickets)} tickets with rules from topics.yaml.")
    export_csv(db)
    cmd_summary(db)


def ticket_detail(db, t) -> str:
    parts = [
        f"ID: {t['id']}  Type: {t['type']}  State: {t['state']}",
        f"Title: {mask(t['title'])}",
        f"Area: {t['area']}  Tags: {t['tags'] or '-'}",
        f"Description: {clip(mask(t['description']), 1500)}",
    ]
    if t["acceptance"]:
        parts.append(f"Acceptance criteria: {clip(mask(t['acceptance']), 800)}")
    if t["repro"]:
        parts.append(f"Repro steps: {clip(mask(t['repro']), 800)}")
    tasks = db.execute("SELECT title FROM tickets WHERE parent_id = ? LIMIT 15", (t["id"],)).fetchall()
    if tasks:
        parts.append("Child tasks: " + "; ".join(mask(r["title"]) for r in tasks))
    att = db.execute(
        """SELECT name FROM attachments WHERE text IS NOT NULL AND ticket_id IN
           (SELECT ? UNION SELECT child_id FROM children WHERE parent_id = ?) LIMIT 20""",
        (t["id"], t["id"])).fetchall()
    if att:
        parts.append("Attachments: " + ", ".join(a["name"] for a in att))
    changes = change_refs_for(db, t["id"])[:10]
    for c in changes:
        files = [r["path"] for r in db.execute(
            "SELECT path FROM change_files WHERE kind = ? AND ref = ? LIMIT 15", (c["kind"], c["ref"]))]
        parts.append(f"Change {c['kind']} {c['ref']}: {clip(mask(c['comment']), 200)} | files: {', '.join(files)}")
    return "\n".join(parts)


def cmd_label(args, cfg, db) -> None:
    tax, tax_hash = load_taxonomy()
    topic_names = {x["name"] for x in tax["topics"]} | {"Other"}
    pattern_names = {x["name"] for x in tax["work_patterns"]} | {"Other"}
    topic_lookup = {name.lower(): name for name in topic_names}
    pattern_lookup = {name.lower(): name for name in pattern_names}
    fmt = lambda items: "\n".join(f"- {x['name']}: {x.get('definition', '')}" for x in items)
    system = LABEL_SYSTEM.replace("{topics}", fmt(tax["topics"])).replace("{patterns}", fmt(tax["work_patterns"]))

    todo = []
    for t in label_tickets(db, cfg):
        done = db.execute("SELECT rev, taxonomy_hash FROM labels WHERE ticket_id = ?", (t["id"],)).fetchone()
        if args.force or not done or done["rev"] != t["rev"] or done["taxonomy_hash"] != tax_hash:
            todo.append(t)
    if args.limit:
        todo = todo[:args.limit]
    print(f"Labelling {len(todo)} tickets ...")
    if not todo:
        return cmd_summary(db)

    llm = Llm(cfg)
    prompts = {t["id"]: ticket_detail(db, t) for t in todo}
    revs = {t["id"]: t["rev"] for t in todo}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    failed = 0
    with ThreadPoolExecutor(max_workers=llm.workers) as pool:
        futures = {pool.submit(llm.json, system, prompts[tid]): tid for tid in prompts}
        for n, fut in enumerate(as_completed(futures), 1):
            tid = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:
                failed += 1
                print(f"  ! #{tid}: {exc}")
                continue
            # Match case-insensitively and fall back to Other so labels stay within the reviewed list.
            topics = [topic_lookup[x.lower()] for x in res.get("topics", [])
                      if str(x).lower() in topic_lookup][:3] or ["Other"]
            pattern = pattern_lookup.get(str(res.get("work_pattern", "")).lower(), "Other")
            db.execute("INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (tid, revs[tid], json.dumps(topics), pattern, res.get("summary", ""),
                        float(res.get("confidence") or 0), tax_hash, llm.model, now))
            if n % 25 == 0:
                db.commit()
                print(f"  {n}/{len(todo)}")
    db.commit()
    if failed:
        print(f"{failed} tickets failed; re-run to retry them.")
    export_csv(db)
    cmd_summary(db)


# ---------- review / export / summary ----------

def export_csv(db) -> None:
    rows = db.execute(
        """SELECT t.id, t.type, t.state, t.sprint, t.created, t.closed, t.area, t.tags, t.title,
                  l.topics, l.work_pattern, l.summary, l.confidence, t.url
           FROM tickets t LEFT JOIN labels l ON l.ticket_id = t.id
           WHERE t.in_scope = 1 ORDER BY t.id""").fetchall()
    path = DATA / "tickets.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "type", "state", "sprint", "created", "closed", "area", "tags", "title",
                    "topics", "work_pattern", "ai_summary", "ai_confidence", "linked_changes", "url"])
        for r in rows:
            topics = "; ".join(json.loads(r["topics"])) if r["topics"] else ""
            n_changes = len(change_refs_for(db, r["id"]))
            w.writerow([r["id"], r["type"], r["state"], r["sprint"], (r["created"] or "")[:10],
                        (r["closed"] or "")[:10], r["area"], r["tags"], r["title"], topics,
                        r["work_pattern"], r["summary"], r["confidence"], n_changes, r["url"]])
    print(f"Wrote {path}")


def cmd_review(args, cfg, db) -> None:
    rows = db.execute(
        """SELECT t.id, t.type, t.title, t.url, l.topics, l.work_pattern, l.summary, l.confidence
           FROM labels l JOIN tickets t ON t.id = l.ticket_id""").fetchall()
    if not rows:
        raise SystemExit("No labels yet. Run `python src/categorize.py label` first.")
    random.seed(args.seed)
    sample = random.sample(rows, min(args.n, len(rows)))
    path = DATA / "review_sample.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "type", "title", "ai_topics", "ai_work_pattern", "ai_summary", "ai_confidence",
                    "url", "topics_correct (y/n)", "pattern_correct (y/n)", "correct_labels", "notes"])
        for r in sample:
            w.writerow([r["id"], r["type"], r["title"], "; ".join(json.loads(r["topics"])), r["work_pattern"],
                        r["summary"], r["confidence"], r["url"], "", "", "", ""])
    print(f"Wrote {path} with {len(sample)} tickets. Fill in the last four columns.")


def cmd_summary(db) -> None:
    rows = db.execute("SELECT topics, work_pattern, confidence FROM labels").fetchall()
    if not rows:
        return
    topics = Counter(x for r in rows for x in json.loads(r["topics"]))
    patterns = Counter(r["work_pattern"] for r in rows)
    low = sum(1 for r in rows if (r["confidence"] or 0) < 0.6)
    print(f"\nLabelled tickets: {len(rows)}  (low confidence < 0.6: {low})")
    print("\nTopics (a ticket can have several):")
    for k, v in topics.most_common():
        print(f"  {k:<35} {v:>5}")
    print("\nWork patterns:")
    for k, v in patterns.most_common():
        print(f"  {k:<35} {v:>5}")


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("rules", help="label with topics.yaml regexes (no Azure OpenAI)")
    p.add_argument("--draft", action="store_true", help="allow a topics.yaml that is not reviewed yet")
    p = sub.add_parser("propose", help="AI proposes topics and work patterns")
    p.add_argument("--sample", type=int, default=200)
    p = sub.add_parser("label", help="AI labels tickets using the reviewed topics.yaml")
    p.add_argument("--limit", type=int, default=0, help="label only N tickets (for a trial run)")
    p.add_argument("--force", action="store_true", help="re-label tickets already labelled")
    p = sub.add_parser("review", help="random sample for human checking")
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--seed", type=int, default=7)
    sub.add_parser("export", help="write data/tickets.csv")
    sub.add_parser("summary", help="print label counts")
    args = ap.parse_args()

    db = require_db()
    if args.cmd == "rules":
        cmd_rules(args, cfg, db)
    elif args.cmd == "propose":
        cmd_propose(args, cfg, db)
    elif args.cmd == "label":
        cmd_label(args, cfg, db)
    elif args.cmd == "review":
        cmd_review(args, cfg, db)
    elif args.cmd == "export":
        export_csv(db)
    else:
        cmd_summary(db)


if __name__ == "__main__":
    main()
