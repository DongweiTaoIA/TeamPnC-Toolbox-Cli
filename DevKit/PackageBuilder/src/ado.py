from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import quote

import requests

API_VERSION = "7.1"
COMMENTS_API_VERSION = "7.1-preview.4"
PAT_ENV_VAR = "AZURE_DEVOPS_PAT"
_FIELDS = ",".join((
    "System.Id", "System.Title", "System.State", "System.AreaPath", "System.IterationPath",
    "System.WorkItemType", "System.AssignedTo", "System.Description",
))
_CHILD_LINK = "System.LinkTypes.Hierarchy-Forward"
_PARENT_LINK = "System.LinkTypes.Hierarchy-Reverse"


class AdoError(Exception):
    pass


@dataclass
class WorkItem:
    id: int
    title: str
    state: str
    iteration_path: str
    work_item_type: str
    web_url: str
    assigned_to: str = ""
    description_html: str = ""
    area_path: str = ""


@dataclass
class Comment:
    text_html: str
    author: str
    created: datetime | None


@dataclass
class TicketCreationResult:
    ticket: WorkItem
    created: bool
    tasks_cloned: int = 0
    tasks_total: int = 0
    note: str = ""


def _wiql_literal(value: str) -> str:
    return value.replace("'", "''")


class AdoClient:
    def __init__(self, org: str, project: str, timeout: int = 30):
        pat = os.environ.get(PAT_ENV_VAR)
        if not pat:
            raise AdoError(f"{PAT_ENV_VAR} is not set (add it to .env or the environment).")
        self._org_base = f"https://dev.azure.com/{quote(org)}"
        self._base = f"{self._org_base}/{quote(project)}"
        self._timeout = timeout
        self._session = requests.Session()
        self._session.auth = ("", pat)
        self._session.headers["Accept"] = "application/json"

    def _request(self, method: str, path: str, params: dict | None = None, *, org_level: bool = False,
                 api_version: str = API_VERSION, **kwargs) -> dict:
        params = {**(params or {}), "api-version": api_version}
        response = self._session.request(
            method,
            f"{self._org_base if org_level else self._base}/_apis/{path}",
            params=params,
            timeout=self._timeout,
            allow_redirects=False,
            **kwargs,
        )
        # An invalid PAT yields 401, or 203/302 to a sign-in page.
        if response.status_code in (203, 302, 401):
            raise AdoError(f"Authentication failed - check {PAT_ENV_VAR}.")
        if not response.ok:
            raise AdoError(f"ADO request failed ({response.status_code}): {response.text[:300]}")
        return response.json()

    def get_current_user(self) -> tuple[str, str]:
        """(display name, account email) of the PAT owner."""
        data = self._request("GET", "connectionData", org_level=True, api_version="7.1-preview")
        user = data.get("authenticatedUser") or {}
        account = (user.get("properties") or {}).get("Account") or {}
        return user.get("providerDisplayName", ""), account.get("$value", "")

    def get_work_items(self, ids: list[int]) -> list[WorkItem]:
        items: list[WorkItem] = []
        for start in range(0, len(ids), 200):
            batch = ids[start:start + 200]
            data = self._request(
                "GET",
                "wit/workitems",
                params={"ids": ",".join(map(str, batch)), "fields": _FIELDS},
            )
            items.extend(self._to_work_item(raw) for raw in data.get("value", []))
        return items

    def get_work_item(self, work_item_id: int) -> WorkItem:
        items = self.get_work_items([int(work_item_id)])
        if not items:
            raise AdoError(f"Work item #{work_item_id} not found.")
        return items[0]

    def find_migration_tickets(self, prod_date: date, title_prefix: str, iteration_root: str,
                               lookback_days: int = 120) -> list[WorkItem]:
        """Team tickets titled '<prefix> Mon DD' (e.g. 'Migration Sep 28') for the PROD date, newest first."""
        month = prod_date.strftime("%b")
        variants = sorted({f"{title_prefix} {month} {prod_date.day}", f"{title_prefix} {month} {prod_date:%d}"})
        title_clause = " OR ".join(f"[System.Title] CONTAINS '{_wiql_literal(v)}'" for v in variants)
        since = (prod_date - timedelta(days=lookback_days)).isoformat()
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            "WHERE [System.TeamProject] = @project "
            f"AND [System.IterationPath] UNDER '{_wiql_literal(iteration_root)}' "
            "AND [System.WorkItemType] NOT IN ('Code Review Request', 'Code Review Response') "
            f"AND ({title_clause}) "
            f"AND [System.CreatedDate] >= '{since}' "
            "ORDER BY [System.CreatedDate] DESC"
        )
        data = self._request("POST", "wit/wiql", params={"$top": 50}, json={"query": wiql})
        ids = [ref["id"] for ref in data.get("workItems", [])]
        if not ids:
            return []
        # Titles must start with e.g. 'Migration Oct 07'; 'Migration Sep 2' must not match 'Migration Sep 28'.
        pattern = re.compile(
            rf"^\s*{re.escape(title_prefix)}\s+{month}\s+0?{prod_date.day}(?!\d)", re.IGNORECASE)
        order = {work_item_id: index for index, work_item_id in enumerate(ids)}
        items = [item for item in self.get_work_items(ids) if pattern.match(item.title)]
        return sorted(items, key=lambda item: order[item.id])

    def get_children(self, parent_id: int) -> list[WorkItem]:
        data = self._request("GET", f"wit/workitems/{int(parent_id)}", params={"$expand": "relations"})
        child_ids = [
            int(rel["url"].rstrip("/").rsplit("/", 1)[-1])
            for rel in data.get("relations") or []
            if rel.get("rel") == _CHILD_LINK
        ]
        return self.get_work_items(child_ids) if child_ids else []

    def get_comments(self, work_item_id: int) -> list[Comment]:
        """Discussion comments, newest first."""
        data = self._request("GET", f"wit/workItems/{int(work_item_id)}/comments",
                             params={"order": "desc", "$top": 200}, api_version=COMMENTS_API_VERSION)
        return [self._to_comment(raw) for raw in data.get("comments", [])]

    def add_comment(self, work_item_id: int, text_html: str) -> Comment:
        raw = self._request("POST", f"wit/workItems/{int(work_item_id)}/comments",
                            json={"text": text_html}, api_version=COMMENTS_API_VERSION)
        return self._to_comment(raw)

    @staticmethod
    def _to_comment(raw: dict) -> Comment:
        created = raw.get("createdDate")
        try:
            when = datetime.fromisoformat(created) if created else None
        except ValueError:
            when = None
        return Comment(raw.get("text") or "", (raw.get("createdBy") or {}).get("displayName", ""), when)

    def find_iteration_path(self, iteration_root: str, on_date: date) -> str:
        """Path of the sprint under iteration_root whose date range contains on_date."""
        root = iteration_root.strip("\\")
        relative = root.split("\\", 1)[1] if "\\" in root else ""
        path = "wit/classificationnodes/Iterations" + (f"/{quote(relative.replace(chr(92), '/'))}" if relative else "")
        node = self._request("GET", path, params={"$depth": 1})
        for child in node.get("children") or []:
            attributes = child.get("attributes") or {}
            start, finish = attributes.get("startDate"), attributes.get("finishDate")
            if start and finish and date.fromisoformat(start[:10]) <= on_date <= date.fromisoformat(finish[:10]):
                return f"{root}\\{child['name']}"
        raise AdoError(f"No sprint under '{root}' covers {on_date:%b %d, %Y}.")

    def create_migration_ticket_if_missing(
        self, prod_date: date, title_prefix: str, iteration_root: str,
        template: WorkItem | None, builder: str,
    ) -> TicketCreationResult:
        iteration_path = self.find_iteration_path(iteration_root, prod_date)

        existing = self.find_migration_tickets(prod_date, title_prefix, iteration_root)
        in_sprint = [item for item in existing if item.iteration_path.casefold() == iteration_path.casefold()]
        if in_sprint:
            note = "A matching ticket already exists in the target sprint; no new ticket was created."
            return TicketCreationResult(in_sprint[0], False, note=note)
        if existing:
            note = (
                f"A matching ticket already exists in '{existing[0].iteration_path}', not '{iteration_path}'. "
                "No duplicate was created."
            )
            return TicketCreationResult(existing[0], False, note=note)
        if template is None:
            raise AdoError("The current migration has no linked ticket to clone tasks from.")

        children = self.get_children(template.id)

        notes: list[str] = []
        try:
            assignee = self._find_assignee(builder, iteration_root)
        except (AdoError, requests.RequestException) as exc:
            assignee = None
            notes.append(f"Could not verify builder '{builder}'; left unassigned: {exc}")
        if builder.strip() and assignee is None and not notes:
            notes.append(f"Builder '{builder}' was not found uniquely in Azure DevOps; left unassigned.")
        elif not builder.strip():
            notes.append("No builder is listed; left unassigned.")

        fields = {
            "System.Title": f"{title_prefix} {prod_date:%b %d}",
            "System.IterationPath": iteration_path,
            # Blank overrides ADO's default of assigning new items to the creator.
            "System.AssignedTo": assignee or "",
        }
        if template.area_path:
            fields["System.AreaPath"] = template.area_path
        ticket = self._create_work_item(template.work_item_type, fields)

        cloned = 0
        for child in children:
            child_fields = {
                "System.Title": child.title,
                "System.IterationPath": iteration_path,
                "System.AssignedTo": assignee or "",
            }
            # Descriptions hold the source migration's ServiceNow request links, so they aren't cloned.
            if child.area_path or template.area_path:
                child_fields["System.AreaPath"] = child.area_path or template.area_path
            try:
                self._create_work_item(child.work_item_type, child_fields, parent_id=ticket.id)
            except (AdoError, requests.RequestException) as exc:
                notes.append(f"Task cloning stopped after {cloned} of {len(children)} tasks: {exc}")
                break
            cloned += 1

        if not children:
            notes.append("No child tasks were found on the current migration ticket to clone.")
        return TicketCreationResult(ticket, True, cloned, len(children), " ".join(notes))

    def _find_assignee(self, builder: str, iteration_root: str) -> str | None:
        person = self.find_person(builder, iteration_root)
        return person[0] if person else None

    def find_person(self, name: str, iteration_root: str) -> tuple[str, str] | None:
        """(uniqueName, displayName) of the one person on the team board whose name has all of name's parts."""
        tokens = re.findall(r"\w+", name.casefold())
        if not tokens:
            return None
        name_clause = " AND ".join(f"[System.AssignedTo] CONTAINS '{_wiql_literal(t)}'" for t in tokens)
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            "WHERE [System.TeamProject] = @project "
            f"AND [System.IterationPath] UNDER '{_wiql_literal(iteration_root)}' "
            f"AND {name_clause} "
            "ORDER BY [System.ChangedDate] DESC"
        )
        data = self._request("POST", "wit/wiql", params={"$top": 50}, json={"query": wiql})
        ids = [ref["id"] for ref in data.get("workItems", [])]
        if not ids:
            return None
        raw_items = self._request(
            "GET", "wit/workitems", params={"ids": ",".join(map(str, ids)), "fields": "System.AssignedTo"},
        ).get("value", [])
        people: dict[str, tuple[str, str]] = {}
        for raw in raw_items:
            person = (raw.get("fields") or {}).get("System.AssignedTo") or {}
            unique, display = person.get("uniqueName", ""), person.get("displayName", "")
            # CONTAINS is a substring match; require whole-word name parts (e.g. 'Bill' must not match 'Billy').
            if unique and set(tokens) <= set(re.findall(r"\w+", display.casefold())):
                people[unique.casefold()] = (unique, display)
        return next(iter(people.values())) if len(people) == 1 else None

    def get_changesets(self, ids: list[int]) -> list[dict]:
        """Raw TFVC changesets (author, createdDate, full comment)."""
        found: list[dict] = []
        for start in range(0, len(ids), 100):
            data = self._request("POST", "tfvc/changesetsbatch", org_level=True,
                                 json={"changesetIds": ids[start:start + 100], "commentLength": 4000})
            found.extend(data.get("value", []))
        return found

    def activate_task(self, task_id: int, assignee: str) -> WorkItem:
        """Set the task to Active and assign it; Resolved/Closed tasks are left alone."""
        task = self.get_work_item(task_id)
        if task.state.lower() in ("active", "resolved", "closed"):
            return task
        return self._update_work_item(task_id, {"System.State": "Active", "System.AssignedTo": assignee})

    def resolve_task(self, task_id: int, assignee: str) -> WorkItem:
        """Set the task to Resolved and assign it; the process only allows Resolved from Active."""
        task = self.get_work_item(task_id)
        if task.state.lower() in ("resolved", "closed"):
            return task
        if task.state.lower() != "active":
            self._update_work_item(task_id, {"System.State": "Active", "System.AssignedTo": assignee})
        return self._update_work_item(task_id, {"System.State": "Resolved", "System.AssignedTo": assignee})

    def _update_work_item(self, work_item_id: int, fields: dict[str, str]) -> WorkItem:
        operations = [{"op": "add", "path": f"/fields/{field}", "value": value} for field, value in fields.items()]
        raw = self._request("PATCH", f"wit/workitems/{int(work_item_id)}", json=operations,
                            headers={"Content-Type": "application/json-patch+json"})
        return self._to_work_item(raw)

    def _create_work_item(self, work_item_type: str, fields: dict[str, str], parent_id: int | None = None) -> WorkItem:
        operations = [
            {"op": "add", "path": f"/fields/{field}", "value": value}
            for field, value in fields.items()
        ]
        if parent_id is not None:
            operations.append({
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": _PARENT_LINK,
                    "url": f"{self._base}/_apis/wit/workItems/{parent_id}",
                },
            })
        raw = self._request(
            "POST", f"wit/workitems/${quote(work_item_type)}", json=operations,
            headers={"Content-Type": "application/json-patch+json"},
        )
        return self._to_work_item(raw)

    def _to_work_item(self, raw: dict) -> WorkItem:
        fields = raw.get("fields", {})
        assigned = fields.get("System.AssignedTo") or {}
        return WorkItem(
            id=raw["id"],
            title=fields.get("System.Title", ""),
            state=fields.get("System.State", ""),
            iteration_path=fields.get("System.IterationPath", ""),
            work_item_type=fields.get("System.WorkItemType", ""),
            web_url=f"{self._base}/_workitems/edit/{raw['id']}",
            assigned_to=assigned.get("displayName", "") if isinstance(assigned, dict) else str(assigned),
            description_html=fields.get("System.Description") or "",
            area_path=fields.get("System.AreaPath", ""),
        )
