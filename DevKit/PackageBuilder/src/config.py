from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Optional overrides live in config.json at the project root.
CONFIG_FILE = PROJECT_ROOT / "config.json"
ENV_FILE = PROJECT_ROOT / ".env"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PnCMigration"


@dataclass
class ScheduleColumns:
    sprint: str = "A"  # TODO: confirm which column holds the sprint name
    code_freeze: str = "D"
    prod_date: str = "F"
    builder: str = "K"
    builder_backup: str = "L"
    deployer: str = "V"
    deployer_backup: str = "W"


@dataclass
class Settings:
    excel_path: str = ""
    schedule_download_url: str = ""
    holidays: frozenset[date] = field(default_factory=frozenset)
    sheet_name: str = "Sprint & Migration Schedule"
    first_data_row: int = 2
    columns: ScheduleColumns = field(default_factory=ScheduleColumns)
    ado_org: str = "SOPCV-SNW"
    ado_project: str = "DP_Unifi_NEW"
    ado_title_prefix: str = "Migration"
    # Team PnC board: DP_Unifi_NEW\Team1 PnC Admin\Sprint NNN
    ado_iteration_root: str = "DP_Unifi_NEW\\Team1 PnC Admin"
    servicenow_dba_url: str = (
        "https://iaaesf.service-now.com/sp?id=sc_cat_item&sys_id=9b5954ed978a591024c43cf3a253af56"
        "&table=sc_cat_item&searchTerm=Request%20for%20the%20Database%20team"
    )
    servicenow_ops_url: str = (
        "https://iaaesf.service-now.com/sp?id=sc_cat_item&sys_id=53f7a515db98a0543694dd0968961972&table=sc_cat_item"
    )
    planview_code: str = "0011024 - DS \u2013 Run Operational"
    refresh_planview_code: str = "0011630 - DS - Operational run"
    refresh_project_lead: str = "Abhinesh Ram"
    anonymization_script_url: str = (
        "https://indall.sharepoint.com/teams/-IAP-LS-ITSAL-VAGDevelopmentTeam/_layouts/15/download.aspx?SourceUrl="
        "%2Fteams%2F-IAP-LS-ITSAL-VAGDevelopmentTeam%2FShared%20Documents%2FGeneral%2FServiceNow"
        "%2FRequests%20to%20submit%20in%20Portal%2FUnifi%20Anonymization%20Script.sql"
    )
    # Paste the channel's "Get link to channel" URL here to open it directly.
    teams_channel_url: str = "https://teams.microsoft.com/v2/"
    teams_team: str = "*IAP-LS-IT-Dealer Services Team"
    teams_channel: str = "General"


def load_settings() -> Settings:
    if not CONFIG_FILE.exists():
        return Settings()
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    columns = ScheduleColumns(**data.pop("columns", {}))
    holidays = frozenset(date.fromisoformat(value) for value in data.pop("holidays", []))
    return Settings(columns=columns, holidays=holidays, **data)
