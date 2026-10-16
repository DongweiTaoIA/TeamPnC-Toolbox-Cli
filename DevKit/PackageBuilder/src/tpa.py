"""TPA releases (git + YAML pipelines in the indall organization).

The package builder picks a pipeline run whose ACCP stage succeeded; the commits to migrate are those in that
run's commit but not in the previous production release (see `TpaClient.release_commits`).
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

import requests

log = logging.getLogger("pnc.tpa")

PAT_ENV_VAR = "AZURE_DEVOPS_INDALL_PAT"
API_VERSION = "7.1"
ORG_URL = "https://dev.azure.com/indall"
PROJECT = "Vancouver"
ACCP_STAGE = "ACCP"
PROD_STAGE = "PROD"
RUNS_SHOWN = 15
# Completed runs checked for a successful PROD stage when looking for the previous production release.
PROD_RUNS_CHECKED = 60
HISTORY_LIMIT = 1000


class TpaError(Exception):
    pass


@dataclass(frozen=True)
class TpaComponent:
    key: str
    title: str
    definition_id: int
    sheet: str  # worksheet in MigrationList_YYYY-MM-DD.xlsx


COMPONENTS = (
    TpaComponent("service", "TPA Service", 17488, "TPA (WebAPI)-PnC"),
    TpaComponent("web", "TPA Web", 17560, "TPA(Portal)-PnC"),
)


@dataclass
class Stage:
    name: str
    state: str  # pending / inProgress / completed
    result: str  # succeeded / failed / canceled / skipped / ''


@dataclass
class ReleaseRun:
    id: int
    number: str
    commit: str
    branch: str
    queued: datetime | None
    stages: list[Stage]
    web_url: str
    status: str = ""
    result: str = ""

    @property
    def accp_ok(self) -> bool:
        return any(s.name.upper() == ACCP_STAGE and s.result == "succeeded" for s in self.stages)


@dataclass
class ReleaseList:
    pipeline: str
    repository: str
    runs: list[ReleaseRun]


@dataclass
class Commit:
    id: str  # 8-character short ID, as Azure DevOps shows it
    full_id: str
    author: str
    date: datetime | None
    message: str  # first line of the commit message


@dataclass
class ReleaseCommits:
    run: ReleaseRun
    baseline: str  # e.g. "PROD run 20260923.1" or "tag 20260529.2"
    baseline_commit: str
    commits: list[Commit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _local(text: str) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


class TpaClient:
    def __init__(self, timeout: int = 60):
        pat = os.environ.get(PAT_ENV_VAR, "").strip().strip('"')
        if not pat:
            raise TpaError(f"{PAT_ENV_VAR} is not set - add an indall PAT (Build Read + Code Read) to .env.")
        self._session = requests.Session()
        self._session.auth = ("", pat)
        self._session.headers["Accept"] = "application/json"
        self._timeout = timeout
        self._base = f"{ORG_URL}/{PROJECT}/_apis"

    def _get(self, path: str, **params) -> dict:
        response = self._session.get(f"{self._base}/{path}", params={**params, "api-version": API_VERSION},
                                     timeout=self._timeout, allow_redirects=False)
        if response.status_code in (203, 302, 401):
            scope = "Build (Read)" if path.startswith("build/") else "Code (Read)"
            raise TpaError(f"Not authorized - {PAT_ENV_VAR} needs {scope} in the indall organization.")
        if not response.ok:
            raise TpaError(f"indall request failed ({response.status_code}): {response.text[:200]}")
        return response.json()

    def _definition(self, component: TpaComponent) -> dict:
        return self._get(f"build/definitions/{component.definition_id}")

    def _stages(self, build_id: int) -> list[Stage]:
        records = self._get(f"build/builds/{build_id}/timeline").get("records") or []
        stages = sorted((r for r in records if r.get("type") == "Stage"), key=lambda r: r.get("order") or 0)
        return [Stage(r.get("name", ""), r.get("state") or "", r.get("result") or "") for r in stages]

    def _run(self, raw: dict, stages: list[Stage]) -> ReleaseRun:
        return ReleaseRun(
            id=raw["id"], number=raw.get("buildNumber", ""), commit=raw.get("sourceVersion", ""),
            branch=raw.get("sourceBranch", "").replace("refs/heads/", ""), queued=_local(raw.get("queueTime", "")),
            stages=stages, web_url=((raw.get("_links") or {}).get("web") or {}).get("href", ""),
            status=raw.get("status", ""), result=raw.get("result") or "",
        )

    def list_releases(self, component: TpaComponent, top: int = RUNS_SHOWN) -> ReleaseList:
        definition = self._definition(component)
        builds = self._get("build/builds", definitions=component.definition_id, **{"$top": top},
                           queryOrder="queueTimeDescending").get("value", [])
        with ThreadPoolExecutor(max_workers=6) as pool:
            stages = list(pool.map(lambda b: self._stages(b["id"]), builds))
        runs = [self._run(b, s) for b, s in zip(builds, stages)]
        log.info("%s: %d runs, ACCP succeeded: %s", component.title, len(runs),
                 [r.number for r in runs if r.accp_ok])
        return ReleaseList(definition.get("name", ""), (definition.get("repository") or {}).get("name", ""), runs)

    def _tags(self, repo: str) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for ref in self._get(f"git/repositories/{repo}/refs", filter="tags/", peelTags="true").get("value", []):
            commit = ref.get("peeledObjectId") or ref.get("objectId")
            found.setdefault(commit, []).append(ref["name"].removeprefix("refs/tags/"))
        return found

    def _message(self, repo: str, raw: dict) -> str:
        comment = raw.get("comment", "")
        if raw.get("commentTruncated"):
            comment = self._get(f"git/repositories/{repo}/commits/{raw['commitId']}").get("comment", comment)
        return (comment.strip().splitlines() or [""])[0].strip()

    def _prod_releases(self, component: TpaComponent) -> dict[str, str]:
        """Commit -> build number of recent runs whose PROD stage succeeded."""
        builds = self._get("build/builds", definitions=component.definition_id, **{"$top": PROD_RUNS_CHECKED},
                           statusFilter="completed", resultFilter="succeeded,partiallySucceeded",
                           queryOrder="queueTimeDescending").get("value", [])
        with ThreadPoolExecutor(max_workers=6) as pool:
            stages = list(pool.map(lambda b: self._stages(b["id"]), builds))
        found: dict[str, str] = {}
        for build, run_stages in zip(builds, stages):
            if any(s.name.upper() == PROD_STAGE and s.result == "succeeded" for s in run_stages):
                found.setdefault(build.get("sourceVersion", ""), build.get("buildNumber", ""))
        return found

    def _tag_warnings(self, component: TpaComponent, tag_names: list[str]) -> list[str]:
        """A tag counts as a PROD release, but warn when its own pipeline run never reached PROD."""
        for tag in tag_names:
            runs = self._get("build/builds", definitions=component.definition_id,
                             buildNumber=tag.lstrip("#")).get("value", [])
            if not runs:
                continue
            stage = next((s for s in self._stages(runs[0]["id"]) if s.name.upper() == PROD_STAGE), None)
            if stage is not None and stage.result != "succeeded":
                return [f"Run {runs[0].get('buildNumber')} was tagged {tag} but its PROD stage shows "
                        f"'{stage.result or stage.state}'. Check it really is the last PROD release."]
        return []

    def release_commits(self, component: TpaComponent, run: ReleaseRun) -> ReleaseCommits:
        """Commits in `run` that aren't in the previous production release.

        That release is the nearest commit before the run that is either tagged or was deployed by a run whose
        PROD stage succeeded - the release process stopped tagging, so both are checked.
        """
        if not run.accp_ok:
            raise TpaError(f"Run {run.number} hasn't passed ACCP - only ACCP: Success runs can be released.")
        repo = (self._definition(component).get("repository") or {}).get("id")
        if not repo:
            raise TpaError("The pipeline has no git repository.")
        tags, prod = self._tags(repo), self._prod_releases(component)
        history = self._get(f"git/repositories/{repo}/commits", **{
            "searchCriteria.itemVersion.version": run.commit, "searchCriteria.itemVersion.versionType": "commit",
            "searchCriteria.$top": HISTORY_LIMIT}).get("value", [])
        base = next((c["commitId"] for c in history if c["commitId"] in tags or c["commitId"] in prod), None)
        if base is None:
            raise TpaError(f"No tagged or PROD-released commit in the last {HISTORY_LIMIT} commits before "
                           f"{run.commit[:8]}.")
        label = (f"PROD run {prod[base]}" if base in prod else "") or f"tag {sorted(tags[base])[-1]}"
        if base in prod and base in tags:
            label += f" (tag {sorted(tags[base])[-1]})"
        result = ReleaseCommits(run, label, base)
        if base == run.commit:
            result.notes.append(f"Run {run.number}'s commit is already in PROD ({label}) - nothing new to migrate.")
            return result
        # Commits reachable from the run's commit but not from the baseline, i.e. git log <base>..<commit>.
        raw = self._get(f"git/repositories/{repo}/commits", **{
            "searchCriteria.itemVersion.version": base, "searchCriteria.itemVersion.versionType": "commit",
            "searchCriteria.compareVersion.version": run.commit,
            "searchCriteria.compareVersion.versionType": "commit", "searchCriteria.$top": HISTORY_LIMIT,
        }).get("value", [])
        result.commits = [Commit(c["commitId"][:8], c["commitId"], (c.get("author") or {}).get("name", ""),
                                 _local((c.get("author") or {}).get("date", "")), self._message(repo, c))
                          for c in raw]
        if base not in prod:
            result.notes += self._tag_warnings(component, tags[base])
        log.info("%s run %s: baseline %s (%s), %d commits", component.title, run.number, label, base[:8],
                 len(result.commits))
        return result
