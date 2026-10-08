from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from .config import Settings
from .schedule import MigrationEntry

EXECUTION_TIME = "21:30:00"
REFRESH_TIME = "5 AM PST"
# (database name, DNS entry and port)
DP_PROD = ("DPPROD", "SQL_QUE_PROD_PROD.iap.iafg.net,62358")
DP_ACCP = ("DPACCP", "SQL_VAN_TEST_ACCP.iap.iafg.net,49826")
ANONYMIZE_SCRIPT_NAME = "Unifi Anonymization Script.sql"


@dataclass(frozen=True)
class Attachment:
    name: str
    # SharePoint download URL; content is filled in once downloaded.
    url: str
    content: bytes = b""


@dataclass(frozen=True)
class FormField:
    label: str
    value: str | bool
    # If the form has no field with this label, append the text to this field instead.
    append_to: str = ""
    # "date" / "datetime": value is yyyy-MM-dd[ HH:mm:ss], converted to the user's format and read back.
    # "person": pick the matching dropdown entry, or leave the field blank if none matches.
    # "reference": look the value up by name in the field's reference table.
    kind: str = ""


@dataclass(frozen=True)
class FormSpec:
    key: str
    title: str
    url: str
    fields: tuple[FormField, ...]
    attachments: tuple[Attachment, ...] = ()


def _long(value: date) -> str:
    return f"{value:%b} {value.day}, {value.year}"


def _dotted(value: date) -> str:
    return f"{value:%b}. {value:%d}, {value.year}"


def refresh_post(entry: MigrationEntry, request: str) -> tuple[str, str]:
    """(subject, message) of the Teams channel post announcing the refresh and code freeze."""
    day = entry.code_freeze
    subject = f"Unifi ACCP DB refresh and code freeze {day:%A} {day:%b} {day:%d}, {day.year}"
    message = "\n".join((
        f"Hi General, code freeze on Unifi ACCP and database refresh is scheduled at 5AM PT on {day:%A}, "
        f"{_dotted(day)}. Thanks",
        f"Service Now request: {request or 'REQ (fill in)'}",
    ))
    return subject, message


def refresh_forms(entry: MigrationEntry, settings: Settings) -> list[FormSpec]:
    """ServiceNow request to restore DPPROD into DPACCP on the code freeze date (none without that date)."""
    day = entry.code_freeze
    if day is None:
        return []
    (source_db, source_dns), (target_db, target_dns) = DP_PROD, DP_ACCP
    description = "\n".join((
        f"Please restore a backup of {source_db} ({source_dns}) into {target_db} ({target_dns})",
        f"Can this be ended on {_dotted(day)} at {REFRESH_TIME}?",
        "Please use latest possible backup.",
    ))
    return [FormSpec("dpaccp", "DPACCP refresh", settings.servicenow_dba_url, (
        FormField("Select required service", "Database restoration"),
        FormField("Enter request summary",
                  f"Please restore {source_db} into {target_db} on {_dotted(day)} {REFRESH_TIME}"),
        FormField("Enter detailed request description", description),
        FormField("Select source environment", "PROD"),
        FormField("Select the destination environment", "ACCP"),
        FormField("Enter source DNS entry and port", source_dns),
        FormField("Enter source database name", source_db),
        FormField("Enter destination database DNS entry and port", target_dns),
        FormField("Enter destination database name", target_db),
        FormField("Enter affected organic system(s)", "DP"),
        FormField("Check this box if the system(s) does not exist", False),
        FormField("iA Wealth", False),
        FormField("iA Auto Finance (IAAF)", False),
        FormField("IAAH", False),
        FormField("iA Vancouver (IAP)", True),
        FormField("iA Québec", False),
        FormField("Other(s)", False),
        FormField("Insert Planview project code", settings.refresh_planview_code),
        FormField("Select the project lead (CP/SM)", settings.refresh_project_lead, kind="person"),
        FormField("Select required date", f"{day:%Y-%m-%d}", kind="date"),
    ), attachments=(Attachment(ANONYMIZE_SCRIPT_NAME, settings.anonymization_script_url),))]


