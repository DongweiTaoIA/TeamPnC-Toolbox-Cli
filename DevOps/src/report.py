"""Emit dashboard-ready JSON for an external web app.

This tool does analysis only - it renders nothing. It writes one self-describing
JSON document that a separate dashboard app can fetch and display.

Usage:
    python src/report.py
    python src/report.py --out data/dashboard.json --pretty
    python src/report.py --split          # also write one file per section
"""

import argparse
import collections
import datetime as dt
import json
import re
import sqlite3
import sys

from common import DATA, DB_PATH as DB, ROOT, load_config
SCHEMA_VERSION = 1

# Step numbers in the DW accounting model rollouts group into these phases (DW2 5xx numbering).
PHASES = [
    (501, 529, "Data upload"),
    (530, 549, "Transaction types"),
    (550, 554, "Reports"),
    (555, 559, "WebAPI"),
    (560, 569, "UI"),
    (570, 599, "Close-out"),
]


def phase_of(step):
    for lo, hi, name in PHASES:
        if lo <= step <= hi:
            return name
    return "Other"


_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


def parse_ts(s):
    """Azure DevOps timestamps vary in fractional digits, so parse the fixed prefix."""
    if not s:
        return None
    m = _TS.match(s)
    if not m:
        return None
    return dt.datetime(*(int(x) for x in m.groups()), tzinfo=dt.timezone.utc)


def percentile(values, pct):
    if not values:
        return None
    vs = sorted(values)
    if len(vs) == 1:
        return round(vs[0], 1)
    pos = (len(vs) - 1) * pct
    lo = int(pos)
    hi = min(lo + 1, len(vs) - 1)
    return round(vs[lo] + (vs[hi] - vs[lo]) * (pos - lo), 1)


def chart(cid, title, ctype, labels, series, **kw):
    """A renderer-agnostic chart spec: labels on one axis, one or more named series."""
    spec = {"id": cid, "title": title, "type": ctype, "labels": labels, "series": series}
    spec.update(kw)
    return spec


def pie(cid, title, pairs, limit=None, note=None):
    """A pie spec with percentages.

    When the list is truncated the remainder is folded into an explicit "Other" slice, so the
    percentages always add up to 100 and the chart is not quietly missing a chunk of the data.
    """
    rows = list(pairs)
    total = sum(r["count"] for r in rows)
    if limit and len(rows) > limit:
        head, tail = rows[:limit], rows[limit:]
        rows = head + [{"name": f"Other ({len(tail)} more)",
                        "count": sum(r["count"] for r in tail)}]
    counts = [r["count"] for r in rows]
    spec = chart(cid, title, "pie",
                 [r["name"] for r in rows],
                 [{"name": title, "data": counts}],
                 **({"note": note} if note else {}))
    spec["total"] = total
    spec["percent"] = [round(100 * c / total, 1) if total else 0 for c in counts]
    return spec


def bar(cid, title, pairs, x_label=None, y_label="Tickets", limit=None, horizontal=False):
    rows = pairs[:limit] if limit else pairs
    return chart(cid, title, "bar",
                 [r["name"] for r in rows],
                 [{"name": y_label, "data": [r["count"] for r in rows]}],
                 x_label=x_label, y_label=y_label, horizontal=horizontal)


def rows_to_pairs(rows):
    return [{"name": k, "count": v} for k, v in rows]


