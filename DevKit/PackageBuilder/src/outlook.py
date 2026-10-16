"""Finds the Change Coordinator's "Action required - CHG..." email in classic Outlook and the migration folder.

Reading a message's Body or sender makes Outlook show its "A program is trying to access email address
information" security prompt. To avoid it, only unprotected properties (Subject, ReceivedTime, EntryID) are
read, and the body is checked with Outlook search filters, which tell whether it contains a phrase without
reading it.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .emails import SentEmail, find_sent
from .migration_list import SHORTCUT, read_shortcut

log = logging.getLogger("pnc.outlook")

INBOX = 6  # olFolderInbox
MAIL_CLASS = 43  # olMail
SUBJECT = "urn:schemas:httpmail:subject"
BODY = "urn:schemas:httpmail:textdescription"
SUBJECT_FILTER = f"@SQL=\"{SUBJECT}\" like '%for scheduled Migration on%'"
PROOF_FOLDER = "Proof of Tests"
# How long before the migration the email can arrive.
LOOKBACK_DAYS = 45
SUBFOLDER_DEPTH = 3
# "Please before end of day on Monday October 19th" - seen 2 to 4 business days before the migration.
DEADLINE_DAYS = 10

_SUBJECT_DATE = re.compile(r"Migration on\s+([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I)
_CHANGE = re.compile(r"\bCHG\d{5,}\b", re.I)


class OutlookError(Exception):
    pass


@dataclass
class ChangeEmail:
    subject: str
    change: str
    migration_date: date
    received: datetime
    deadline: date | None
    entry_id: str
    store_id: str


@dataclass
class MigrationFolder:
    path: str = ""
    exists: bool = False
    in_email: bool = False  # the email body mentions this folder's name
    proof_path: str = ""
    proof_exists: bool = False
    proof_items: int = 0
    contents: list[str] = field(default_factory=list)
    list_url: str = ""  # MigrationList workbook, from the folder's _migration.url shortcut
    note: str = ""


@dataclass
class ChangeLookup:
    email: ChangeEmail | None
    folder: MigrationFolder
    # Migration list / code freeze emails already sent for this migration, by emails.KINDS id
    sent: dict[str, SentEmail | None] = field(default_factory=dict)


def subject_date(subject: str) -> date | None:
    found = _SUBJECT_DATE.search(subject)
    if not found:
        return None
    try:
        return datetime.strptime(f"{found[1][:3]} {found[2]} {found[3]}", "%b %d %Y").date()
    except ValueError:
        return None


def _com():
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise OutlookError("pywin32 is not installed - run: pip install -e .") from exc
    return pythoncom, win32com.client


def _namespace(client):
    try:
        return client.Dispatch("Outlook.Application").GetNamespace("MAPI")
    except Exception as exc:
        raise OutlookError(f"Couldn't open classic Outlook ({exc}). The new Outlook app can't be read.") from exc


def _local(value) -> datetime:
    # pywin32 returns Outlook's local time labelled as UTC; drop the label.
    return datetime(value.year, value.month, value.day, value.hour, value.minute, value.second)


def _quote(text: str) -> str:
    return text.replace("'", "''")


def _folders(root, depth: int):
    yield root
    if depth <= 0:
        return
    try:
        children = list(root.Folders)
    except Exception:
        return
    for child in children:
        yield from _folders(child, depth - 1)


def _candidates(folder, since: datetime):
    try:
        items = folder.Items.Restrict(SUBJECT_FILTER)
    except Exception as exc:
        log.warning("search failed in %s: %s", getattr(folder, "FolderPath", "?"), exc)
        return
    for item in items:
        try:
            if item.Class != MAIL_CLASS:
                continue
            received = _local(item.ReceivedTime)
        except Exception:
            continue
        if received >= since:
            yield item, received


def _find_item(inbox, prod_date: date):
    """Latest email whose subject date is `prod_date`; originals win over RE:/FW:."""
    since = datetime.combine(prod_date - timedelta(days=LOOKBACK_DAYS), datetime.min.time())
    best, best_key = None, None
    # Inbox first; subfolders only if a rule moved it.
    for folders in ([inbox], list(_folders(inbox, SUBFOLDER_DEPTH))[1:]):
        for folder in folders:
            for item, received in _candidates(folder, since):
                subject = item.Subject or ""
                if subject_date(subject) != prod_date:
                    continue
                key = (not re.match(r"\s*(re|fw|fwd)\s*:", subject, re.I), received)
                if best_key is None or key > best_key:
                    best, best_key = item, key
        if best is not None:
            return best, best_key[1]
    return None, None


def _body_has(same_subject, phrase: str) -> bool:
    """Whether the email body contains `phrase`, checked by Outlook without reading the body.

    `same_subject` is the folder's items already narrowed to the email's subject, so each check
    searches a handful of messages instead of the whole Inbox.
    """
    try:
        return same_subject.Restrict(f"@SQL=\"{BODY}\" like '%{_quote(phrase)}%'").Count > 0
    except Exception as exc:
        log.warning("body check failed for %r: %s", phrase, exc)
        return False


def _deadline(same_subject, prod_date: date) -> date | None:
    for back in range(1, DEADLINE_DAYS + 1):
        day = prod_date - timedelta(days=back)
        for number in dict.fromkeys((str(day.day), f"{day.day:02d}")):
            if _body_has(same_subject, f"end of day on {day:%A} {day:%B} {number}"):
                return day
    return None


def _folder_candidates(prod_date: date, root: str) -> tuple[list[Path], str]:
    # scandir returns each entry's type with the listing; asking per path costs ~0.3 s each on the L: share.
    prefix = prod_date.isoformat()
    try:
        with os.scandir(root) as entries:
            return sorted(Path(e.path) for e in entries
                          if e.name.startswith(prefix) and "release" in e.name.lower() and e.is_dir()), ""
    except OSError as exc:
        return [], f"Couldn't read {root}: {exc}"


def _describe(folder: Path, result: MigrationFolder) -> None:
    result.path = str(folder)
    result.proof_path = str(folder / PROOF_FOLDER)
    try:
        with os.scandir(folder) as found:
            entries = [(e.name, e.is_dir()) for e in found]
    except OSError as exc:
        result.note = f"Couldn't list the folder: {exc}"
        return
    result.exists = True
    entries.sort(key=lambda e: (not e[1], e[0].lower()))
    result.contents = [name + ("\\" if is_dir else "") for name, is_dir in entries]
    result.proof_exists = any(is_dir and name.lower() == PROOF_FOLDER.lower() for name, is_dir in entries)
    if any(not is_dir and name.lower() == SHORTCUT.lower() for name, is_dir in entries):
        result.list_url = read_shortcut(result.path)
    if result.proof_exists:
        try:
            with os.scandir(result.proof_path) as found:
                result.proof_items = sum(1 for _ in found)
        except OSError:
            pass


def lookup_change(prod_date: date, root: str) -> ChangeLookup:
    """Email for `prod_date` and the '<root>\\YYYY-MM-DD Release NNN' folder. Runs on a worker thread."""
    folders, note = _folder_candidates(prod_date, root)
    folder = MigrationFolder(note=note)
    pythoncom, client = _com()
    pythoncom.CoInitialize()
    try:
        namespace = _namespace(client)
        item, received = _find_item(namespace.GetDefaultFolder(INBOX), prod_date)
        email = None
        if item is not None:
            subject = item.Subject or ""
            change = _CHANGE.search(subject)
            same_subject = item.Parent.Items.Restrict(f"@SQL=\"{SUBJECT}\" = '{_quote(subject)}'")
            email = ChangeEmail(
                subject=subject, change=change[0].upper() if change else "", migration_date=prod_date,
                received=received, deadline=_deadline(same_subject, prod_date),
                entry_id=item.EntryID, store_id=item.Parent.StoreID,
            )
            # Several folders for the date: prefer the one the email names.
            named = [p for p in folders if _body_has(same_subject, p.name)]
            if named:
                folders = named + [p for p in folders if p not in named]
                folder.in_email = True
        if len(folders) > 1:
            folder.note = "Several folders match: " + ", ".join(p.name for p in folders)
        if folders:
            _describe(folders[0], folder)
        log.info("change email for %s: %s, deadline %s; folder %s (in email: %s)", prod_date,
                 email.change if email else None, email.deadline if email else None, folder.path, folder.in_email)
        return ChangeLookup(email, folder, find_sent(namespace, prod_date))
    finally:
        pythoncom.CoUninitialize()


def open_email(entry_id: str, store_id: str) -> None:
    pythoncom, client = _com()
    pythoncom.CoInitialize()
    try:
        _namespace(client).GetItemFromID(entry_id, store_id).Display()
    finally:
        pythoncom.CoUninitialize()


def open_path(path: str) -> None:
    os.startfile(path)  # type: ignore[attr-defined]
