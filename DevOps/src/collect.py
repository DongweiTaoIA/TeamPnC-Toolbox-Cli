"""Step 2: download tickets and linked changesets/commits into data/tickets.db (read-only)."""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
from collections import Counter
from urllib.parse import unquote

from common import DATA, Ado, connect, html_to_text, load_config

FIELDS = {
    "rev": "System.Rev",
    "type": "System.WorkItemType",
    "title": "System.Title",
    "state": "System.State",
    "area": "System.AreaPath",
    "iteration": "System.IterationPath",
    "tags": "System.Tags",
    "created": "System.CreatedDate",
    "closed": "Microsoft.VSTS.Common.ClosedDate",
    "assigned_to": "System.AssignedTo",
    "description": "System.Description",
    "acceptance": "Microsoft.VSTS.Common.AcceptanceCriteria",
    "repro": "Microsoft.VSTS.TCM.ReproSteps",
    # Team-entered sizing. Size is a Fibonacci-style estimate set during refinement and is
    # populated on ~96% of tickets, so it is the one real measure of effort available.
    "size": "Microsoft.VSTS.Scheduling.Size",
    "priority": "Microsoft.VSTS.Common.Priority",
    "severity": "Microsoft.VSTS.Common.Severity",
}
HTML_FIELDS = {"description", "acceptance", "repro"}


def wiql_escape(text: str) -> str:
    return text.replace("'", "''")


SPRINT_RE = re.compile(r"^sprint\s*0*(\d+)\b", re.I)


def resolve_sprints(ado: Ado, root: str, first: int, last: int | None) -> list[tuple[int, str, str | None, str | None]]:
    """Find iteration paths for sprint numbers. Names often carry a suffix, e.g. 'Sprint 119 - Complete X'.

    With last=None, every sprint from `first` that has started by today is included.
    """
    tree = ado.get_json(f"{ado.project_url}/_apis/wit/classificationnodes/Iterations", {"$depth": 6})
    found: dict[int, tuple[int, str, str | None, str | None]] = {}
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()

    def walk(node: dict, prefix: str) -> None:
        path = f"{prefix}\\{node['name']}" if prefix else node["name"]
        match = SPRINT_RE.match(node["name"])
        if match and path.lower().startswith(root.lower() + "\\"):
            n = int(match.group(1))
            attrs = node.get("attributes") or {}
            started = (attrs.get("startDate") or "")[:10] <= today
            in_range = n <= last if last is not None else bool(attrs.get("startDate")) and started
            if first <= n and in_range and n not in found:
                found[n] = (n, path, attrs.get("startDate"), attrs.get("finishDate"))
        for child in node.get("children") or []:
            walk(child, path)

    walk(tree, "")
    last = last if last is not None else max(found, default=first)
    missing = [n for n in range(first, last + 1) if n not in found]
    if missing:
        print(f"  ! no iteration found for sprint(s): {', '.join(map(str, missing))}")
    return [found[n] for n in sorted(found)]


def query_sprint(ado: Ado, path: str, types: list[str]) -> list[int]:
    wiql = (
        "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
        f"AND [System.IterationPath] UNDER '{wiql_escape(path)}'"
    )
    if types:
        wiql += " AND [System.WorkItemType] IN (" + ", ".join(f"'{wiql_escape(t)}'" for t in types) + ")"
    resp = ado.request("POST", f"{ado.project_url}/_apis/wit/wiql", json={"query": wiql})
    if resp.status_code == 400 and "TF51011" in resp.text:
        print(f"  ! iteration not found: {path}")
        return []
    resp.raise_for_status()
    ids = [w["id"] for w in resp.json().get("workItems", [])]
    if len(ids) >= 20000:
        print(f"  ! {path}: hit the 20,000 WIQL limit, results are incomplete")
    return ids


def parse_relation(rel: dict) -> tuple[str, str, str] | None:
    """Return (kind, ref, name) for relations we care about."""
    url = rel.get("url", "")
    rel_type = rel.get("rel", "")
    name = (rel.get("attributes") or {}).get("name", "")
    if rel_type == "System.LinkTypes.Hierarchy-Forward":
        return "child", url.rsplit("/", 1)[-1], name
    if rel_type == "System.LinkTypes.Hierarchy-Reverse":
        return "parent", url.rsplit("/", 1)[-1], name
    if rel_type == "AttachedFile":
        return "attachment", url, name
    if rel_type != "ArtifactLink":
        return None
    lower = url.lower()
    if lower.startswith("vstfs:///versioncontrol/changeset/"):
        return "tfvc", url.rsplit("/", 1)[-1], name
    if lower.startswith("vstfs:///git/commit/"):
        _project, repo, sha = unquote(url.split("/", 5)[-1]).split("/")
        return "git_commit", f"{repo}/{sha}", name
    if lower.startswith("vstfs:///git/pullrequestid/"):
        _project, repo, pr = unquote(url.split("/", 5)[-1]).split("/")
        return "pr", f"{repo}/{pr}", name
    return "other", url, name


