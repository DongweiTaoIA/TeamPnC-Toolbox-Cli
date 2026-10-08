"""Shared helpers: config, Azure DevOps client, SQLite storage, text cleanup, Azure OpenAI."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from html.parser import HTMLParser
from pathlib import Path

import requests
import yaml

# Project root (one level above src/): config.yaml, .env, topics.yaml, data/, dossiers/.
ROOT = Path(__file__).resolve().parent.parent
# DEVOPS_BOARD=ifrs selects config.ifrs.yaml, ADO_PAT_IFRS and data/ifrs/; unset = the original board.
BOARD = os.environ.get("DEVOPS_BOARD", "").strip().lower()


def data_dir(board: str = BOARD) -> Path:
    return ROOT / "data" / board if board else ROOT / "data"


DATA = data_dir()
DB_PATH = DATA / "tickets.db"
API_VERSION = "7.1"
ADO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"


# ---------- config ----------

def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def load_config(board: str = BOARD) -> dict:
    load_env()
    name = f"config.{board}.yaml" if board else "config.yaml"
    with open(ROOT / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------- Azure DevOps ----------

class Ado:
    """Read-only Azure DevOps REST client."""

    def __init__(self, cfg: dict):
        self.org_url = f"https://dev.azure.com/{cfg['organization']}"
        self.project_url = f"{self.org_url}/{cfg['project']}"
        self.session = requests.Session()
        self.session.headers.update(self._auth_header())
        self.session.headers["Accept"] = "application/json"

    @staticmethod
    def _auth_header() -> dict:
        pat = (BOARD and os.environ.get(f"ADO_PAT_{BOARD.upper()}")) or os.environ.get("ADO_PAT")
        if pat:
            token = base64.b64encode(f":{pat}".encode()).decode()
            return {"Authorization": f"Basic {token}"}
        az = shutil.which("az")
        if az:
            out = subprocess.run(
                [az, "account", "get-access-token", "--resource", ADO_RESOURCE_ID,
                 "--query", "accessToken", "-o", "tsv"],
                capture_output=True, text=True,
            )
            if out.returncode == 0 and out.stdout.strip():
                return {"Authorization": f"Bearer {out.stdout.strip()}"}
        raise SystemExit(
            "No Azure DevOps credentials. Set ADO_PAT in .env (Work Items: Read, Code: Read) "
            "or install Azure CLI and run `az login`."
        )

    def request(self, method: str, url: str, params: dict | None = None, **kwargs) -> requests.Response:
        params = dict(params or {})
        params.setdefault("api-version", API_VERSION)
        for attempt in range(6):
            resp = self.session.request(method, url, params=params, timeout=90, **kwargs)
            # 203 means ADO returned a sign-in page, i.e. the credential is invalid.
            if resp.status_code in (401, 203):
                raise SystemExit(f"Azure DevOps rejected the credential ({resp.status_code}). Check ADO_PAT / az login.")
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = float(resp.headers.get("Retry-After") or 2 ** attempt)
                time.sleep(min(wait, 60))
                continue
            return resp
        resp.raise_for_status()
        return resp

    def get_json(self, url: str, params: dict | None = None) -> dict:
        resp = self.request("GET", url, params)
        resp.raise_for_status()
        return resp.json()

    def post_json(self, url: str, body: dict, params: dict | None = None) -> dict:
        resp = self.request("POST", url, params, json=body)
        resp.raise_for_status()
        return resp.json()

    def file_text(self, kind: str, repo: str | None, path: str, version: str | None) -> str | None:
        """File content at a TFVC changeset or Git commit; None if missing or binary."""
        if not version:
            return None
        if kind == "tfvc":
            url = f"{self.project_url}/_apis/tfvc/items"
            vtype = "changeset"
        else:
            url = f"{self.project_url}/_apis/git/repositories/{repo}/items"
            vtype = "commit"
        resp = self.request("GET", url, {
            "path": path,
            "versionDescriptor.version": version,
            "versionDescriptor.versionType": vtype,
            "includeContent": "true",
        })
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        content = resp.json().get("content")
        if content is None or "\x00" in content or len(content) > 500_000:
            return None
        return content


    def attachment_text(self, url: str, max_bytes: int = 400_000) -> str | None:
        """Text of a work item attachment; None if binary, too big, or missing."""
        resp = self.request("GET", url, {"download": "true"})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        raw = resp.content[:max_bytes]
        if b"\x00" in raw[:4096]:
            return None
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                return raw.decode("cp1252")
            except UnicodeDecodeError:
                return None


# ---------- storage ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    rev INTEGER,
    type TEXT, title TEXT, state TEXT, area TEXT, iteration TEXT, sprint INTEGER,
    tags TEXT, created TEXT, closed TEXT,
    description TEXT, acceptance TEXT, repro TEXT,
    size REAL, priority INTEGER, severity TEXT, assigned_to TEXT,
    parent_id INTEGER, in_scope INTEGER, url TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS children (parent_id INTEGER, child_id INTEGER, PRIMARY KEY (parent_id, child_id));
CREATE TABLE IF NOT EXISTS links (
    ticket_id INTEGER, kind TEXT, ref TEXT, name TEXT,
    PRIMARY KEY (ticket_id, kind, ref)
);
CREATE TABLE IF NOT EXISTS changes (
    kind TEXT, ref TEXT, repo TEXT, version TEXT, base_version TEXT,
    comment TEXT, author TEXT, date TEXT, file_count INTEGER, error TEXT,
    PRIMARY KEY (kind, ref)
);
CREATE TABLE IF NOT EXISTS change_files (
    kind TEXT, ref TEXT, path TEXT, change_type TEXT,
    PRIMARY KEY (kind, ref, path)
);
CREATE TABLE IF NOT EXISTS attachments (
    ticket_id INTEGER, ref TEXT, name TEXT, size INTEGER, text TEXT, error TEXT,
    PRIMARY KEY (ticket_id, ref)
);
CREATE TABLE IF NOT EXISTS labels (
    ticket_id INTEGER PRIMARY KEY, rev INTEGER, topics TEXT, work_pattern TEXT,
    summary TEXT, confidence REAL, taxonomy_hash TEXT, model TEXT, labeled_at TEXT
);
CREATE TABLE IF NOT EXISTS diffs (
    kind TEXT, ref TEXT, path TEXT, diff TEXT,
    PRIMARY KEY (kind, ref, path)
);
CREATE TABLE IF NOT EXISTS ticket_notes (
    ticket_id INTEGER, rev INTEGER, prompt_version TEXT, note TEXT,
    PRIMARY KEY (ticket_id, prompt_version)
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    migrate(db)
    return db


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not add them to an
# existing database, so patch them in; re-run collect.py to populate.
ADDED_COLUMNS = {
    "tickets": {
        "size": "REAL",
        "priority": "INTEGER",
        "severity": "TEXT",
        "assigned_to": "TEXT",
    },
}


def migrate(db: sqlite3.Connection) -> None:
    for table, cols in ADDED_COLUMNS.items():
        have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    db.commit()


def require_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise SystemExit("data/tickets.db not found. Run `python src/collect.py` first.")
    return connect()


def change_refs_for(db: sqlite3.Connection, ticket_id: int) -> list[sqlite3.Row]:
    """Changes linked to a ticket directly or through its child tasks."""
    return db.execute(
        """
        SELECT DISTINCT c.* FROM links l JOIN changes c ON c.kind = l.kind AND c.ref = l.ref
        WHERE l.ticket_id = ? OR l.ticket_id IN (SELECT child_id FROM children WHERE parent_id = ?)
        ORDER BY c.date
        """,
        (ticket_id, ticket_id),
    ).fetchall()


# ---------- text ----------

class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


_MASKS = [
    (re.compile(r"(?i)\b(password|pwd|secret|api[_-]?key|token|accountkey)\b\s*[=:]\s*[^\s;'\"]+"), r"\1=[REDACTED]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"), "[PHONE]"),
    (re.compile(r"\b\d{9,}\b"), "[NUMBER]"),
]


def mask(text: str | None) -> str:
    """Remove secrets and personal data before text is sent to AI."""
    text = text or ""
    for pattern, repl in _MASKS:
        text = pattern.sub(repl, text)
    return text


def clip(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " ...[truncated]"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "topic"


# ---------- Azure OpenAI ----------

class Llm:
    def __init__(self, cfg: dict):
        from openai import AzureOpenAI

        llm_cfg = cfg.get("llm", {})
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        if not endpoint or "<" in endpoint:
            raise SystemExit("Set AZURE_OPENAI_ENDPOINT in .env.")
        api_version = os.environ.get("AZURE_OPENAI_API_VERSION") or llm_cfg.get("api_version", "2024-10-21")
        key = os.environ.get("AZURE_OPENAI_API_KEY")
        if key:
            self.client = AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_version, max_retries=6)
        else:
            try:
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            except ImportError:
                raise SystemExit("Set AZURE_OPENAI_API_KEY in .env, or `pip install azure-identity` for Entra sign-in.")
            provider = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
            self.client = AzureOpenAI(azure_endpoint=endpoint, azure_ad_token_provider=provider,
                                      api_version=api_version, max_retries=6)
        self.model = os.environ.get("AZURE_OPENAI_DEPLOYMENT") or llm_cfg.get("deployment")
        if not self.model:
            raise SystemExit("Set AZURE_OPENAI_DEPLOYMENT in .env or llm.deployment in config.yaml.")
        self.temperature = llm_cfg.get("temperature", 0)
        self.workers = int(llm_cfg.get("workers", 4))

    def _call(self, system: str, user: str, as_json: bool) -> str:
        kwargs = {}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if as_json:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **kwargs,
        )
        return resp.choices[0].message.content or ""

    def json(self, system: str, user: str) -> dict:
        return json.loads(self._call(system, user, as_json=True))

    def text(self, system: str, user: str) -> str:
        return self._call(system, user, as_json=False)