class Report:
    def __init__(self, conn, cfg):
        self.c = conn
        self.cfg = cfg
        self.label_types = cfg.get("label_types") or ["Requirement", "Bug", "Feature"]
        self.child_types = cfg.get("child_types") or []
        qm = ",".join("?" * len(self.label_types))
        self.real = f"in_scope=1 AND type IN ({qm})"
        self.rt = self.label_types
        self.dup_prs = self._mark_duplicate_prs()
        self.alias = {}
        for keep, variants in (cfg.get("author_aliases") or {}).items():
            for v in variants or []:
                self.alias[str(v).strip().lower()] = keep

    def canon_author(self, name):
        return self.alias.get((name or "").strip().lower(), name)

    def _mark_duplicate_prs(self):
        """A pull request and its merge commit are the same work recorded twice.

        Azure DevOps links both to a ticket, so counting them naively inflates anyone who
        works in Git (TFVC check-ins have no equivalent double entry). Keep the commit,
        which is the canonical change, and exclude the pull request from all code counts.
        """
        commits = {ref.split("/", 1)[1] for (ref,) in
                   self.q("SELECT ref FROM changes WHERE kind = 'git_commit'")}
        dups = [ref for ref, ver in
                self.q("SELECT ref, version FROM changes WHERE kind = 'pr' AND version IS NOT NULL")
                if ver in commits]
        self.c.execute("CREATE TEMP TABLE IF NOT EXISTS dup_pr (ref TEXT PRIMARY KEY)")
        self.c.execute("DELETE FROM dup_pr")
        self.c.executemany("INSERT OR IGNORE INTO dup_pr VALUES (?)", [(r,) for r in dups])
        return len(dups)

    # Reusable predicate: a change row that is not a duplicated pull request.
    UNIQUE = "NOT (kind = 'pr' AND ref IN (SELECT ref FROM dup_pr))"

    def uniq_on(self, alias):
        """UNIQUE, qualified for a joined query where `changes` carries a table alias."""
        return f"NOT ({alias}.kind = 'pr' AND {alias}.ref IN (SELECT ref FROM dup_pr))"

    def q(self, sql, params=()):
        return self.c.execute(sql, params).fetchall()

    # ---------- sections ----------

    def meta(self):
        total = self.q("SELECT COUNT(*) FROM tickets")[0][0]
        real = self.q(f"SELECT COUNT(*) FROM tickets WHERE {self.real}", self.rt)[0][0]
        sprints = self.q(f"SELECT MIN(sprint), MAX(sprint) FROM tickets WHERE {self.real}", self.rt)[0]
        fetched = self.q("SELECT MAX(fetched_at) FROM tickets")[0][0]
        errs = self.q("SELECT COUNT(*) FROM changes WHERE COALESCE(error,'')<>''")[0][0]
        unreviewed = self.q(
            "SELECT COUNT(*) FROM labels WHERE confidence < 0.6")[0][0]
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "source": {
                "organization": self.cfg.get("organization"),
                "project": self.cfg.get("project"),
                "iteration_root": self.cfg.get("iteration_root"),
            },
            "last_collected_at": fetched,
            "sprint_first": sprints[0],
            "sprint_last": sprints[1],
            "work_items_collected": total,
            "tickets": real,
            "ticket_types": self.label_types,
            "child_types": self.child_types,
            "data_quality": {
                "changes_with_errors": errs,
                "low_confidence_labels": unreviewed,
                "taxonomy_reviewed": bool(self.cfg.get("_taxonomy_reviewed")),
            },
        }

    def totals(self):
        by_type = self.q(
            "SELECT type, COUNT(*) FROM tickets WHERE in_scope=1 GROUP BY type ORDER BY 2 DESC")
        by_state = self.q(
            f"SELECT state, COUNT(*) FROM tickets WHERE {self.real} GROUP BY state ORDER BY 2 DESC", self.rt)
        real_by_type = self.q(
            f"SELECT type, COUNT(*) FROM tickets WHERE {self.real} GROUP BY type ORDER BY 2 DESC", self.rt)
        closed = self.q(
            f"SELECT COUNT(*) FROM tickets WHERE {self.real} AND state IN ('Closed','Resolved')", self.rt)[0][0]
        real = sum(n for _, n in real_by_type)
        bugs = dict(real_by_type).get("Bug", 0)
        reqs = dict(real_by_type).get("Requirement", 0)
        return {
            "work_items_by_type": rows_to_pairs(by_type),
            "tickets_by_type": rows_to_pairs(real_by_type),
            "tickets_by_state": rows_to_pairs(by_state),
            "tickets_total": real,
            "tickets_closed": closed,
            "closed_pct": round(100 * closed / real, 1) if real else 0,
            "requirement_to_bug_ratio": round(reqs / bugs, 2) if bugs else None,
        }

    def sprints(self):
        out = {}
        for sp, ttype, n in self.q(
                f"SELECT sprint, type, COUNT(*) FROM tickets WHERE {self.real} "
                f"GROUP BY sprint, type", self.rt):
            d = out.setdefault(sp, {"sprint": sp, "tickets": 0, "by_type": {}})
            d["tickets"] += n
            d["by_type"][ttype] = n
        for sp, n in self.q(
                f"SELECT sprint, COUNT(*) FROM tickets WHERE {self.real} "
                f"AND state IN ('Closed','Resolved') GROUP BY sprint", self.rt):
            if sp in out:
                out[sp]["closed"] = n
        for d in out.values():
            d.setdefault("closed", 0)
        return [out[k] for k in sorted(out)]

    def monthly(self):
        created = dict(self.q(
            f"SELECT substr(created,1,7), COUNT(*) FROM tickets WHERE {self.real} "
            f"AND created IS NOT NULL GROUP BY 1", self.rt))
        closed = dict(self.q(
            f"SELECT substr(closed,1,7), COUNT(*) FROM tickets WHERE {self.real} "
            f"AND closed IS NOT NULL GROUP BY 1", self.rt))
        months = sorted(set(created) | set(closed))
        return [{"month": m, "created": created.get(m, 0), "closed": closed.get(m, 0)} for m in months]

    def labels(self):
        rows = self.q(
            f"SELECT l.topics, l.work_pattern, l.confidence, t.type FROM labels l "
            f"JOIN tickets t ON t.id = l.ticket_id "
            f"WHERE t.in_scope=1 AND t.type IN ({','.join('?' * len(self.rt))})",
            self.rt)
        topics = collections.Counter()
        topic_by_type = collections.defaultdict(collections.Counter)
        patterns = collections.Counter()
        conf = []
        for tjson, pattern, c, ttype in rows:
            try:
                ts = json.loads(tjson or "[]")
            except ValueError:
                ts = []
            for t in ts:
                topics[t] += 1
                topic_by_type[t][ttype] += 1
            if pattern:
                patterns[pattern] += 1
            if c is not None:
                conf.append(c)
        return {
            "topics": [
                {"name": k, "count": v, "by_type": dict(topic_by_type[k])}
                for k, v in topics.most_common()
            ],
            "work_patterns": rows_to_pairs(patterns.most_common()),
            "labelled": len(rows),
            "low_confidence": sum(1 for c in conf if c < 0.6),
            "mean_confidence": round(sum(conf) / len(conf), 3) if conf else None,
        }

    def evidence(self):
        scope = "(ticket_id = ? OR ticket_id IN (SELECT child_id FROM children WHERE parent_id = ?))"
        out = []
        for ttype, in self.q(
                f"SELECT DISTINCT type FROM tickets WHERE {self.real} ORDER BY type", self.rt):
            ids = [r[0] for r in self.q(
                "SELECT id FROM tickets WHERE in_scope=1 AND type = ?", (ttype,))]
            code = attach = either = 0
            for tid in ids:
                hc = self.q(f"SELECT 1 FROM links WHERE kind IN ('tfvc','git_commit','pr') "
                            f"AND {scope} LIMIT 1", (tid, tid))
                ha = self.q(f"SELECT 1 FROM attachments WHERE text IS NOT NULL AND {scope} LIMIT 1",
                            (tid, tid))
                code += bool(hc)
                attach += bool(ha)
                either += bool(hc or ha)
            out.append({
                "type": ttype, "tickets": len(ids), "with_code": code,
                "with_attachment": attach, "with_either": either,
                "coverage_pct": round(100 * either / len(ids), 1) if ids else 0,
            })
        return out

    def people(self):
        """Ticket ownership from Azure DevOps Assigned To, independent of source control."""
        rows = self.q(
            f"SELECT assigned_to, size FROM tickets WHERE {self.real}", self.rt)
        assigned = collections.defaultdict(lambda: {"tickets": 0, "size_points": 0.0})
        unassigned = 0
        for name, size in rows:
            name = (name or "").strip()
            if not name:
                unassigned += 1
                continue
            assigned[name]["tickets"] += 1
            assigned[name]["size_points"] += size or 0

        people = [
            {
                "name": name,
                "tickets": values["tickets"],
                "size_points": round(values["size_points"], 1),
            }
            for name, values in assigned.items()
        ]
        people.sort(key=lambda r: (-r["tickets"], -r["size_points"], r["name"]))
        return {
            "assigned": people,
            "tickets_total": len(rows),
            "tickets_assigned": len(rows) - unassigned,
            "tickets_unassigned": unassigned,
            "coverage_pct": round(100 * (len(rows) - unassigned) / len(rows), 1) if rows else 0,
        }

    def code(self):
        uniq = self.UNIQUE
        # change_files rows belong to a change, so they need the same de-duplication.
        cf = (f"SELECT path FROM change_files WHERE "
              f"NOT (kind = 'pr' AND ref IN (SELECT ref FROM dup_pr))")

        by_kind = self.q(
            f"SELECT kind, COUNT(*), SUM(COALESCE(file_count,0)) FROM changes "
            f"WHERE {uniq} GROUP BY kind")
        authors_raw = self.q(
            f"SELECT author, COUNT(*), SUM(COALESCE(file_count,0)) FROM changes "
            f"WHERE COALESCE(author,'')<>'' AND {uniq} GROUP BY author")
        merged = collections.defaultdict(lambda: [0, 0])
        for a, n, f in authors_raw:
            m = merged[self.canon_author(a)]
            m[0] += n
            m[1] += f or 0
        by_kind_author = collections.defaultdict(collections.Counter)
        for a, k, n in self.q(
                f"SELECT author, kind, COUNT(*) FROM changes "
                f"WHERE COALESCE(author,'')<>'' AND {uniq} GROUP BY author, kind"):
            by_kind_author[self.canon_author(a)][k] += n
        # Tickets worked, not check-ins: a change is linked to a ticket or to one of its
        # child tasks, so roll child links up to the parent before counting distinct tickets.
        ticket_authors = collections.defaultdict(set)
        for a, tid in self.q(
                f"SELECT c.author, COALESCE(ch.parent_id, l.ticket_id) "
                f"FROM changes c "
                f"JOIN links l ON l.kind = c.kind AND l.ref = c.ref "
                f"LEFT JOIN children ch ON ch.child_id = l.ticket_id "
                f"WHERE COALESCE(c.author,'')<>'' AND {self.uniq_on('c')} "
                f"AND COALESCE(ch.parent_id, l.ticket_id) IN "
                f"(SELECT id FROM tickets WHERE {self.real})", self.rt):
            ticket_authors[self.canon_author(a)].add(tid)
        # Size of each ticket, so a person's tickets can be weighted by the team's own
        # estimate. Unsized tickets contribute nothing, so points always trail ticket count.
        ticket_size = {tid: sz for tid, sz in self.q(
            f"SELECT id, size FROM tickets WHERE {self.real} AND size IS NOT NULL", self.rt)}

        def points(name):
            return round(sum(ticket_size.get(t, 0) for t in ticket_authors.get(name, ())), 1)

        # How much of the backlog these per-person numbers actually speak for. Tickets
        # delivered without a code change - analysis, testing, sign-off - have no author.
        covered = set().union(*ticket_authors.values()) if ticket_authors else set()
        tickets_total = self.q(f"SELECT COUNT(*) FROM tickets WHERE {self.real}", self.rt)[0][0]
        author_coverage = {
            "tickets_total": tickets_total,
            "tickets_with_author": len(covered),
            "coverage_pct": round(100 * len(covered) / tickets_total, 1) if tickets_total else 0,
            "size_points_covered": round(sum(ticket_size.get(t, 0) for t in covered), 1),
            "shared_tickets": sum(
                1 for t in covered
                if sum(1 for s in ticket_authors.values() if t in s) > 1),
        }

        # Ordered by tickets worked: the clearest measure of how much distinct work a person
        # was involved in. Check-in and file counts reward commit style and are kept only as
        # secondary detail.
        authors = sorted(((a, n, f) for a, (n, f) in merged.items()),
                         key=lambda r: (-len(ticket_authors.get(r[0], ())), -r[2], -r[1]))[:25]
        files = self.q(
            f"SELECT path, COUNT(*) FROM ({cf}) GROUP BY path ORDER BY 2 DESC LIMIT 50")
        # Roll file paths up to a folder so the dashboard can show hot areas.
        folders = collections.Counter()
        exts = collections.Counter()
        for (path,) in self.q(cf):
            p = (path or "").replace("\\", "/").lstrip("$/")
            parts = p.split("/")
            if len(parts) > 1:
                folders["/".join(parts[:3])] += 1
            if "." in parts[-1]:
                exts["." + parts[-1].rsplit(".", 1)[-1].lower()] += 1
        return {
            "changes_by_kind": [
                {"kind": k, "changes": n, "files": f or 0} for k, n, f in by_kind],
            "changes_total": sum(n for _, n, _ in by_kind),
            "files_total": self.q(f"SELECT COUNT(*) FROM ({cf})")[0][0],
            "distinct_files": self.q(f"SELECT COUNT(DISTINCT path) FROM ({cf})")[0][0],
            "duplicate_prs_excluded": self.dup_prs,
            "author_coverage": author_coverage,
            "top_authors": [
                {"name": a, "tickets": len(ticket_authors.get(a, ())), "size_points": points(a),
                 "count": n, "files": f or 0, "by_kind": dict(by_kind_author.get(a, {}))}
                for a, n, f in authors],
            "top_files": rows_to_pairs(files),
            "top_folders": rows_to_pairs(folders.most_common(40)),
            "file_extensions": rows_to_pairs(exts.most_common(30)),
        }

    def links_and_attachments(self):
        links = self.q("SELECT kind, COUNT(*) FROM links GROUP BY kind ORDER BY 2 DESC")
        exts = collections.Counter()
        for (name,) in self.q("SELECT name FROM attachments WHERE text IS NOT NULL"):
            if name and "." in name:
                exts["." + name.rsplit(".", 1)[-1].lower()] += 1
        return {
            "links_by_kind": rows_to_pairs(links),
            "text_attachments_by_extension": rows_to_pairs(exts.most_common()),
            "text_attachments": sum(exts.values()),
        }

    def rollout(self):
        """The DW2 (5xx) -> DW3 (6xx) repeated sequence, as dashboard rows."""
        steps = {}
        for wid, title, state, sprint in self.q(
                f"SELECT id, title, state, sprint FROM tickets WHERE {self.real}", self.rt):
            m = re.search(r"\b(\d{3})\b", title or "")
            if m:
                steps.setdefault(int(m.group(1)), []).append((wid, title or "", state, sprint))
        prefix = re.compile(
            r"^\s*\(EP\):\s*DW Accounting Model\s*-\s*New DW\d+ AM\s*-\s*\d{3}\s*-\s*", re.I)
        pairs = []
        for n in sorted(steps):
            if 500 <= n <= 599 and n + 100 in steps:
                a, b = steps[n][0], steps[n + 100][0]
                pairs.append({
                    "step": n,
                    "phase": phase_of(n),
                    "description": prefix.sub("", a[1]).strip(),
                    "dw2": {"id": a[0], "sprint": a[3], "state": a[2]},
                    "dw3": {"id": b[0], "sprint": b[3], "state": b[2]},
                })
        by_phase = collections.Counter(p["phase"] for p in pairs)
        return {
            "name": "DW accounting model rollout",
            "matched_steps": len(pairs),
            "dw2_steps": sum(1 for k in steps if 500 <= k <= 599),
            "dw3_steps": sum(1 for k in steps if 600 <= k <= 699),
            "phases": [{"name": n, "steps": c} for n, c in by_phase.most_common()],
            "steps": pairs,
        }

    def cycle_time(self):
        """Days from created to closed - how long work actually took."""
        rows = self.q(
            f"SELECT type, sprint, created, closed FROM tickets WHERE {self.real} "
            f"AND created IS NOT NULL AND closed IS NOT NULL", self.rt)
        all_days, by_type, by_sprint = [], collections.defaultdict(list), collections.defaultdict(list)
        for ttype, sprint, created, closed in rows:
            a, b = parse_ts(created), parse_ts(closed)
            if not a or not b:
                continue
            days = (b - a).total_seconds() / 86400
            if days < 0:
                continue
            all_days.append(days)
            by_type[ttype].append(days)
            by_sprint[sprint].append(days)

        def stats(v):
            return {
                "count": len(v),
                "median_days": percentile(v, 0.5),
                "p90_days": percentile(v, 0.9),
                "mean_days": round(sum(v) / len(v), 1) if v else None,
            }

        buckets = [(0, 7, "0-7 days"), (7, 14, "1-2 weeks"), (14, 30, "2-4 weeks"),
                   (30, 90, "1-3 months"), (90, 180, "3-6 months"), (180, 10 ** 6, "6+ months")]
        hist = []
        for lo, hi, name in buckets:
            hist.append({"name": name, "count": sum(1 for d in all_days if lo <= d < hi)})

        return {
            "overall": stats(all_days),
            "by_type": [dict(type=k, **stats(v)) for k, v in
                        sorted(by_type.items(), key=lambda kv: -len(kv[1]))],
            "by_sprint": [dict(sprint=k, **stats(v)) for k, v in sorted(by_sprint.items())],
            "histogram": hist,
            "note": "Created-to-closed elapsed days. Includes time in backlog, so it is lead "
                    "time rather than hands-on effort.",
        }

    def tags(self):
        counter = collections.Counter()
        for (tags,) in self.q(f"SELECT tags FROM tickets WHERE {self.real}", self.rt):
            for t in (tags or "").split(";"):
                t = t.strip()
                if t:
                    # Some tags were typed with an en/em dash and others with a hyphen
                    # (e.g. "ACCP - Ready for testing"), which would otherwise split one
                    # tag into two slices.
                    counter[t.replace("\u2013", "-").replace("\u2014", "-")] += 1
        return rows_to_pairs(counter.most_common(30))

    def topic_trend(self):
        """Topic counts per sprint - a stacked area or heatmap of what the team focused on."""
        lab = {}
        for tid, tjson in self.q("SELECT ticket_id, topics FROM labels"):
            try:
                lab[tid] = json.loads(tjson or "[]")
            except ValueError:
                lab[tid] = []
        grid = collections.defaultdict(collections.Counter)
        for tid, sprint in self.q(f"SELECT id, sprint FROM tickets WHERE {self.real}", self.rt):
            for t in lab.get(tid, []):
                grid[t][sprint] += 1
        sprints = sorted({s for c in grid.values() for s in c})
        totals = {t: sum(c.values()) for t, c in grid.items()}
        top = sorted(totals, key=lambda t: -totals[t])[:10]
        return {
            "sprints": sprints,
            "topics": [{"name": t, "data": [grid[t].get(s, 0) for s in sprints]} for t in top],
        }

    def size(self):
        """The team's own Size estimate - the only real measure of effort in the data.

        Size is set during refinement on a Fibonacci-style scale. It is not captured for
        every ticket, so unsized tickets are reported separately rather than treated as
        zero, which would understate every total.
        """
        rows = self.q(
            f"SELECT type, sprint, size, closed FROM tickets WHERE {self.real}", self.rt)
        sized = [r for r in rows if r[2] is not None]
        dist = collections.Counter()
        by_type = collections.defaultdict(list)
        by_sprint_pts = collections.Counter()
        by_sprint_n = collections.Counter()
        for ttype, sprint, size, closed in sized:
            dist[float(size)] += 1
            by_type[ttype].append(float(size))
            # Credit size to the sprint the ticket was delivered in, so this reads as
            # throughput. Tickets still open contribute nothing yet.
            if closed and sprint is not None:
                by_sprint_pts[sprint] += float(size)
                by_sprint_n[sprint] += 1

        def num(x):
            return int(x) if float(x).is_integer() else x

        points = [float(r[2]) for r in sized]
        return {
            "field": "Microsoft.VSTS.Scheduling.Size",
            "tickets": len(rows),
            "sized": len(sized),
            "unsized": len(rows) - len(sized),
            "coverage_pct": round(100 * len(sized) / len(rows), 1) if rows else 0,
            "total_points": round(sum(points), 1),
            "median_points": percentile(points, 0.5),
            "mean_points": round(sum(points) / len(points), 2) if points else None,
            "distribution": [{"name": str(num(s)), "count": n} for s, n in sorted(dist.items())],
            "by_type": [
                {"type": t, "tickets": len(v), "total_points": round(sum(v), 1),
                 "median_points": percentile(v, 0.5),
                 "mean_points": round(sum(v) / len(v), 2) if v else None}
                for t, v in sorted(by_type.items(), key=lambda kv: -sum(kv[1]))],
            "by_sprint": [
                {"sprint": s, "points": round(by_sprint_pts[s], 1), "tickets": by_sprint_n[s]}
                for s in sorted(by_sprint_pts)],
        }

    def charts(self, data):
        """Ready-to-render chart specs so the web app can bind without recomputing.

        Deliberately a short list. Sections such as cycle_time, evidence, tags and the code
        hot spots are still computed and published in the JSON for analysis and drill-down -
        they simply do not get a chart here.
        """
        out = []
        types = [r["name"] for r in data["totals"]["tickets_by_type"]]

        # Tickets per sprint, stacked by ticket type.
        sprints = data["sprints"]
        out.append(chart(
            "tickets_by_sprint", "Tickets per sprint", "bar",
            [str(s["sprint"]) for s in sprints],
            [{"name": t, "data": [s["by_type"].get(t, 0) for s in sprints]} for t in types],
            stacked=True, x_label="Sprint", y_label="Tickets"))

        # Composition pies.
        out.append(pie("ticket_types", "Ticket types", data["totals"]["tickets_by_type"]))
        out.append(pie("work_patterns", "Work patterns", data["labels"]["work_patterns"],
                       note="Each ticket has exactly one pattern."))
        out.append(pie("topics", "Topics", data["labels"]["topics"], limit=14,
                       note="A ticket can carry several topics, so the slices are shares of "
                            "all topic mentions, not of tickets - they cannot be read as "
                            "'x% of our work'. Use them to compare topics against each other."))

        # Flow.
        months = data["monthly"]
        out.append(chart(
            "created_vs_closed", "Created vs closed by month", "line",
            [m["month"] for m in months],
            [{"name": "Created", "data": [m["created"] for m in months]},
             {"name": "Closed", "data": [m["closed"] for m in months]}],
            x_label="Month", y_label="Tickets"))

        # Size - the team's own effort estimate.
        sz = data["size"]
        out.append(bar("size_distribution", "Tickets by size", sz["distribution"],
                       x_label="Size", y_label="Tickets"))
        out[-1]["note"] = (
            f"The team's own Size estimate. {sz['sized']} of {sz['tickets']} tickets are "
            f"sized ({sz['coverage_pct']}%); the rest are excluded rather than counted as zero.")
        out.append(chart(
            "size_by_sprint", "Size delivered per sprint", "bar",
            [str(s["sprint"]) for s in sz["by_sprint"]],
            [{"name": "Size points", "data": [s["points"] for s in sz["by_sprint"]]}],
            x_label="Sprint", y_label="Size points"))
        out[-1]["note"] = (
            "Total Size of tickets closed in each sprint - the team's own throughput measure. "
            "Sprints where sizing was skipped will read low.")

        people = data["people"]
        out.append(chart(
            "tickets_by_assignee", "Tickets assigned per person", "bar",
            [a["name"] for a in people["assigned"][:15]],
            [{"name": "Tickets", "data": [a["tickets"] for a in people["assigned"][:15]]},
             {"name": "Size points",
              "data": [a["size_points"] for a in people["assigned"][:15]]}],
            x_label="Person", y_label="Count", horizontal=True))
        out[-1]["note"] = (
            f"Counts the Azure DevOps Assigned To field on each ticket; source-control "
            f"authors and changesets are not used. {people['tickets_assigned']} of "
            f"{people['tickets_total']} tickets ({people['coverage_pct']}%) are assigned. "
            f"Size points are the sum of the team's Size estimates for those same tickets.")

        # Focus over time.
        tt = data["topic_trend"]
        out.append(chart(
            "topic_trend", "Topic focus over time", "area",
            [str(s) for s in tt["sprints"]], tt["topics"],
            stacked=True, x_label="Sprint", y_label="Tickets"))
        return out

    def tickets(self):
        """Slim per-ticket rows so the dashboard can drill down and link back to ADO."""
        lab = {}
        for tid, tjson, pattern, conf in self.q(
                "SELECT ticket_id, topics, work_pattern, confidence FROM labels"):
            try:
                ts = json.loads(tjson or "[]")
            except ValueError:
                ts = []
            lab[tid] = (ts, pattern, conf)

        code = collections.Counter()
        for tid, n in self.q(
                "SELECT ticket_id, COUNT(*) FROM links "
                "WHERE kind IN ('tfvc','git_commit','pr') GROUP BY ticket_id"):
            code[tid] = n
        # Roll child-task links up to the parent ticket.
        for parent, n in self.q(
                "SELECT c.parent_id, COUNT(*) FROM children c JOIN links l ON l.ticket_id = c.child_id "
                "WHERE l.kind IN ('tfvc','git_commit','pr') GROUP BY c.parent_id"):
            code[parent] += n
        att = dict(self.q(
            "SELECT ticket_id, COUNT(*) FROM attachments WHERE text IS NOT NULL GROUP BY ticket_id"))

        out = []
        for r in self.q(
                f"SELECT id, type, title, state, sprint, created, closed, url, tags, "
                f"size, priority, severity, assigned_to "
                f"FROM tickets WHERE {self.real} ORDER BY sprint, id", self.rt):
            tid = r[0]
            topics, pattern, conf = lab.get(tid, ([], None, None))
            out.append({
                "id": tid, "type": r[1], "title": r[2], "state": r[3], "sprint": r[4],
                "created": r[5], "closed": r[6], "url": r[7],
                "tags": [t.strip() for t in (r[8] or "").split(";") if t.strip()],
                "size": r[9], "priority": r[10], "severity": r[11],
                "assigned_to": r[12],
                "topics": topics, "work_pattern": pattern, "confidence": conf,
                "code_links": code.get(tid, 0), "text_attachments": att.get(tid, 0),
            })
        return out

    def build(self):
        data = {
            "meta": self.meta(),
            "totals": self.totals(),
            "sprints": self.sprints(),
            "monthly": self.monthly(),
            "cycle_time": self.cycle_time(),
            "size": self.size(),
            "labels": self.labels(),
            "topic_trend": self.topic_trend(),
            "tags": self.tags(),
            "evidence": self.evidence(),
            "people": self.people(),
            "code": self.code(),
            "links": self.links_and_attachments(),
            "rollout": self.rollout(),
            "tickets": self.tickets(),
        }
        data["charts"] = self.charts(data)
        return data


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", help="output JSON path (default: <data dir>/dashboard.json)")
    ap.add_argument("--pretty", action="store_true", help="indent the JSON (bigger file)")
    ap.add_argument("--split", action="store_true",
                    help="also write each section to data/dashboard/<section>.json")
    args = ap.parse_args()

    if not DB.exists():
        sys.exit(f"No database at {DB}. Run src/collect.py first.")

    cfg = load_config()
    try:
        import yaml
        tax = yaml.safe_load((ROOT / "topics.yaml").read_text(encoding="utf-8")) or {}
        cfg["_taxonomy_reviewed"] = bool(tax.get("reviewed"))
    except Exception:
        cfg["_taxonomy_reviewed"] = False

    conn = sqlite3.connect(DB)
    data = Report(conn, cfg).build()

    out = ROOT / args.out if args.out else DATA / "dashboard.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(data, indent=2 if args.pretty else None, ensure_ascii=False),
        encoding="utf-8")
    kb = out.stat().st_size / 1024
    print(f"Wrote {out}  ({kb:.0f} KB)")
    print(f"  {data['meta']['tickets']} tickets, {data['totals']['tickets_closed']} closed, "
          f"{data['code']['changes_total']} changes, {data['code']['files_total']} file touches")

    if args.split:
        d = out.parent / "dashboard"
        d.mkdir(parents=True, exist_ok=True)
        for k, v in data.items():
            p = d / f"{k}.json"
            p.write_text(json.dumps(v, indent=2 if args.pretty else None, ensure_ascii=False),
                         encoding="utf-8")
            print(f"  {p.name}")


if __name__ == "__main__":
    main()
