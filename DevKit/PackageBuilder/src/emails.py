"""The 3 Days before emails: built from the team's Outlook templates, shown for review, never sent by the app.

Template HTML is read with extract_msg rather than through Outlook, because reading a message body through
Outlook triggers its "A program is trying to access email address information" prompt.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .business_days import add_business_days

log = logging.getLogger("pnc.emails")

INBOX, SENT = 6, 5  # olFolderInbox, olFolderSentMail
SUBJECT = "urn:schemas:httpmail:subject"
RECEIVED = "urn:schemas:httpmail:datereceived"
SENT_ON = "urn:schemas:httpmail:date"
# The code freeze subject never changes, so only emails this close to the migration count.
FREEZE_WINDOW_DAYS = 10
LIST_WINDOW_DAYS = 45

MIGRATION_LIST = "migration_list"
CODE_FREEZE = "code_freeze"


class EmailError(Exception):
    pass


@dataclass(frozen=True)
class EmailKind:
    id: str
    title: str
    template: str


KINDS = (
    EmailKind(MIGRATION_LIST, "Migration list", "11-Migration_list.msg"),
    EmailKind(CODE_FREEZE, "ACCP code freeze", "26-ACCP_Code_Freeze_New.msg"),
)
FREEZE_SUBJECT = "UniFi ACCP Refresh and Code Freeze"


@dataclass
class SentEmail:
    subject: str
    when: datetime
    folder: str  # "Inbox" or "Sent Items"
    entry_id: str
    store_id: str


@dataclass
class EmailDraft:
    kind: EmailKind
    template_path: str
    subject: str
    html: str


def list_subject(prod_date: date, sprint: str) -> str:
    return f"{prod_date:%Y%m%d} Migration list sprint {sprint}"


def _day(value: date) -> str:
    # "Monday Oct 19, 2026", as in past code freeze emails.
    return f"{value:%A %b %d, %Y}"


def _read_template(path: Path) -> str:
    try:
        import extract_msg
    except ImportError as exc:
        raise EmailError("extract-msg is not installed - run: pip install -e .") from exc
    if not path.is_file():
        raise EmailError(f"Email template not found: {path}")
    message = extract_msg.Message(str(path))
    try:
        raw = message.htmlBody
    finally:
        message.close()
    if not raw:
        raise EmailError(f"{path.name} has no HTML body.")
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw


def _replace(html: str, pattern: str, new: str, what: str, template: str) -> str:
    result, count = re.subn(pattern, lambda _m: new, html, count=1, flags=re.S)
    if not count:
        raise EmailError(f"Couldn't find {what} in {template} - the template may have changed.")
    return result


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def migration_list_draft(templates: str, prod_date: date, sprint: str, list_url: str) -> EmailDraft:
    kind = KINDS[0]
    path = Path(templates) / kind.template
    html = _read_template(path)
    link = _escape(list_url)
    # "Link to folder with spreadsheet:  <i>refresh the link</i>" + the old L:\SAL...MigrationList.xlsx line.
    html = _replace(html, r"(Link to folder with spreadsheet:).*?</p>\s*<p class=MsoNormal>(<span[^>]*>).*?"
                          r"MigrationList\.xlsx(<o:p></o:p></span></p>)",
                    "Link to folder with spreadsheet:<o:p></o:p></span></p><p class=MsoNormal>"
                    f"<span style='font-size:12.0pt'><a href=\"{link}\">{link}</a><o:p></o:p></span></p>",
                    "the spreadsheet link", kind.template)
    return EmailDraft(kind, str(path), list_subject(prod_date, sprint), html)


def code_freeze_draft(templates: str, prod_date: date, code_freeze: date, today: date,
                      holidays: Collection[date] = ()) -> EmailDraft:
    kind = KINDS[1]
    path = Path(templates) / kind.template
    html = _read_template(path)
    same_week = prod_date.isocalendar()[:2] == today.isocalendar()[:2]
    when = "this week" if same_week else "next week"
    html = _replace(html, r"In preparation for our Production migration next week,",
                    f"In preparation for our Production migration {when},", "the opening sentence", kind.template)
    # The highlighted placeholder: "UniFi ACCP DB has been Refreshed and, Code Freeze started at 5:00 AM Pacific
    # Monday</span> Apr 24, 2026"
    html = _replace(html, r"<span style='background:yellow;mso-highlight:yellow'>UniFi ACCP DB has been Refreshed "
                          r"and, Code Freeze started at 5:00 AM Pacific \w+</span>\s*\w{3} \d{1,2}, \d{4}",
                    f"UniFi ACCP DB will be Refreshed and, Code Freeze will start at 5:00 AM Pacific {_day(code_freeze)}",
                    "the code freeze date", kind.template)
    days = 0
    day = code_freeze
    while day < prod_date:
        days += 1
        day = add_business_days(day, 1, holidays)
    in_effect = (f"The code freeze in ACCP will be in effect for the following {days} day{'s' if days != 1 else ''}, "
                 f"until the PROD migration on {_day(prod_date)}.")
    html = _replace(html, r"(<p class=MsoNormal>)(Code freeze will be lifted after a successful production "
                          r"implementation\.)",
                    f"<p class=MsoNormal>{in_effect}<o:p></o:p></p><p class=MsoNormal>Code freeze will be lifted "
                    "after a successful production implementation.",
                    "the 'Code freeze will be lifted' sentence", kind.template)
    return EmailDraft(kind, str(path), FREEZE_SUBJECT, html)


def _com():
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise EmailError("pywin32 is not installed - run: pip install -e .") from exc
    return pythoncom, win32com.client


def _date(value: datetime) -> str:
    return value.strftime("%m/%d/%Y %I:%M %p")


def _local(value) -> datetime:
    return datetime(value.year, value.month, value.day, value.hour, value.minute, value.second)


def _search(namespace, query: str) -> SentEmail | None:
    """Latest match in Inbox or Sent Items; only unguarded properties are read."""
    best: SentEmail | None = None
    for folder_id, name, field in ((INBOX, "Inbox", RECEIVED), (SENT, "Sent Items", SENT_ON)):
        folder = namespace.GetDefaultFolder(folder_id)
        try:
            items = folder.Items.Restrict(query.format(date=field))
        except Exception as exc:
            log.warning("search in %s failed: %s", name, exc)
            continue
        for item in items:
            try:
                when = _local(item.ReceivedTime if folder_id == INBOX else item.SentOn)
                found = SentEmail(item.Subject or "", when, name, item.EntryID, folder.StoreID)
            except Exception:
                continue
            if best is None or found.when > best.when:
                best = found
    return best


def find_sent(namespace, prod_date: date) -> dict[str, SentEmail | None]:
    """Emails already sent for this migration, so they aren't sent twice. Runs on a worker thread."""
    end = datetime.combine(prod_date + timedelta(days=1), datetime.min.time())
    list_since = datetime.combine(prod_date - timedelta(days=LIST_WINDOW_DAYS), datetime.min.time())
    freeze_since = datetime.combine(prod_date - timedelta(days=FREEZE_WINDOW_DAYS), datetime.min.time())
    stamp = f"{prod_date:%Y%m%d}"
    found = {
        MIGRATION_LIST: _search(namespace, (
            f"@SQL=\"{SUBJECT}\" like '{stamp} Migration list%' "
            f"AND \"{{date}}\" >= '{_date(list_since)}' AND \"{{date}}\" < '{_date(end)}'")),
        CODE_FREEZE: _search(namespace, (
            f"@SQL=\"{SUBJECT}\" = '{FREEZE_SUBJECT}' "
            f"AND \"{{date}}\" >= '{_date(freeze_since)}' AND \"{{date}}\" < '{_date(end)}'")),
    }
    log.info("sent emails for %s: %s", prod_date, {k: v and (v.folder, v.when) for k, v in found.items()})
    return found


def show_draft(draft: EmailDraft) -> None:
    """Open the email from its template in Outlook for review. The user clicks Send."""
    pythoncom, client = _com()
    pythoncom.CoInitialize()
    try:
        try:
            outlook = client.Dispatch("Outlook.Application")
        except Exception as exc:
            raise EmailError(f"Couldn't open classic Outlook ({exc}).") from exc
        # The template keeps its recipients and formatting; only the subject and body are replaced.
        item = outlook.CreateItemFromTemplate(draft.template_path)
        item.Subject = draft.subject
        item.HTMLBody = draft.html
        item.Display(False)
        log.info("draft shown: %s", draft.subject)
    finally:
        pythoncom.CoUninitialize()
