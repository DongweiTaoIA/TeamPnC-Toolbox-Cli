"""Tickets each person was involved in - a separate view from the dashboard's assignee chart.

A ticket counts +1 for every distinct person assigned to the ticket itself or to any of its
child Tasks. Someone with several Tasks on one ticket still counts that ticket once.

Usage:
    python src/involvement.py
    python src/involvement.py --pretty
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json

from common import DATA, load_config, require_db

OUT = DATA / "involvement.json"


def build(db, cfg: dict) -> dict:
    types = cfg.get("label_types") or ["Requirement", "Bug", "Feature"]
    marks = ",".join("?" * len(types))
    tickets = {
        r["id"]: {"sprint": r["sprint"], "owner": (r["assigned_to"] or "").strip()}
        for r in db.execute(
            f"SELECT id, sprint, assigned_to FROM tickets WHERE in_scope = 1 AND type IN ({marks})",
            types)
    }

    # Parent links live in both the children table and tickets.parent_id; use either.
    task_people = collections.defaultdict(set)
    for parent, name in db.execute(
            """SELECT c.parent_id, t.assigned_to FROM children c JOIN tickets t ON t.id = c.child_id
               WHERE t.type = 'Task'
               UNION
               SELECT parent_id, assigned_to FROM tickets
               WHERE type = 'Task' AND parent_id IS NOT NULL"""):
        name = (name or "").strip()
        if parent in tickets and name:
            task_people[parent].add(name)

    def blank():
        return {"tickets": 0, "as_assignee": 0, "via_tasks_only": 0}

    total = collections.defaultdict(blank)
    by_sprint = collections.defaultdict(lambda: collections.defaultdict(blank))
    nobody = 0
    for tid, t in tickets.items():
        people = set(task_people[tid])
        if t["owner"]:
            people.add(t["owner"])
        if not people:
            nobody += 1
        for name in people:
            kind = "as_assignee" if name == t["owner"] else "via_tasks_only"
            for bucket in (total[name], by_sprint[t["sprint"]][name]):
                bucket["tickets"] += 1
                bucket[kind] += 1

    def rows(counts):
        return sorted(({"name": n, **v} for n, v in counts.items()),
                      key=lambda r: (-r["tickets"], r["name"]))

    return {
        "meta": {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "tickets": len(tickets),
            "ticket_types": types,
            "tickets_without_anyone": nobody,
            "rule": "A ticket counts once for each distinct person assigned to the ticket "
                    "or to one of its child Tasks.",
        },
        "total": rows(total),
        "by_sprint": [{"sprint": s, "tickets": sum(1 for t in tickets.values() if t["sprint"] == s),
                       "people": rows(by_sprint[s])}
                      for s in sorted(k for k in by_sprint if k is not None)],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true", help="indent JSON")
    args = parser.parse_args()
    data = build(require_db(), load_config())
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2 if args.pretty else None),
                   encoding="utf-8")
    print(f"Wrote {OUT}")
    for r in data["total"][:5]:
        print(f"  {r['name']}: {r['tickets']} ({r['as_assignee']} assigned + "
              f"{r['via_tasks_only']} via tasks only)")


if __name__ == "__main__":
    main()
