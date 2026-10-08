from __future__ import annotations

import json
from pathlib import Path

from .config import DATA_DIR


class AppState:
    """Per-user state: checklist progress and ticket overrides per migration."""

    def __init__(self, path: Path = DATA_DIR / "state.json"):
        self._path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self._data: dict = data if isinstance(data, dict) else {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def _migration(self, key: str) -> dict:
        return self._data.setdefault("migrations", {}).setdefault(key, {})

    def get_check(self, key: str, item: str) -> bool | None:
        return self._data.get("migrations", {}).get(key, {}).get("checks", {}).get(item)

    def set_check(self, key: str, item: str, value: bool) -> None:
        self._migration(key).setdefault("checks", {})[item] = value
        self._save()

    def ticket_id(self, key: str) -> int | None:
        return self._data.get("migrations", {}).get(key, {}).get("ticket_id")

    def set_ticket_id(self, key: str, ticket_id: int) -> None:
        self._migration(key)["ticket_id"] = ticket_id
        self._save()
