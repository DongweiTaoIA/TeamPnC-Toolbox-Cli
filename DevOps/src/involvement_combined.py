"""Tickets involving each person across both boards (PnC Admin + IFRS).

Uses the same rule as involvement.py, per board, then adds the boards together. Sprints are
paired by their position in the year (PnC Admin 104 with IFRS 133, 105 with 134, ...).
A board whose database has not been collected yet is skipped with a warning.

Usage:
    python src/involvement_combined.py
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json

from common import ROOT, connect, data_dir, load_config
from involvement import build

BOARDS = [("", "PnC Admin"), ("ifrs", "IFRS")]
OUT = ROOT / "data" / "involvement-combined.json"


def combine(boards: list[dict]) -> dict:
    def blank():
        return {"tickets": 0, "as_assignee": 0, "via_tasks_only": 0,
                "by_board": {b["name"]: 0 for b in boards}}

    def add(bucket, row, board):
        for k in ("tickets", "as_assignee", "via_tasks_only"):
            bucket[k] += row[k]
        bucket["by_board"][board] += row["tickets"]

    total = collections.defaultdict(blank)
    slots = collections.defaultdict(lambda: {"sprints": {}, "tickets": {},
                                             "people": collections.defaultdict(blank)})
    for b in boards:
        for row in b["data"]["total"]:
            add(total[row["name"]], row, b["name"])
        for s in b["data"]["by_sprint"]:
            slot = slots[s["sprint"] - b["first"] + 1]
            slot["sprints"][b["name"]] = s["sprint"]
            slot["tickets"][b["name"]] = s["tickets"]
            for row in s["people"]:
                add(slot["people"][row["name"]], row, b["name"])

    def rows(counts):
        return sorted(({"name": n, **v} for n, v in counts.items()),
                      key=lambda r: (-r["tickets"], r["name"]))

    return {
        "meta": {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "boards": [{"name": b["name"], "first_sprint": b["first"],
                        "tickets": b["data"]["meta"]["tickets"]} for b in boards],
            "rule": boards[0]["data"]["meta"]["rule"] if boards else "",
        },
        "total": rows(total),
        "by_sprint": [{"index": i, "sprints": slots[i]["sprints"], "tickets": slots[i]["tickets"],
                       "people": rows(slots[i]["people"])} for i in sorted(slots) if i >= 1],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true", help="indent JSON")
    args = parser.parse_args()
    boards = []
    for key, name in BOARDS:
        path = data_dir(key) / "tickets.db"
        if not path.exists():
            print(f"  ! skipping {name}: {path} not found (run collect.py for this board)")
            continue
        cfg = load_config(key)
        boards.append({"name": name, "first": cfg["sprint_first"], "data": build(connect(path), cfg)})
    data = combine(boards)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2 if args.pretty else None),
                   encoding="utf-8")
    print(f"Wrote {OUT}")
    for r in data["total"][:5]:
        print(f"  {r['name']}: {r['tickets']} {r['by_board']}")


if __name__ == "__main__":
    main()