def fetch_items(ado: Ado, ids: list[int]) -> list[dict]:
    items = []
    for i in range(0, len(ids), 200):
        batch = ids[i:i + 200]
        data = ado.get_json(f"{ado.project_url}/_apis/wit/workitems", {
            "ids": ",".join(map(str, batch)), "$expand": "relations", "errorPolicy": "omit",
        })
        items.extend(w for w in data.get("value", []) if w)
    return items


def store_items(db: sqlite3.Connection, items: list[dict], sprint_of: dict[int, int], in_scope: bool,
                web_url: str) -> set[int]:
    """Save tickets and links; return child IDs referenced by these tickets."""
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    child_ids: set[int] = set()
    for w in items:
        f = w.get("fields", {})
        row = {k: f.get(v) for k, v in FIELDS.items()}
        if isinstance(row["assigned_to"], dict):
            row["assigned_to"] = (
                row["assigned_to"].get("displayName")
                or row["assigned_to"].get("uniqueName")
            )
        for k in HTML_FIELDS:
            row[k] = html_to_text(row[k])
        parent_id = None
        db.execute("DELETE FROM links WHERE ticket_id = ?", (w["id"],))
        db.execute("DELETE FROM children WHERE parent_id = ?", (w["id"],))
        for rel in w.get("relations") or []:
            parsed = parse_relation(rel)
            if not parsed:
                continue
            kind, ref, name = parsed
            if kind == "child":
                child_ids.add(int(ref))
                db.execute("INSERT OR IGNORE INTO children VALUES (?, ?)", (w["id"], int(ref)))
            elif kind == "parent":
                parent_id = int(ref)
            else:
                db.execute("INSERT OR IGNORE INTO links VALUES (?, ?, ?, ?)", (w["id"], kind, ref, name))
        # Keep in_scope=1 if the ticket was already found in a sprint.
        existing = db.execute("SELECT in_scope, sprint FROM tickets WHERE id = ?", (w["id"],)).fetchone()
        scope = 1 if in_scope or (existing and existing["in_scope"]) else 0
        sprint = sprint_of.get(w["id"]) or (existing["sprint"] if existing else None)
        db.execute(
            """INSERT OR REPLACE INTO tickets
               (id, rev, type, title, state, area, iteration, sprint, tags, created, closed,
                description, acceptance, repro, size, priority, severity, assigned_to,
                parent_id, in_scope, url, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (w["id"], row["rev"], row["type"], row["title"], row["state"], row["area"], row["iteration"],
             sprint, row["tags"], row["created"], row["closed"], row["description"], row["acceptance"],
             row["repro"], row["size"], row["priority"], row["severity"], row["assigned_to"],
             parent_id, scope, f"{web_url}/_workitems/edit/{w['id']}", now),
        )
    db.commit()
    return child_ids


def paged(ado: Ado, url: str, key: str, top_param: str, skip_param: str, page: int = 500) -> list[dict]:
    out, skip = [], 0
    while True:
        data = ado.get_json(url, {top_param: page, skip_param: skip})
        chunk = data.get(key, [])
        out.extend(chunk)
        if len(chunk) < page:
            return out
        skip += page


def fetch_change(ado: Ado, kind: str, ref: str) -> tuple[dict, list[tuple[str, str]]]:
    if kind == "tfvc":
        cs = ado.get_json(f"{ado.org_url}/_apis/tfvc/changesets/{ref}", {"maxCommentLength": 4000})
        files = paged(ado, f"{ado.org_url}/_apis/tfvc/changesets/{ref}/changes", "value", "$top", "$skip")
        meta = {
            "repo": None, "version": ref, "base_version": str(int(ref) - 1),
            "comment": cs.get("comment", ""), "author": (cs.get("author") or {}).get("displayName"),
            "date": cs.get("createdDate"),
        }
        return meta, [(c["item"]["path"], c.get("changeType", "")) for c in files if not c["item"].get("isFolder")]

    repo, ident = ref.split("/", 1)
    # Repository GUIDs are unique across the organization and some linked repos live in other
    # projects (e.g. DP_WebAPIHub in DP_Unifi_DealerPortal), so scope these calls to the org.
    repo_url = f"{ado.org_url}/_apis/git/repositories/{repo}"
    if kind == "pr":
        pr = ado.get_json(f"{repo_url}/pullrequests/{ident}")
        sha = (pr.get("lastMergeCommit") or {}).get("commitId")
        comment = f"PR {ident}: {pr.get('title', '')}\n{pr.get('description', '')}"
        author, date = (pr.get("createdBy") or {}).get("displayName"), pr.get("creationDate")
    else:
        sha, comment, author, date = ident, None, None, None
    if not sha:
        return {"repo": repo, "version": None, "base_version": None, "comment": comment,
                "author": author, "date": date}, []
    commit = ado.get_json(f"{repo_url}/commits/{sha}")
    files = paged(ado, f"{repo_url}/commits/{sha}/changes", "changes", "top", "skip")
    meta = {
        "repo": repo, "version": sha, "base_version": (commit.get("parents") or [None])[0],
        "comment": comment or commit.get("comment", ""),
        "author": author or (commit.get("author") or {}).get("name"),
        "date": date or (commit.get("author") or {}).get("date"),
    }
    return meta, [(c["item"]["path"], c.get("changeType", "")) for c in files
                  if c.get("item", {}).get("gitObjectType", "blob") == "blob"]


def collect_changes(ado: Ado, db: sqlite3.Connection, refresh: bool) -> None:
    pending = db.execute(
        """SELECT DISTINCT l.kind, l.ref FROM links l
           LEFT JOIN changes c ON c.kind = l.kind AND c.ref = l.ref
           WHERE l.kind IN ('tfvc', 'git_commit', 'pr') AND (c.ref IS NULL OR ? OR c.error IS NOT NULL)""",
        (1 if refresh else 0,),
    ).fetchall()
    print(f"Fetching {len(pending)} linked changes ...")
    for n, row in enumerate(pending, 1):
        kind, ref = row["kind"], row["ref"]
        try:
            meta, files = fetch_change(ado, kind, ref)
            error = None
        except Exception as exc:  # keep going; the error is recorded and retried next run
            meta, files, error = {}, [], str(exc)[:500]
        db.execute("DELETE FROM change_files WHERE kind = ? AND ref = ?", (kind, ref))
        db.executemany("INSERT OR IGNORE INTO change_files VALUES (?, ?, ?, ?)",
                       [(kind, ref, p, t) for p, t in files])
        db.execute(
            "INSERT OR REPLACE INTO changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, ref, meta.get("repo"), meta.get("version"), meta.get("base_version"), meta.get("comment"),
             meta.get("author"), meta.get("date"), len(files), error),
        )
        if n % 50 == 0:
            db.commit()
            print(f"  {n}/{len(pending)}")
    db.commit()


def collect_attachments(ado: Ado, db: sqlite3.Connection, acfg: dict, refresh: bool) -> None:
    """Download text attachments (e.g. .sql scripts) - they often show how the work was actually done."""
    exts = tuple(e.lower() for e in acfg.get("text_extensions", []))
    max_bytes = int(acfg.get("max_bytes", 400_000))
    pending = db.execute(
        """SELECT l.ticket_id, l.ref, l.name FROM links l
           LEFT JOIN attachments a ON a.ticket_id = l.ticket_id AND a.ref = l.ref
           WHERE l.kind = 'attachment' AND (a.ref IS NULL OR ? OR a.error IS NOT NULL)""",
        (1 if refresh else 0,),
    ).fetchall()
    wanted = [r for r in pending if (r["name"] or "").lower().endswith(exts)]
    skipped = len(pending) - len(wanted)
    print(f"Fetching {len(wanted)} text attachments ({skipped} non-text skipped) ...")
    for n, row in enumerate(wanted, 1):
        try:
            text = ado.attachment_text(row["ref"], max_bytes)
            error = None if text is not None else "not text or unavailable"
        except Exception as exc:
            text, error = None, str(exc)[:500]
        db.execute("INSERT OR REPLACE INTO attachments VALUES (?, ?, ?, ?, ?, ?)",
                   (row["ticket_id"], row["ref"], row["name"], len(text or ""), text, error))
        if n % 25 == 0:
            db.commit()
            print(f"  {n}/{len(wanted)}")
    db.commit()


def report(db: sqlite3.Connection, child_types: list[str]) -> str:
    lines: list[str] = []
    tickets = db.execute("SELECT * FROM tickets WHERE in_scope = 1").fetchall()
    lines.append(f"Tickets in scope: {len(tickets)}")

    def table(title: str, counter: Counter, sort_by_key: bool = False) -> None:
        lines.append(f"\n{title}")
        items = sorted(counter.items()) if sort_by_key else counter.most_common()
        for key, count in items:
            lines.append(f"  {str(key):<30} {count:>6}")

    table("By type", Counter(t["type"] for t in tickets))
    table("By sprint", Counter(t["sprint"] for t in tickets), sort_by_key=True)
    table("Created by month", Counter((t["created"] or "")[:7] or "?" for t in tickets), sort_by_key=True)
    table("Closed by month", Counter((t["closed"] or "")[:7] or "open" for t in tickets), sort_by_key=True)
    table("By state", Counter(t["state"] for t in tickets))

    link_kinds = Counter(r["kind"] for r in db.execute(
        "SELECT l.kind FROM links l JOIN tickets t ON t.id = l.ticket_id"))
    table("Links found (all fetched tickets)", link_kinds)

    lines.append("\nEvidence coverage (direct or via child tasks)")
    lines.append(f"  {'type':<28} {'total':>6} {'code':>6} {'attach':>7} {'either':>7}")
    by_type: dict[str, list[int]] = {}
    for t in tickets:
        by_type.setdefault(t["type"], []).append(t["id"])
    scope = """(ticket_id = ? OR ticket_id IN (SELECT child_id FROM children WHERE parent_id = ?))"""
    for ttype, ids in sorted(by_type.items()):
        code = attach = either = 0
        for tid in ids:
            has_code = db.execute(
                f"SELECT 1 FROM links WHERE kind IN ('tfvc','git_commit','pr') AND {scope} LIMIT 1",
                (tid, tid)).fetchone()
            has_attach = db.execute(
                f"SELECT 1 FROM attachments WHERE text IS NOT NULL AND ticket_id IN "
                f"(SELECT ? UNION SELECT child_id FROM children WHERE parent_id = ?) LIMIT 1",
                (tid, tid)).fetchone()
            code += 1 if has_code else 0
            attach += 1 if has_attach else 0
            either += 1 if (has_code or has_attach) else 0
        note = "  (child type)" if ttype in child_types else ""
        pct = 100 * either / len(ids)
        lines.append(f"  {ttype:<28} {len(ids):>6} {code:>6} {attach:>7} {either:>7} ({pct:.0f}%){note}")

    att = db.execute("SELECT name FROM attachments WHERE text IS NOT NULL").fetchall()
    if att:
        table("Text attachments by extension", Counter("." + a["name"].rsplit(".", 1)[-1].lower()
                                                       for a in att if "." in a["name"]))

    errors = db.execute("SELECT COUNT(*) FROM changes WHERE error IS NOT NULL").fetchone()[0]
    if errors:
        lines.append(f"\n! {errors} linked changes could not be fetched (see changes.error); re-run to retry.")
    return "\n".join(lines)


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--first", type=int, default=cfg["sprint_first"], help="first sprint number")
    ap.add_argument("--last", type=int, default=cfg.get("sprint_last"),
                    help="last sprint number (default: latest sprint started by today)")
    ap.add_argument("--no-changes", action="store_true", help="skip fetching changesets/commits")
    ap.add_argument("--no-attachments", action="store_true", help="skip downloading text attachments")
    ap.add_argument("--refresh", action="store_true", help="re-fetch changes/attachments already downloaded")
    ap.add_argument("--report-only", action="store_true", help="print the report from existing data")
    args = ap.parse_args()

    db = connect()
    child_types = cfg.get("child_types", [])
    if not args.report_only:
        ado = Ado(cfg)
        sprint_of: dict[int, int] = {}
        for n, path, start, finish in resolve_sprints(ado, cfg["iteration_root"], args.first, args.last):
            ids = query_sprint(ado, path, cfg.get("work_item_types") or [])
            dates = f"  [{(start or '?')[:10]} .. {(finish or '?')[:10]}]"
            print(f"{path}: {len(ids)} tickets{dates}")
            for i in ids:
                sprint_of.setdefault(i, n)
        if not sprint_of:
            raise SystemExit("No tickets found. Check iteration_root and the sprint range in config.yaml.")
        child_ids = store_items(db, fetch_items(ado, list(sprint_of)), sprint_of, True, ado.project_url)

        # Child tasks outside the sprint range still carry changeset links for their parent.
        known = {r[0] for r in db.execute("SELECT id FROM tickets")}
        missing = sorted(child_ids - known)
        if missing:
            print(f"Fetching {len(missing)} child items outside the sprint range ...")
            store_items(db, fetch_items(ado, missing), {}, False, ado.project_url)
        if not args.no_changes:
            collect_changes(ado, db, args.refresh)
        if not args.no_attachments:
            collect_attachments(ado, db, cfg.get("attachments", {}), args.refresh)

    text = report(db, child_types)
    print("\n" + text)
    (DATA / "collect_report.txt").write_text(text, encoding="utf-8")
    print(f"\nSaved {DATA / 'tickets.db'} and {DATA / 'collect_report.txt'}")


if __name__ == "__main__":
    main()
