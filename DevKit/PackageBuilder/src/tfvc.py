"""Get Latest and the ACCP -> Main merge candidates through tf.exe, Visual Studio's TFVC command line.

tf.exe only accepts a PAT as an OAuth login, and only a full-access PAT can use the TFVC service.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger("pnc.tfvc")

PAT_ENV_VARS = ("AZURE_DEVOPS_TFVC_PAT", "AZURE_DEVOPS_PAT")
TF_GLOB = (r"C:\Program Files*\Microsoft Visual Studio\*\*\Common7\IDE\CommonExtensions\Microsoft"
           r"\TeamFoundation\Team Explorer\TF.exe")
TIMEOUT = 1800

_CANDIDATE = re.compile(r"^(\d+)(\*?)\s")
_DO_NOT_MERGE = re.compile(r"\[\s*do\s+not\s+merge\s*\]", re.I)
_CHANGED = re.compile(r"^(Getting|Replacing|Deleting|Adding|Renaming)\s", re.I)
# "---- Summary: 0 conflicts, 1 warnings, 0 errors ----"; the only warning is usually the filtered TF401190.
_SUMMARY = re.compile(r"^-+\s*Summary:\s*(?P<conflicts>\d+) conflicts?, \d+ warnings?, (?P<errors>\d+) errors?", re.I)
# Workspace-size advice tf.exe prints on every command.
_NOISE = ("TF401190",)


class TfvcError(Exception):
    pass


@dataclass(frozen=True)
class TfsSolution:
    name: str
    source: str  # ACCP branch
    target: str  # Main branch
    sheet: str  # worksheet in MigrationList_YYYY-MM-DD.xlsx

    @property
    def default_path(self) -> str:
        return rf"C:\repos\tfs\{self.name}"


SOLUTIONS = (
    TfsSolution("DP_Unifi_NEW", "$/DP_Unifi_NEW/Dev/ACCP", "$/DP_Unifi_NEW/Main", "Unifi-PnC"),
    TfsSolution("DP_Unifi_Resources", "$/DP_Unifi_Resources/Dev/_ACCP", "$/DP_Unifi_Resources/Main", "Resources-PnC"),
)


@dataclass
class Changeset:
    id: int
    author: str = ""
    created: datetime | None = None  # local time, no time zone
    comment: str = ""
    partial: bool = False  # '*' in the candidate list: partly merged already

    @property
    def excluded(self) -> bool:
        return bool(_DO_NOT_MERGE.search(self.comment))


@dataclass
class GetResult:
    branch: str
    updated: int = 0
    problems: list[str] = field(default_factory=list)


@dataclass
class CompareResult:
    solution: TfsSolution
    path: str
    gets: list[GetResult] = field(default_factory=list)
    candidates: list[Changeset] = field(default_factory=list)
    error: str = ""

    @property
    def included(self) -> list[Changeset]:
        return [c for c in self.candidates if not c.excluded]

    @property
    def excluded(self) -> list[Changeset]:
        return [c for c in self.candidates if c.excluded]


def find_tf(override: str = "") -> str:
    if override:
        if os.path.isfile(override):
            return override
        raise TfvcError(f"tf.exe not found at {override} (tf_exe in config.json).")
    found = sorted(glob.glob(TF_GLOB), key=os.path.getmtime, reverse=True)
    if not found:
        raise TfvcError("tf.exe not found - install Visual Studio with Team Explorer, or set tf_exe in config.json.")
    return found[0]


def _pat() -> str:
    for name in PAT_ENV_VARS:
        value = os.environ.get(name, "").strip().strip('"')
        if value:
            return value
    raise TfvcError(f"{PAT_ENV_VARS[0]} is not set - add a full-access PAT to .env.")


def _run(tf: str, pat: str, args: list[str], cwd: str) -> tuple[int, list[str]]:
    try:
        done = subprocess.run(
            [tf, "vc", *args, "/loginType:OAuth", f"/login:.,{pat}", "/noprompt"], cwd=cwd, capture_output=True,
            text=True, errors="replace", timeout=TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise TfvcError(f"tf {args[0]} took longer than {TIMEOUT // 60} minutes") from exc
    lines = [line.rstrip() for line in (done.stdout + done.stderr).replace(pat, "***").splitlines()
             if line.strip() and not line.startswith(_NOISE)]
    log.info("tf %s in %s -> %s, %d lines", " ".join(args[:3]), cwd, done.returncode, len(lines))
    if any(line.startswith("TF30063") for line in lines):
        raise TfvcError(f"Not authorized - {PAT_ENV_VARS[0]} in .env must be a full-access PAT.")
    return done.returncode, lines


def get_latest(tf: str, pat: str, cwd: str, branch: str) -> GetResult:
    code, lines = _run(tf, pat, ["get", branch, "/recursive"], cwd)
    result = GetResult(branch, updated=sum(1 for line in lines if _CHANGED.match(line)))
    # Folder headers end with ':'; what's left besides file actions are conflicts and warnings.
    for line in lines:
        if _CHANGED.match(line) or line.endswith(":") or "all files are up to date" in line.lower():
            continue
        summary = _SUMMARY.match(line)
        if summary is None or int(summary["conflicts"]) or int(summary["errors"]):
            result.problems.append(line)
    # 1 = partial success (conflicts); higher codes mean the get failed.
    if code > 1:
        raise TfvcError(f"Get Latest of {branch} failed: " + " ".join(lines[-3:]))
    return result


def merge_candidates(tf: str, pat: str, cwd: str, source: str, target: str) -> list[tuple[int, bool]]:
    """(changeset, partly merged) in tf's order, newest first. Lists only - never merges."""
    code, lines = _run(tf, pat, ["merge", "/candidate", "/recursive", source, target], cwd)
    found = [(int(m[1]), bool(m[2])) for m in map(_CANDIDATE.match, lines) if m]
    if code != 0 and not found and not any("no changes to merge" in line.lower() for line in lines):
        raise TfvcError(f"Comparing {source} with {target} failed: " + " ".join(lines[-3:]))
    return found


def _to_changeset(raw: dict, partial: bool) -> Changeset:
    created = raw.get("createdDate", "")
    when = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone().replace(tzinfo=None) if created else None
    author = (raw.get("author") or raw.get("checkedInBy") or {}).get("displayName", "")
    return Changeset(int(raw["changesetId"]), author, when, raw.get("comment", ""), partial)


def compare_solution(solution: TfsSolution, path: str, details: Callable[[list[int]], list[dict]],
                     tf_override: str = "") -> CompareResult:
    """Get Latest of both branches, then the ACCP changesets not yet merged into Main. Runs on a worker thread."""
    result = CompareResult(solution, path)
    try:
        if not os.path.isdir(path):
            raise TfvcError(f"{path} doesn't exist - set the local folder of $/{solution.name}.")
        tf, pat = find_tf(tf_override), _pat()
        for branch in (solution.source, solution.target):
            result.gets.append(get_latest(tf, pat, path, branch))
        found = merge_candidates(tf, pat, path, solution.source, solution.target)
        raw = {int(r["changesetId"]): r for r in details([cid for cid, _ in found])} if found else {}
        result.candidates = [_to_changeset(raw[cid], partial) if cid in raw else Changeset(cid, partial=partial)
                             for cid, partial in found]
    except TfvcError as exc:
        result.error = str(exc)
    except Exception as exc:
        log.exception("compare failed for %s", solution.name)
        result.error = f"{type(exc).__name__}: {exc}"
    return result