def booking_forms(entry: MigrationEntry, settings: Settings,
                  full_names: Mapping[str, str] | None = None) -> list[FormSpec]:
    """ServiceNow resource-booking requests (DBA team, Operation team) for a migration.

    full_names maps schedule names (often first names only) to full names.
    """
    names = full_names or {}
    prod = entry.prod_date
    when = _long(prod)
    developer = names.get(entry.deployer, entry.deployer)
    summary = f"Unifi and UN Production Implementation - {when}"

    dba_description = "\n".join((
        summary,
        "IA Quebec DBA resource is needed to be on stand by during our Unifi/UniFi 2.0/Unification production "
        f"migration tonight {when} to assist us should we run into issues with Unification/Unifi SQL database.",
        "The deployment will start at 9:30PM PST.",
        "It should end approx. 2 AM PST.",
        "The Start and End dates are PST. Also, the End Date is approximate - migration might be done earlier "
        "or later.",
    ))
    dba = FormSpec("dba", "DBA team", settings.servicenow_dba_url, (
        FormField("Select required service", "Resources reservation of pager production implementation type"),
        FormField("Select the relevant time period", "Non-business hours"),
        FormField("Specify execution date and time", f"{prod:%Y-%m-%d} {EXECUTION_TIME}", kind="datetime"),
        FormField("Enter request summary", summary),
        FormField("Enter detailed request description", dba_description),
        FormField("Enter affected organic system(s)", "UniFi applications"),
        FormField("Check this box if the system(s) does not exist", False),
        FormField("Select the environment", "PROD"),
        FormField("iA Wealth", False),
        FormField("iA Auto Finance (IAAF)", False),
        FormField("IAAH", False),
        FormField("iA Vancouver (IAP)", True),
        FormField("iA Québec", False),
        FormField("Other(s)", False),
        FormField("Insert Planview project code", settings.planview_code),
        FormField("Select the project lead (CP/SM)", entry.deployer, kind="person"),
        FormField("Select required date", f"{prod:%Y-%m-%d}", kind="date"),
    ))

    backup_name = names.get(entry.deployer_backup, entry.deployer_backup)
    backup = f"  Backup is {backup_name}." if backup_name else ""
    ops_description = "\n".join((
        f"We are going to do UniFi production migration on {prod:%b}. {prod:%d}, {prod.year}.",
        "Please refer to \u201cUnifi Production Migration document for IAQ Operations\u201d "
        "for the steps to be done on migration night.",
        "",
        f"The developer doing migration tonight is {developer}.{backup}",
        "Can you please send us the names of the Operators that will be on shift during our migration night?",
        "",
        "IMPORTANT NOTES:",
        "o\tIf the NDPY150Q, NDPY151Q, NDPY154Q, NDPY152Q, NDPY153Q, NDPY190Q, NDPY191Q jobs are not done "
        "by 9:45 PM PST, inform developer.",
        "o\tIf the backup job NSQY514D is running for more than 30 minutes, inform developer.",
        "o\tInform developer as soon as \u201cNSQY514D - BACKUP SQL DP PRODUCTION DATABASE PRIOR TO MIGRATION\u201d "
        "is done so we can start migration.",
    ))
    requirement = f"UniFi/UniFI 2.0 production migration - {when}"
    ops = FormSpec("ops", "Operation team", settings.servicenow_ops_url, (
        FormField("Select required service type", "System Management"),
        FormField("Enter a requirement description", requirement),
        FormField("Description of work to be done", ops_description, append_to="Enter a requirement description"),
        FormField("Select the team required to carry out this request", "Ops. Centre", kind="reference"),
        FormField("Select approximative required date", f"{prod:%Y-%m-%d}", kind="date"),
    ))
    return [dba, ops]
