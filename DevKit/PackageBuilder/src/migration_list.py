"""Adds the ACCP -> Main changesets to the migration's MigrationList_YYYY-MM-DD.xlsx on SharePoint, through Excel.

The workbook opens in the user's own Excel and is left open for review; nothing is saved by the app
(Excel's AutoSave may still save SharePoint files on its own).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .tfvc import Changeset

log = logging.getLogger("pnc.migration_list")

SHORTCUT = "_migration.url"
DATE_FORMAT = "m/d/yyyy h:mm:ss AM/PM"
EXCEL_EPOCH = datetime(1899, 12, 30)
XL_UP = -4162
# Excel refuses outside calls while a cell is being edited, a dialog is open, or it's busy (e.g. syncing).
BUSY = {-2147418111, -2147417846}  # RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER
BUSY_WAIT_SECONDS = 45


class MigrationListError(Exception):
    pass


@dataclass
class ListRow:
    id: int | str  # TFVC changeset number or short git commit ID
    author: str
    created: datetime | None  # local time, no time zone
    message: str


@dataclass
class SheetWrite:
    sheet: str
    added: list[int | str] = field(default_factory=list)
    skipped: list[int | str] = field(default_factory=list)  # already on the sheet
    first_row: int = 0
    last_row: int = 0


@dataclass
class ListWrite:
    workbook: str
    sheets: list[SheetWrite]


def read_shortcut(folder: str) -> str:
    """URL in the migration folder's _migration.url shortcut, or ''."""
    try:
        text = (Path(folder) / SHORTCUT).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = re.search(r"^URL=(\S+)", text, re.M)
    return found[1] if found else ""


def excel_path(url: str) -> str:
    """Sharing link ('/:x:/r/teams/...xlsx?d=...') -> the document URL Excel can open."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return url
    path = re.sub(r"^/:\w:/\w/", "/", unquote(parts.path))
    return f"{parts.scheme}://{parts.netloc}{path}"


def workbook_name(url: str) -> str:
    return excel_path(url).rsplit("/", 1)[-1] if url else ""


def _key(value) -> str:
    """Comparable column A value: changeset numbers as digits, commit IDs lower-case."""
    text = str(value if value is not None else "").strip().rstrip("*")
    try:
        number = float(text)
    except ValueError:
        return text.lower()
    return str(int(number)) if number.is_integer() else text.lower()


def _serial(value: datetime | None) -> float | None:
    """Excel date number; pywin32 would shift a plain datetime from local time to UTC."""
    return (value - EXCEL_EPOCH).total_seconds() / 86400 if value else None


def _open(excel, path: str):
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()

    def find():
        return next((b for b in excel.Workbooks if b.Name.lower() == name), None)

    book = find() or excel.Workbooks.Open(path) or find()
    if book is None:
        # Open returns nothing when Excel puts the file in Protected View (internet or Temp files).
        for window in excel.ProtectedViewWindows:
            if window.Workbook.Name.lower() == name:
                book = window.Edit()
                break
    if book is None:
        raise MigrationListError(f"Excel didn't open {name} - check Excel for a message or a Protected View bar.")
    return book


def _write_sheet(book, sheet: str, rows: list[ListRow]) -> SheetWrite:
    try:
        ws = book.Worksheets(sheet)
    except Exception as exc:
        raise MigrationListError(f"Sheet '{sheet}' not found in {book.Name}") from exc
    last = ws.Cells(ws.Rows.Count, 1).End(XL_UP).Row
    existing: set[str] = set()
    if last >= 2:
        values = ws.Range(ws.Cells(2, 1), ws.Cells(last, 1)).Value
        for row in values if isinstance(values, tuple) else ((values,),):
            if row[0] is not None:
                existing.add(_key(row[0]))
    result = SheetWrite(sheet, skipped=[r.id for r in rows if _key(r.id) in existing])
    new = [r for r in rows if _key(r.id) not in existing]
    if not new:
        return result
    first, end = last + 1, last + len(new)
    if any(isinstance(r.id, str) for r in new):
        # Commit IDs such as "1e512345" or "00123456" would otherwise become numbers.
        ws.Range(ws.Cells(first, 1), ws.Cells(end, 1)).NumberFormat = "@"
    ws.Range(ws.Cells(first, 3), ws.Cells(end, 3)).NumberFormat = DATE_FORMAT
    # Text format keeps comments such as "- fix" or "=..." from being read as formulas.
    ws.Range(ws.Cells(first, 4), ws.Cells(end, 4)).NumberFormat = "@"
    ws.Range(ws.Cells(first, 1), ws.Cells(end, 4)).Value = tuple(
        (r.id, r.author, _serial(r.created), r.message) for r in new)
    if str(ws.Cells(first, 3).Text).startswith("#"):
        ws.Columns(3).AutoFit()
    result.added, result.first_row, result.last_row = [r.id for r in new], first, end
    log.info("%s: added %d rows %d-%d, skipped %d", sheet, len(new), first, end, len(result.skipped))
    return result


def changeset_rows(changesets: list[Changeset]) -> list[ListRow]:
    return [ListRow(c.id, c.author, c.created, c.comment) for c in changesets]


def write_changesets(url: str, sheets: dict[str, list[ListRow]]) -> ListWrite:
    """Append rows whose ID isn't already in column A of each sheet. Runs on a worker thread."""
    try:
        import pythoncom
        import pywintypes
        import win32com.client
    except ImportError as exc:
        raise MigrationListError("pywin32 is not installed - run: pip install -e .") from exc
    path = excel_path(url)
    pythoncom.CoInitialize()
    try:
        deadline = time.monotonic() + BUSY_WAIT_SECONDS
        attempt = 0
        while True:
            attempt += 1
            try:
                return _write(win32com.client.Dispatch("Excel.Application"), path, sheets)
            except pywintypes.com_error as exc:
                # Writing again is safe: rows already added are skipped as duplicates.
                if exc.hresult not in BUSY or time.monotonic() >= deadline:
                    raise
                log.info("Excel busy (attempt %d), retrying", attempt)
                time.sleep(1)
    except pywintypes.com_error as exc:
        if exc.hresult in BUSY:
            raise MigrationListError(
                f"Excel stayed busy for {BUSY_WAIT_SECONDS} seconds. Press Esc in Excel if you're editing a cell, "
                "close any open dialog, then try again.") from exc
        detail = exc.excepinfo[2] if exc.excepinfo and exc.excepinfo[2] else exc.strerror
        raise MigrationListError(f"Excel: {detail}") from exc
    finally:
        pythoncom.CoUninitialize()


def _write(excel, path: str, sheets: dict[str, list[ListRow]]) -> ListWrite:
    excel.Visible = True
    book = _open(excel, path)
    if book.ReadOnly:
        raise MigrationListError(f"{book.Name} opened read-only - close it elsewhere or check your access.")
    results = [_write_sheet(book, sheet, rows) for sheet, rows in sheets.items()]
    shown = next((r for r in results if r.added), None)
    if shown is not None:
        ws = book.Worksheets(shown.sheet)
        ws.Activate()
        ws.Range(ws.Cells(shown.first_row, 1), ws.Cells(shown.last_row, 4)).Select()
    return ListWrite(book.Name, results)
