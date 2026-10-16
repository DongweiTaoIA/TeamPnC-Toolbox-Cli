from __future__ import annotations

import html
import re
import threading
from dataclasses import replace
from datetime import date
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QMainWindow, QMessageBox, QProgressBar,
    QSplitter,
    QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .ado import AdoClient, AdoError, Comment, TicketCreationResult, WorkItem
from .booking import FormSpec, booking_forms, refresh_forms, refresh_post
from .config import DATA_DIR, load_settings
from .emails import CODE_FREEZE, KINDS, MIGRATION_LIST, code_freeze_draft, list_subject, migration_list_draft, show_draft
from .migration_list import ListRow, ListWrite, changeset_rows, workbook_name, write_changesets
from .outlook import ChangeLookup, lookup_change, open_email, open_path
from .tfvc import SOLUTIONS, CompareResult, TfsSolution, compare_solution
from .tpa import COMPONENTS as TPA_COMPONENTS, ReleaseCommits, ReleaseList, ReleaseRun, TpaClient
from .schedule import MigrationEntry, pick_current_and_next, read_schedule
from .servicenow import ServiceNowRunner
from .sharepoint import SharePointDownloader
from .state import AppState
from .steps import DAYS_AFTER_PROD, DONE_STATES, PlannedStep, build_step_plan
from .teams import TeamsPoster, watch_loading
from .theme import CHIPS, DANGER, INFO, MUTED, SUCCESS, TEXT, TODAY_BG, WARNING
from .widgets import (
    BrowserPane, DetailsPane, MigrationCard, TodayBanner, UserBar, button, chip, display_name, dot_icon, fmt_date,
    html_chip, label, migration_label, person, relative_days, state_chip_color, web_profile,
)

ROLE_STEP = int(Qt.ItemDataRole.UserRole)
ROLE_CHECK_ID = ROLE_STEP + 1
SCHEDULE_CACHE = DATA_DIR / "schedule.xlsx"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
CARDS = (("current", "Current migration", False), ("next", "Next migration", True))
STATUS_COLORS = {"info": INFO, "busy": WARNING, "ok": SUCCESS, "error": DANGER}
STEP_COLORS = {"green": SUCCESS, "amber": WARNING, "red": DANGER, "blue": INFO}
RUN_MODES = {"test-run": "test", "run": "fill"}
RUN_TITLES = {"test": "Test run finished", "fill": "Run finished"}
# Discussion entries containing "<form title> ServiceNow request" mark that form as already submitted.
LOG_MARKER = "ServiceNow request"
TEAMS_MARKER = "Teams post"
# Steps whose Run also fills a Teams channel post after the ServiceNow requests.
TEAMS_POST_ACTIONS = {"dpaccp_refresh"}


class _Bridge(QObject):
    # Emitted from worker threads; the connected slot runs on the UI thread.
    finished = Signal(object, object, object, object)


class _Job(QRunnable):
    def __init__(self, bridge: _Bridge, fn, on_done, on_error):
        super().__init__()
        self._bridge, self._fn, self._on_done, self._on_error = bridge, fn, on_done, on_error

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:
            self._bridge.finished.emit(self._on_done, self._on_error, None, str(exc) or type(exc).__name__)
        else:
            self._bridge.finished.emit(self._on_done, self._on_error, result, None)


def _rows(pairs: list[tuple[str, str]]) -> str:
    cells = "".join(f"<tr><td class='k'>{html.escape(k)}</td><td>{v}</td></tr>" for k, v in pairs)
    return f"<table cellspacing='0'>{cells}</table>"


def _link(item: WorkItem) -> str:
    return f"<a href='{html.escape(item.web_url)}'>#{item.id}</a> {html.escape(item.title)}"


def _preview_value(value: str | bool) -> str:
    if isinstance(value, bool):
        return html_chip("Yes", "green") if value else html_chip("No", "slate")
    return html.escape(value).replace("\n", "<br>") or "<i>(blank)</i>"


def _form_preview(form: FormSpec) -> str:
    rows = "".join(
        f"<tr><td class='k'>{html.escape(f.label)}</td><td>{_preview_value(f.value)}</td></tr>"
        for f in form.fields
    )
    rows += "".join(
        f"<tr><td class='k'>Attachment</td><td><a href='{html.escape(a.url)}'>{html.escape(a.name)}</a>"
        " &nbsp;<i>(downloaded from SharePoint when the run starts)</i></td></tr>"
        for a in form.attachments
    )
    return f"<h4>ServiceNow \u00b7 {html.escape(form.title)}</h4><table cellspacing='0'>{rows}</table>"


def _status_chip(status: str) -> str:
    color = "green" if status == "filled" or status.startswith("appended") else (
        "red" if status.startswith("error") or status == "not found" else "amber")
    return html_chip(status, color)


def _step_status(due: date, done: bool, today: date) -> tuple[str, str]:
    if done:
        return "Done", "green"
    if due == today:
        return "Due today", "amber"
    if due < today:
        return "Overdue", "red"
    return "Upcoming", "blue"


def _comment_texts(comments: list[Comment]) -> list[str]:
    return [html.unescape(re.sub(r"<[^>]+>", "", c.text_html)) for c in comments]


def _logged_forms(comments: list[Comment], titles: list[str]) -> set[str]:
    texts = _comment_texts(comments)
    return {title for title in titles if any(f"{title} {LOG_MARKER}" in text for text in texts)}


def _logged_request(comments: list[Comment], title: str) -> str:
    for text in _comment_texts(comments):
        found = re.search(r"\bREQ\d+\b", text) if f"{title} {LOG_MARKER}" in text else None
        if found:
            return found.group()
    return ""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PnC Migration Tool")
        self.settings = load_settings()
        self.state = AppState()
        self._ado: AdoClient | None = None
        self._ado_lock = threading.Lock()
        self._pool = QThreadPool.globalInstance()
        self._bridge = _Bridge()
        self._bridge.finished.connect(self._on_job_finished)
        self._busy = 0
        self._populating = False
        self._downloading = False
        self._generation = 0
        self._children: dict[int, list[WorkItem]] = {}
        self._comments: dict[int, list[Comment]] = {}
        self._comments_loading: set[int] = set()
        self._creating_ticket_keys: set[str] = set()
        self._downloader: SharePointDownloader | None = None
        # (url, on_done, on_failed, on_sign_in) of the download in progress
        self._download_job: tuple = ("", None, None, None)
        self._hidden_page: QWebEnginePage | None = None
        self.selected: MigrationCard | None = None
        self.browser: BrowserPane | None = None
        # Separate pane so Teams stays loaded between runs instead of restarting each time.
        self.teams_browser: BrowserPane | None = None
        self._focus_step: str | None = None
        self._today = date.today()
        self._runner: ServiceNowRunner | None = None
        self._booking: dict | None = None
        self._user_email = ""
        self._full_names: dict[str, str] = {}
        self._names_loading: set[str] = set()
        # migration key -> Outlook/folder lookup result, or the error text
        self._changes: dict[str, ChangeLookup | str] = {}
        self._changes_loading: set[str] = set()
        # migration key -> ACCP -> Main comparison per solution, or the error text
        self._tfs_results: dict[str, list[CompareResult] | str] = {}
        self._tfs_running: set[str] = set()
        self._list_writes: dict[str, ListWrite | str] = {}
        self._list_writing: set[str] = set()
        self._drafting: set[str] = set()  # "<migration key>|<email kind>"
        # TPA pipeline runs per component key; selections and writes per (migration key, component key)
        self._tpa_lists: dict[str, ReleaseList | str] = {}
        self._tpa_loading: set[str] = set()
        self._tpa_selected: dict[tuple[str, str], ReleaseCommits | str] = {}
        self._tpa_busy: set[tuple[str, str]] = set()
        self._tpa_writes: dict[tuple[str, str], ListWrite | str] = {}
        self._resolving: set[int] = set()  # ADO task ids being resolved

        self._build_ui()
        QShortcut(QKeySequence(Qt.Key.Key_F5), self).activated.connect(self._load_schedule)
        QTimer.singleShot(0, self._load_schedule)
        QTimer.singleShot(0, self._load_user)
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._check_date)
        self._clock.start(60_000)

    # ---------- layout ----------
    def _build_ui(self) -> None:
        root = QWidget(objectName="Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(16)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(16)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)

        # Left column: Current / Next tabs above the task checklist
        left_column = QWidget()
        column_layout = QVBoxLayout(left_column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(12)
        tabs = QHBoxLayout()
        tabs.setSpacing(10)
        self.cards = [MigrationCard(tone, badge, compact) for tone, badge, compact in CARDS]
        for card in self.cards:
            card.clicked.connect(self._on_card_clicked)
            # Current gets more room than Next.
            tabs.addWidget(card, 2 if card.compact else 3)
        column_layout.addLayout(tabs)
        self.today_banner = TodayBanner()
        self.today_banner.picked.connect(self._pick_today)
        column_layout.addWidget(self.today_banner)

        left = QFrame(objectName="Panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(16, 14, 16, 10)
        left_layout.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(label("Tasks", "section"))
        head.addStretch()
        self._tasks_hint = label("", "muted")
        head.addWidget(self._tasks_hint)
        left_layout.addLayout(head)
        self._task_progress = QProgressBar(objectName="TaskProgress", textVisible=False)
        self._task_progress.setRange(0, 1)
        self._task_progress.setValue(0)
        left_layout.addWidget(self._task_progress)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Task", "Due", "ADO"])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setIndentation(18)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.currentItemChanged.connect(self._on_task_selected)
        self.tree.itemDoubleClicked.connect(lambda _item, _col: self._open_current_task())
        left_layout.addWidget(self.tree, 1)
        column_layout.addWidget(left, 1)
        splitter.addWidget(left_column)

        # Right column: signed-in user above task information or the embedded browser
        right_column = QWidget()
        right_column_layout = QVBoxLayout(right_column)
        right_column_layout.setContentsMargins(0, 0, 0, 0)
        right_column_layout.setSpacing(10)
        self.user_bar = UserBar()
        right_column_layout.addWidget(self.user_bar)
        right_column_layout.addWidget(QFrame(objectName="Divider"))
        right = QFrame(objectName="Panel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.details = DetailsPane()
        self.details.open_url.connect(self._open_in_browser)
        self.details.action.connect(self._on_details_action)
        self.stack.addWidget(self.details)
        right_layout.addWidget(self.stack)
        right_column_layout.addWidget(right, 1)
        splitter.addWidget(right_column)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([580, 820])

        # Bottom: status
        self._status_dot = label("\u25cf")
        self._status_label = label()
        self._notice = chip("", "amber")
        self._notice.hide()
        self._progress = QProgressBar(textVisible=False)
        self._progress.setRange(0, 0)
        self._progress.setFixedWidth(160)
        self._progress.hide()
        refresh = button("Refresh  (F5)", role="link")
        refresh.clicked.connect(self._load_schedule)
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        bar.addWidget(self._status_dot)
        bar.addWidget(self._status_label, 1)
        bar.addPermanentWidget(self._notice)
        bar.addPermanentWidget(self._progress)
        bar.addPermanentWidget(refresh)
        bar.addPermanentWidget(chip(f"ADO  {self.settings.ado_org} / {self.settings.ado_project}", "violet"))

    # ---------- background jobs ----------
    def _begin(self, text: str) -> None:
        self._busy += 1
        self._progress.show()
        self._status(text, "busy")

    def _end(self) -> None:
        self._busy = max(0, self._busy - 1)
        if self._busy == 0:
            self._progress.hide()
            self._status("Ready", "ok")

    def _run(self, busy_text: str, fn, on_done, on_error=None) -> None:
        self._begin(busy_text)
        self._pool.start(_Job(self._bridge, fn, on_done, on_error))

    @Slot(object, object, object, object)
    def _on_job_finished(self, on_done, on_error, result, error) -> None:
        self._end()
        if error is None:
            on_done(result)
        elif on_error is not None:
            on_error(error)
        else:
            self._status(f"Error: {error}", "error")
            QMessageBox.critical(self, "PnC Migration Tool", error)

    def _status(self, text: str, level: str = "info") -> None:
        self._status_label.setText(text)
        self._status_dot.setStyleSheet(f"color: {STATUS_COLORS[level]}; font-size: 12pt;")

    def _client(self) -> AdoClient:
        with self._ado_lock:
            if self._ado is None:
                self._ado = AdoClient(self.settings.ado_org, self.settings.ado_project)
            return self._ado

    # ---------- signed-in user ----------
    def _load_user(self) -> None:
        self._run("Connecting to Azure DevOps\u2026", lambda: self._client().get_current_user(),
                  self._on_user, self.user_bar.set_error)

    def _on_user(self, user: tuple[str, str]) -> None:
        self.user_bar.set_user(*user)
        self._user_email = user[1]
        self._update_roles()

    def _update_roles(self) -> None:
        card, first = self.selected, self.user_bar.first_name.lower()
        roles: list[tuple[str, str]] = []
        if card is not None and card.entry is not None and first:
            e = card.entry
            for name, text, color in (
                (e.builder, "Package builder", "blue"), (e.builder_backup, "Builder backup", "slate"),
                (e.deployer, "PROD deployer", "violet"), (e.deployer_backup, "Deployer backup", "slate"),
            ):
                if name.strip().lower().split(" ")[0] == first:
                    roles.append((text, color))
        self.user_bar.set_roles(roles)

    def _on_details_action(self, action: str) -> None:
        if action == "create-ticket" and self.selected is not None:
            self._create_ticket(self.selected)
        elif action == "change-ticket" and self.selected is not None:
            self._change_ticket(self.selected)
        elif action == "add-log" and self.selected is not None:
            self._add_log(self.selected)
        elif action in RUN_MODES:
            self._start_booking(RUN_MODES[action])
        elif action == "test-teams":
            self._start_teams_test()
        elif action == "check-email" and self.selected is not None:
            self._ensure_change(self.selected, force=True)
        elif action == "start-3days":
            self._start_three_days()
        elif action == "resolve-3days":
            self._resolve_three_days()
        elif action == "write-list":
            self._write_list()
        elif action.startswith("draft-email/"):
            self._draft_email(action.split("/", 1)[1])
        elif action.startswith("open-sent/"):
            self._open_sent(action.split("/", 1)[1])
        elif action.startswith("tpa-"):
            verb, _, rest = action.partition("/")
            component, _, run = rest.partition("/")
            if verb == "tpa-reload":
                self._ensure_tpa(force=True)
            elif verb == "tpa-select" and run.isdigit():
                self._tpa_select(component, int(run))
            elif verb == "tpa-copy":
                self._tpa_copy(component)
            elif verb == "tpa-write":
                self._tpa_write(component)
        elif action.startswith("tfs-path/"):
            self._change_tfs_path(action.split("/", 1)[1])
        elif action in {"open-email", "open-folder", "open-proof"}:
            self._open_change_item(action)

    # ---------- migration schedule ----------
    def _load_schedule(self) -> None:
        if self._downloading or self._runner is not None:
            return
        self._generation += 1
        self.selected = None
        self._comments.clear()
        self._populating = True
        self.tree.clear()
        self._populating = False
        self._tasks_hint.setText("")
        self._task_progress.setValue(0)
        self._notice.hide()
        for card in self.cards:
            card.set_selected(False)
            card.set_loading()
        self.today_banner.show_message("Checking today's tasks\u2026")
        self.details.show_content("Loading\u2026", "Migration schedule",
                                  "<p>Loading the migration schedule and Azure DevOps tickets\u2026</p>")
        self.stack.setCurrentWidget(self.details)

        local = self.settings.excel_path
        if local and Path(local).is_file():
            self._read_schedule(local, "local file")
        elif self.settings.schedule_download_url:
            self._download_schedule()
        else:
            self._schedule_failed("Set schedule_download_url (or excel_path) in config.json.")

    def _download_schedule(self) -> None:
        def sign_in() -> None:
            for card in self.cards:
                card.set_message("Sign-in required",
                                 "Sign in to SharePoint on the right to load the migration schedule.")

        self._download(self.settings.schedule_download_url, SCHEDULE_CACHE,
                       "Downloading migration schedule from SharePoint\u2026",
                       lambda path: self._read_schedule(path, "SharePoint"), self._on_download_failed, sign_in)

    def _download(self, url: str, target: Path, busy_text: str, on_done, on_failed, on_sign_in=None) -> None:
        self._downloading = True
        self._download_job = (url, on_done, on_failed, on_sign_in)
        self._begin(busy_text)
        profile = web_profile()
        if self._downloader is None:
            self._downloader = SharePointDownloader(profile, parent=self)
            self._downloader.finished.connect(lambda path: self._on_download_done(path, None))
            self._downloader.failed.connect(lambda error: self._on_download_done("", error))
            self._downloader.sign_in_required.connect(self._on_sign_in_required)
        self._downloader.start(target)
        self._hidden_page = QWebEnginePage(profile, self)
        self._hidden_page.urlChanged.connect(self._downloader.watch_url)
        self._hidden_page.load(QUrl(url))

    def _finish_download(self) -> None:
        self._downloading = False
        self._end()
        if self._hidden_page is not None:
            self._hidden_page.deleteLater()
            self._hidden_page = None
        if self.browser is not None and self.stack.currentWidget() is self.browser:
            self.stack.setCurrentWidget(self.details)

    def _on_download_done(self, path: str, error: str | None) -> None:
        _, on_done, on_failed, _ = self._download_job
        self._finish_download()
        if error is None:
            on_done(path)
        else:
            on_failed(error)

    def _on_download_failed(self, error: str) -> None:
        if SCHEDULE_CACHE.is_file():
            self._notice.setText("Using cached schedule")
            self._notice.setToolTip(f"SharePoint download failed: {error}")
            self._notice.show()
            self._read_schedule(str(SCHEDULE_CACHE), "cached copy")
        else:
            self._schedule_failed(f"Couldn't download the migration schedule from SharePoint: {error}")

    def _on_sign_in_required(self) -> None:
        url, _, _, on_sign_in = self._download_job
        if on_sign_in is not None:
            on_sign_in()
        self._open_in_browser(url)
        self._status("Waiting for SharePoint sign-in\u2026", "busy")

    def _schedule_failed(self, message: str) -> None:
        for card in self.cards:
            card.set_message("Schedule unavailable", message)
        self.today_banner.show_message("Schedule unavailable")
        self.details.show_content("Migration schedule unavailable", "Error", f"<p>{html.escape(message)}</p>",
                                  tone="next")
        self._status(message, "error")

    def _read_schedule(self, path: str, source: str) -> None:
        generation = self._generation
        self._run(f"Reading migration schedule ({source})\u2026",
                  lambda: read_schedule(path, self.settings),
                  lambda entries: self._on_schedule(entries, source, generation),
                  lambda error: self._schedule_failed(f"Couldn't read the migration schedule: {error}"))

    def _on_schedule(self, entries: list[MigrationEntry], source: str, generation: int) -> None:
        if generation != self._generation:
            return
        picked = pick_current_and_next(entries, date.today(), DAYS_AFTER_PROD, self.settings.holidays)
        for index, card in enumerate(self.cards):
            if index < len(picked):
                card.set_entry(picked[index])
            else:
                card.set_message("No migration", "No further migration found in the schedule.")
        self._status(f"Loaded {len(entries)} migrations from the {source}.", "ok")
        if not picked:
            self.today_banner.show_message("No upcoming migrations")
            self.details.show_content("No upcoming migrations", "Migration schedule",
                                      "<p>The schedule has no current or upcoming migration.</p>")
            return
        due, _ = self._agenda()
        # Open on today's task when there is one.
        self._focus_step = due[0][1].step.name if due else None
        self._select_card(due[0][0] if due else self.cards[0])
        self._refresh_today()
        for card in self.cards[:len(picked)]:
            self._lookup_ticket(card)

    # ---------- Azure DevOps tickets ----------
    def _lookup_ticket(self, card: MigrationCard) -> None:
        entry = card.entry
        override = self.state.ticket_id(entry.key)
        prod_date, prefix, root = entry.prod_date, self.settings.ado_title_prefix, self.settings.ado_iteration_root

        def fn() -> list[WorkItem]:
            client = self._client()
            if override:
                try:
                    return [client.get_work_item(override)]
                except AdoError:
                    pass
            return client.find_migration_tickets(prod_date, prefix, root)
        self._run(f"Looking up '{prefix} {prod_date:%b} {prod_date.day}' in Azure DevOps\u2026", fn,
                  lambda items: self._on_tickets(card, entry, items),
                  lambda error: self._on_ticket_error(card, entry, error))

    def _on_tickets(self, card: MigrationCard, entry: MigrationEntry, items: list[WorkItem]) -> None:
        if card.entry is not entry:
            return
        if items:
            card.set_ticket(items[0], others=len(items) - 1)
        else:
            card.set_ticket_status("No ticket", "amber", f"No '{self.settings.ado_title_prefix} "
                                   f"{entry.prod_date:%b %d}' ticket found on the team board.")
        if card is self.selected:
            self._refresh_details(card)
        # Load tasks for both migrations so today's agenda reflects ADO states.
        self._load_tasks(card)
        self._refresh_today()

    def _on_ticket_error(self, card: MigrationCard, entry: MigrationEntry, error: str) -> None:
        if card.entry is entry:
            card.set_ticket_status("ADO error", "red", error)
            if card is self.selected:
                self._refresh_details(card)
        self._status(f"Azure DevOps error: {error}", "error")

    def _change_ticket(self, card: MigrationCard) -> None:
        if card.entry is None:
            return
        current = card.ticket.id if card.ticket else 1
        value, ok = QInputDialog.getInt(
            self, "Azure DevOps ticket", f"Work item ID for {card.entry.sprint}:", current, 1, 99_999_999)
        if not ok:
            return
        self.state.set_ticket_id(card.entry.key, value)
        card.set_ticket_status("ADO\u2026", "slate", "Loading ticket\u2026")
        self._lookup_ticket(card)

    def _create_ticket(self, card: MigrationCard) -> None:
        entry = card.entry
        if entry is None or card.ticket is not None or entry.key in self._creating_ticket_keys:
            return
        self._creating_ticket_keys.add(entry.key)
        card.set_ticket_status("ADO\u2026", "slate", "Checking for an existing ticket before creating\u2026")
        if card is self.selected:
            self._refresh_details(card)
        template = self.cards[0].ticket if self.cards else None
        self._run(
            f"Checking Azure DevOps before creating the {entry.sprint} ticket\u2026",
            lambda: self._client().create_migration_ticket_if_missing(
                entry.prod_date, self.settings.ado_title_prefix, self.settings.ado_iteration_root,
                template, entry.builder,
            ),
            lambda result: self._on_ticket_created(card, entry, result),
            lambda error: self._on_ticket_creation_error(card, entry, error),
        )

    def _on_ticket_created(self, card: MigrationCard, entry: MigrationEntry, result: TicketCreationResult) -> None:
        self._creating_ticket_keys.discard(entry.key)
        if card.entry is not entry:
            return
        self.state.set_ticket_id(entry.key, result.ticket.id)
        if not result.created:
            self._on_tickets(card, entry, [result.ticket])
            card.ticket_note = result.note
            if card is self.selected:
                self._refresh_details(card)
            self._status(result.note, "ok")
            return

        card.set_ticket(result.ticket)
        card.ticket_note = result.note
        self._children.pop(result.ticket.id, None)
        if card is self.selected:
            self._refresh_details(card)
        self._load_tasks(card)
        self._refresh_today()
        summary = f"Created ADO #{result.ticket.id}; cloned {result.tasks_cloned}/{result.tasks_total} tasks."
        self._status(f"{summary} {result.note}".strip(), "error" if result.note else "ok")

    def _on_ticket_creation_error(self, card: MigrationCard, entry: MigrationEntry, error: str) -> None:
        self._creating_ticket_keys.discard(entry.key)
        if card.entry is not entry:
            return
        card.set_ticket_status("ADO error", "red", error)
        if card is self.selected:
            self._refresh_details(card)
        self._status(f"Ticket creation failed: {error}", "error")

    # ---------- ticket discussion (log) ----------
    def _log_html(self, ticket: WorkItem) -> str:
        add = "&nbsp; <a href='action:add-log'>Add log entry</a>"
        comments = self._comments.get(ticket.id)
        if comments is None:
            self._load_comments(ticket.id)
            return f"<h4>Log</h4><p><i>Loading discussion\u2026</i>{add}</p>"
        if not comments:
            return f"<h4>Log</h4><p><i>No entries yet.</i>{add}</p>"
        entries = "".join(
            f"<p><b>{html.escape(c.author)}</b> &nbsp;"
            f"<span style='color:{MUTED}'>{c.created.astimezone():%b %d, %Y %H:%M}</span></p>{c.text_html}"
            if c.created else f"<p><b>{html.escape(c.author)}</b></p>{c.text_html}"
            for c in comments
        )
        # Comment HTML is rendered by QTextBrowser, which does not run scripts.
        return f"<h4>Log</h4><p>{add.removeprefix('&nbsp; ')}</p>{entries}"

    def _load_comments(self, ticket_id: int) -> None:
        if ticket_id in self._comments_loading:
            return
        self._comments_loading.add(ticket_id)
        self._run(f"Loading log for #{ticket_id}\u2026",
                  lambda: self._client().get_comments(ticket_id),
                  lambda comments: self._on_comments(ticket_id, comments),
                  lambda error: self._on_comments_error(ticket_id, error))

    def _on_comments(self, ticket_id: int, comments: list[Comment]) -> None:
        self._comments_loading.discard(ticket_id)
        self._comments[ticket_id] = comments
        card = self.selected
        if card is not None and card.ticket and card.ticket.id == ticket_id and self.tree.currentItem() is None:
            self._show_migration(card)

    def _on_comments_error(self, ticket_id: int, error: str) -> None:
        self._comments_loading.discard(ticket_id)
        # Cache an empty log so rendering doesn't retry in a loop; F5 reloads.
        self._comments[ticket_id] = []
        self._status(f"Couldn't load the log for #{ticket_id}: {error}", "error")

    def _add_log(self, card: MigrationCard) -> None:
        ticket = card.ticket
        if ticket is None:
            return
        text, ok = QInputDialog.getMultiLineText(self, "Add log entry", f"Log entry for ADO #{ticket.id}:")
        text = text.strip()
        if not ok or not text:
            return
        body = html.escape(text).replace("\n", "<br>")
        ticket_id = ticket.id
        self._run(f"Adding log entry to #{ticket_id}\u2026",
                  lambda: self._client().add_comment(ticket_id, body),
                  lambda comment: self._on_log_added(ticket_id, comment),
                  lambda error: self._status(f"Couldn't add the log entry: {error}", "error"))

    def _on_log_added(self, ticket_id: int, comment: Comment) -> None:
        self._comments.setdefault(ticket_id, []).insert(0, comment)
        self._status(f"Log entry added to #{ticket_id}.", "ok")
        card = self.selected
        if card is not None and card.ticket and card.ticket.id == ticket_id:
            self._show_migration(card)

    def _on_card_clicked(self, card: MigrationCard) -> None:
        self._focus_step = None
        self._select_card(card)

    def _select_card(self, card: MigrationCard) -> None:
        if card.entry is None:
            return
        self.selected = card
        for other in self.cards:
            other.set_selected(other is card)
        self._update_roles()
        self._show_migration(card)
        self._load_tasks(card)

    # ---------- today's agenda ----------
    def _is_done(self, card: MigrationCard, planned: PlannedStep) -> bool:
        key, step = card.entry.key, planned.step
        if step.checklist:
            return all(self.state.get_check(key, f"{step.name}::{text}") for text in step.checklist)
        return bool(planned.task and planned.task.state.lower() in DONE_STATES)

    def _agenda(self) -> tuple[list, list]:
        """(due today or overdue, upcoming) open steps across the loaded migrations, by due date."""
        today = date.today()
        due, upcoming = [], []
        for card in self.cards:
            if card.entry is None:
                continue
            children = self._children.get(card.ticket.id, []) if card.ticket else []
            for planned in build_step_plan(card.entry.prod_date, children, self.settings.holidays):
                if not self._is_done(card, planned):
                    (due if planned.due_date <= today else upcoming).append((card, planned))
        by_date = lambda pair: pair[1].due_date
        return sorted(due, key=by_date), sorted(upcoming, key=by_date)

    def _refresh_today(self) -> None:
        due, upcoming = self._agenda()
        self.today_banner.show_agenda(due, upcoming[0] if upcoming else None)

    def _pick_today(self, card: MigrationCard, step_name: str) -> None:
        self._focus_step = step_name
        self._select_card(card)

    def _check_date(self) -> None:
        if date.today() != self._today:
            self._today = date.today()
            self._load_schedule()

    # ---------- task checklist ----------
    def _load_tasks(self, card: MigrationCard) -> None:
        ticket = card.ticket
        cached = self._children.get(ticket.id) if ticket else None
        if card is self.selected:
            self._populate_tree(card, cached or [])
        if ticket is None or cached is not None:
            return
        ticket_id = ticket.id
        self._run(f"Loading tasks for #{ticket_id}\u2026",
                  lambda: self._client().get_children(ticket_id),
                  lambda children: self._on_children(card, ticket_id, children))

    def _on_children(self, card: MigrationCard, ticket_id: int, children: list[WorkItem]) -> None:
        self._children[ticket_id] = children
        if card is self.selected and card.ticket and card.ticket.id == ticket_id:
            self._populate_tree(card, children)
        self._refresh_today()

    def _populate_tree(self, card: MigrationCard, children: list[WorkItem]) -> None:
        key, today = card.entry.key, date.today()
        bold = QFont(self.tree.font())
        bold.setBold(True)
        self._populating = True
        self.tree.clear()
        for planned in build_step_plan(card.entry.prod_date, children, self.settings.holidays):
            step, task = planned.step, planned.task
            item = QTreeWidgetItem([step.name, planned.due_date.strftime("%b %d"), task.state if task else "\u2014"])
            item.setData(0, ROLE_STEP, planned)
            item.setData(0, ROLE_CHECK_ID, step.name)
            # Read-only: the check mirrors the ADO task state.
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            done = bool(task and task.state.lower() in DONE_STATES)
            # Set before children are added so it doesn't cascade to them.
            item.setCheckState(0, Qt.CheckState.Checked if done else Qt.CheckState.Unchecked)
            for text in step.checklist:
                child = QTreeWidgetItem([text])
                child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                check_id = f"{step.name}::{text}"
                child.setData(0, ROLE_CHECK_ID, check_id)
                checked = bool(self.state.get_check(key, check_id))
                child.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                item.addChild(child)
            item.setFont(2, bold)
            if planned.due_date == today:
                for column in range(3):
                    item.setBackground(column, QColor(TODAY_BG))
                item.setFont(0, bold)
                item.setFont(1, bold)
            self.tree.addTopLevelItem(item)
            self._style_step(item, today)
        self.tree.expandAll()
        self._populating = False
        self._update_progress(card)
        if self._focus_step:
            for i in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(i)
                if item.data(0, ROLE_CHECK_ID) == self._focus_step:
                    self.tree.setCurrentItem(item)
                    break

    @staticmethod
    def _style_step(item: QTreeWidgetItem, today: date) -> None:
        planned: PlannedStep = item.data(0, ROLE_STEP)
        done = item.checkState(0) == Qt.CheckState.Checked
        color = STEP_COLORS[_step_status(planned.due_date, done, today)[1]]
        item.setIcon(1, dot_icon(color))
        item.setForeground(1, QColor(color))
        item.setForeground(0, QColor(MUTED if done else TEXT))
        task = planned.task
        item.setForeground(2, QColor(CHIPS[state_chip_color(task.state)][1] if task else MUTED))

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._populating or column != 0 or self.selected is None:
            return
        check_id = item.data(0, ROLE_CHECK_ID)
        # Parents with a checklist derive their state from their children.
        if check_id and item.childCount() == 0:
            self.state.set_check(self.selected.entry.key, check_id, item.checkState(0) == Qt.CheckState.Checked)
        top = item.parent() or item
        # Styling also emits itemChanged.
        self._populating = True
        self._style_step(top, date.today())
        self._populating = False
        self._update_progress(self.selected)
        self._refresh_today()
        current = self.tree.currentItem()
        if current is not None and (current.parent() or current) is top:
            self._show_step(top)

    def _update_progress(self, card: MigrationCard) -> None:
        total = self.tree.topLevelItemCount()
        done = sum(
            1 for i in range(total)
            if self.tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked
        )
        card.set_progress(done, total)
        self._task_progress.setRange(0, max(total, 1))
        self._task_progress.setValue(done)
        ticket = f"  \u00b7  ADO #{card.ticket.id}" if card.ticket else ""
        self._tasks_hint.setText(f"{done} of {total} done{ticket}")

    def _on_task_selected(self, current: QTreeWidgetItem | None, _previous) -> None:
        if current is None or self._populating:
            return
        top = current.parent() or current
        # Keep the selection when the tree is rebuilt (e.g. after ADO tasks arrive).
        self._focus_step = top.data(0, ROLE_CHECK_ID)
        self._show_step(top)

    def _open_current_task(self) -> None:
        current = self.tree.currentItem()
        if current is None:
            return
        planned: PlannedStep = (current.parent() or current).data(0, ROLE_STEP)
        if planned.task:
            self._open_in_browser(planned.task.web_url)

    # ---------- right panel ----------
    def _refresh_details(self, card: MigrationCard) -> None:
        current = self.tree.currentItem()
        if current is not None:
            self._show_step(current.parent() or current)
        else:
            self._show_migration(card)

    def _show_migration(self, card: MigrationCard) -> None:
        e, t = card.entry, card.ticket
        if e is None:
            return
        pairs = [
            ("PROD migration", f"<b>{fmt_date(e.prod_date)}</b> &nbsp;{html_chip(relative_days(e.prod_date), 'blue')}"),
            ("Code freeze", fmt_date(e.code_freeze)),
            ("Package builder", html.escape(person(e.builder, e.builder_backup))),
            ("PROD deployer", html.escape(person(e.deployer, e.deployer_backup))),
        ]
        if t:
            change = "&nbsp; <a href='action:change-ticket'>Change</a>"
            pairs += [
                ("ADO ticket", f"{_link(t)} &nbsp;{html_chip(t.state, state_chip_color(t.state))}{change}"),
                ("Sprint", html.escape(t.iteration_path.rsplit("\\", 1)[-1])),
                ("Assigned to", html.escape(t.assigned_to or "\u2014")),
            ]
            if card.ticket_note:
                pairs.append(("Note", html.escape(card.ticket_note)))
        else:
            note = html.escape(card.ticket_note or "Not linked")
            action = "" if e.key in self._creating_ticket_keys else " &nbsp; <a href='action:create-ticket'>Create ticket</a>"
            pairs.append(("ADO ticket", f"{note}{action}"))
        pairs.append(("Schedule row", str(e.row)))
        body = _rows(pairs)
        if t:
            body += self._log_html(t)
        self.details.show_content(migration_label(e), card.badge, body, tone=card.tone)
        if self._runner is None and not (self._booking and self._booking.get("teams")):
            self.stack.setCurrentWidget(self.details)

    def _show_step(self, item: QTreeWidgetItem) -> None:
        planned: PlannedStep = item.data(0, ROLE_STEP)
        step, task = planned.step, planned.task
        done = item.checkState(0) == Qt.CheckState.Checked
        status, tone = _step_status(planned.due_date, done, date.today())
        offset = step.offset_days
        when = "PROD day" if offset == 0 else f"{abs(offset)} day{'s' if abs(offset) != 1 else ''} " + (
            "before PROD" if offset < 0 else "after PROD")
        pairs = [
            ("Status", html_chip(status, tone)),
            ("Due", f"<b>{fmt_date(planned.due_date)}</b> &nbsp;{html_chip(relative_days(planned.due_date), 'slate')}"),
            ("Timing", when),
        ]
        if task:
            pairs += [
                ("ADO task", f"{_link(task)} &nbsp;{html_chip(task.state, state_chip_color(task.state))}"),
                ("Assigned to", html.escape(task.assigned_to or "\u2014")),
            ]
        else:
            pairs.append(("ADO task", html_chip("no matching child task", "amber")))
        body = _rows(pairs)
        if step.checklist:
            items = "".join(f"<li>{html.escape(text)}</li>" for text in step.checklist)
            body += f"<h4>Checklist</h4><ul>{items}</ul>"
        card = self.selected
        actions: tuple = ()
        if step.action == "change_email" and card is not None and card.entry is not None:
            self._ensure_change(card)
            entry = card.entry
            missing = self._change_missing(entry)
            self._ensure_tpa()
            body = (self._change_html(entry) + self._start_html(missing, task, done) + self._tfs_html(entry)
                    + self._tpa_html(card, missing) + self._emails_html(card, missing) + body)
            loading = entry.key in self._changes_loading
            finished = done or bool(task and task.state.lower() in DONE_STATES)
            comparing = entry.key in self._tfs_running
            active = bool(task and task.state.lower() == "active")
            compared = self._tfs_results.get(entry.key)
            new = sum(len(r.included) for r in compared) if isinstance(compared, list) else 0
            list_url = self._list_url(entry)
            actions = (("check-email", "Check email again", not loading,
                        "Search Outlook again for the Action required - CHG email and re-check the folder."),
                       ("start-3days", "Compare again" if compared is not None else
                        "Get latest & compare" if active else "Start tasks",
                        not missing and task is not None and not finished and not comparing,
                        "Already done." if finished else "No matching ADO task." if task is None
                        else "Not ready: " + "; ".join(missing) if missing
                        else ("" if active else "Set the ADO task to Active, then ")
                        + "Get Latest of ACCP and Main and list the ACCP changesets not merged into Main. "
                          "Never merges."),
                       ("write-list", "Write to migration list",
                        bool(new) and bool(list_url) and not comparing and entry.key not in self._list_writing,
                        "Compare first." if compared is None else "No migration list link (_migration.url) "
                        "in the migration folder." if not list_url else
                        f"Add the changesets to {workbook_name(list_url)} in Excel; ones already listed are skipped."),
                       ("resolve-3days", "Resolve task",
                        task is not None and not finished and task.id not in self._resolving,
                        "Already resolved." if finished else "No matching ADO task." if task is None
                        else f"Set ADO task #{task.id} to Resolved and assign it to you."))
        elif step.action and card is not None and card.entry is not None:
            if step.action == "book_resources":
                self._ensure_full_names(card)
            forms = self._step_forms(step.action, card.entry)
            preview = "".join(_form_preview(form) for form in forms) or (
                "<p>" + html_chip("No code freeze date in the schedule - the request can't be built", "red") + "</p>")
            if step.action in TEAMS_POST_ACTIONS and forms:
                # Teams takes long to start; begin loading it in the background as soon as the task is viewed.
                self._ensure_teams()
                comments = self._comments.get(card.ticket.id, []) if card.ticket else []
                request = next((n for n in (_logged_request(comments, f.title) for f in forms) if n), "")
                subject, message = refresh_post(card.entry, request or "REQ\u2026 (added after you click Request)")
                where = f"{self.settings.teams_team} > {self.settings.teams_channel}"
                preview += (f"<h4>Teams \u00b7 {html.escape(where)}</h4>"
                            + _rows([("Subject", html.escape(subject)), ("Message", _preview_value(message))]))
            body = preview + body
            busy = self._runner is not None or self._booking is not None
            actions = ()
            if step.action in TEAMS_POST_ACTIONS:
                actions += (("test-teams", "Test Teams post", not busy and bool(forms),
                             "Skip ServiceNow: open the Teams channel and fill the post without posting it."),)
            actions += (("test-run", "Test run \u00b7 Save as Draft", not busy and bool(forms),
                        "Fill the ServiceNow form(s) and click Save as Draft. Never clicks Request."
                        + (" Then fills the Teams post without posting it." if step.action in TEAMS_POST_ACTIONS
                           else "")),)
            if step.action in {"book_resources", *TEAMS_POST_ACTIONS}:
                resolved = done or bool(task and task.state.lower() in DONE_STATES)
                teams = (" Then the Teams post is filled; you click Post." if step.action in TEAMS_POST_ACTIONS
                         else "")
                actions += (
                    ("run", "Run", not busy and not resolved and bool(forms),
                     "Already resolved - use Test run instead." if resolved
                     else "Fill each form; you review it and click Request. REQ numbers are logged in the "
                          f"ticket discussion.{teams} Then the task is resolved."),
                )
        self.details.show_content(step.name, migration_label(card.entry) if card and card.entry else "", body,
                                  tone=card.tone if card else "current", actions=actions)
        if self._runner is None and not (self._booking and self._booking.get("teams")):
            self.stack.setCurrentWidget(self.details)

    # ---------- ServiceNow booking ----------
    def _step_forms(self, action: str, entry: MigrationEntry) -> list[FormSpec]:
        if action == "book_resources":
            return booking_forms(entry, self.settings, self._full_names)
        if action == "dpaccp_refresh":
            return refresh_forms(entry, self.settings)
        return []

    def _missing_names(self, entry: MigrationEntry) -> list[str]:
        return [n for n in dict.fromkeys((entry.deployer, entry.deployer_backup))
                if n.strip() and n not in self._full_names]

    def _lookup_full_names(self, names: list[str]) -> dict[str, str]:
        """Runs on a worker thread; unknown names map to themselves."""
        client, result = self._client(), {}
        for name in names:
            try:
                found = client.find_person(name, self.settings.ado_iteration_root)
            except AdoError:
                found = None
            result[name] = display_name(found[1]) if found else name
        return result

    def _ensure_full_names(self, card: MigrationCard) -> None:
        names = [n for n in self._missing_names(card.entry) if n not in self._names_loading]
        if not names:
            return
        self._names_loading.update(names)

        def done(result: dict[str, str]) -> None:
            self._names_loading.difference_update(names)
            self._full_names.update(result)
            if card is self.selected:
                self._refresh_details(card)

        def failed(error: str) -> None:
            self._names_loading.difference_update(names)
            self._full_names.update({n: n for n in names})
            self._status(f"Couldn't look up full names: {error}", "error")

        self._run("Looking up full names\u2026", lambda: self._lookup_full_names(names), done, failed)

    # ---------- 3 days before: change email and migration folder ----------
    def _ensure_change(self, card: MigrationCard, force: bool = False) -> None:
        entry = card.entry
        key = entry.key
        if key in self._changes_loading or (key in self._changes and not force):
            return
        self._changes_loading.add(key)
        root = self.settings.migration_root

        def done(result: ChangeLookup) -> None:
            self._changes_loading.discard(key)
            self._changes[key] = result
            if result.email:
                self._status(f"Found {result.email.change or 'the change email'} for {fmt_date(entry.prod_date)}", "ok")
            else:
                self._status(f"No change email yet for {fmt_date(entry.prod_date)}", "info")
            if card is self.selected:
                self._refresh_details(card)

        def failed(error: str) -> None:
            self._changes_loading.discard(key)
            self._changes[key] = error
            self._status(f"Couldn't read Outlook: {error}", "error")
            if card is self.selected:
                self._refresh_details(card)

        self._run(f"Searching Outlook for the {fmt_date(entry.prod_date)} change email\u2026",
                  lambda: lookup_change(entry.prod_date, root), done, failed)
        if force and card is self.selected:
            self._refresh_details(card)

    def _change_html(self, entry: MigrationEntry) -> str:
        result = self._changes.get(entry.key)
        out = f"<h4>Email \u00b7 Action required - CHG\u2026 for {html.escape(fmt_date(entry.prod_date))}</h4>"
        if entry.key in self._changes_loading:
            return out + "<p>" + html_chip("Searching Outlook\u2026", "amber") + "</p>"
        if result is None:
            return out
        if isinstance(result, str):
            return out + "<p>" + html_chip("Couldn't read Outlook", "red") + f" &nbsp;{html.escape(result)}</p>"
        email, folder = result.email, result.folder
        if email is None:
            out += ("<p>" + html_chip("Not received yet", "amber") + " &nbsp;No \u201cAction required - CHG\u2026 "
                    f"for scheduled Migration on {html.escape(fmt_date(entry.prod_date))}\u201d email in the Inbox "
                    "or its subfolders. Use <b>Check email again</b> once it arrives.</p>")
        else:
            open_link = " &nbsp;<a href='action:open-email'>Open in Outlook</a>"
            out += _rows([
                ("Change request", f"<b>{html.escape(email.change or '(not in subject)')}</b>"),
                ("Subject", html.escape(email.subject) + open_link),
                ("Received", html.escape(email.received.strftime("%a %b %d, %Y %I:%M %p"))),
                ("Team input due", f"End of day <b>{html.escape(fmt_date(email.deadline))}</b> &nbsp;"
                 + html_chip(relative_days(email.deadline), "slate") if email.deadline
                 else "<i>(not found - check the email)</i>"),
            ])

        out += "<h4>Migration folder</h4>"
        if not folder.path:
            out += ("<p>" + html_chip("Not found", "red") + " &nbsp;No "
                    f"\u201c{entry.prod_date.isoformat()} Release \u2026\u201d folder in "
                    f"{html.escape(self.settings.migration_root)}.</p>")
            if folder.note:
                out += f"<p>{html.escape(folder.note)}</p>"
            return out
        if email is None:
            source = html_chip("no email yet", "slate")
        elif folder.in_email:
            source = html_chip("named in the email", "green")
        else:
            source = html_chip("the email names a different folder - check it", "amber")
        pairs = [
            ("Folder", f"{html.escape(folder.path)} &nbsp;<a href='action:open-folder'>Open</a>"),
            ("Email check", source),
        ]
        if folder.exists:
            proof = (html_chip(f"{folder.proof_items} item{'s' if folder.proof_items != 1 else ''}",
                               "green" if folder.proof_items else "slate")
                     + " &nbsp;<a href='action:open-proof'>Open</a>") if folder.proof_exists else html_chip("missing", "red")
            pairs.append(("Proof of Tests", proof))
            pairs.append(("Contents", "<br>".join(html.escape(n) for n in folder.contents) or "<i>(empty)</i>"))
        if folder.note:
            pairs.append(("Note", html.escape(folder.note)))
        return out + _rows(pairs)

    def _change_missing(self, entry: MigrationEntry) -> list[str]:
        """What still blocks the 3 Days before tasks; empty when everything checks out."""
        if entry.key in self._changes_loading:
            return ["still searching Outlook"]
        result = self._changes.get(entry.key)
        if result is None:
            return ["Outlook not searched yet"]
        if isinstance(result, str):
            return [f"couldn't read Outlook ({result})"]
        missing = []
        if result.email is None:
            missing.append("the Action required - CHG email hasn't arrived")
        folder = result.folder
        if not folder.path:
            missing.append(f"no {entry.prod_date.isoformat()} Release folder in {self.settings.migration_root}")
        elif not folder.exists:
            missing.append(f"can't open {folder.path}")
        elif not folder.proof_exists:
            missing.append("the migration folder has no Proof of Tests folder")
        if result.email is not None and folder.path and not folder.in_email:
            missing.append("the email names a different folder")
        return missing

    @staticmethod
    def _start_html(missing: list[str], task: WorkItem | None, done: bool) -> str:
        out = "<h4>Tasks</h4>"
        if done or (task and task.state.lower() in DONE_STATES):
            return out + "<p>" + html_chip("Done", "green") + "</p>"
        if missing:
            items = "".join(f"<li>{html.escape(m)}</li>" for m in missing)
            return out + "<p>" + html_chip("Not ready", "amber") + " &nbsp;Can't start yet:</p>" + f"<ul>{items}</ul>"
        if task is None:
            return out + "<p>" + html_chip("No matching ADO task", "amber") + "</p>"
        state = (" &nbsp;The ADO task is Active." if task.state.lower() == "active"
                 else f" &nbsp;<b>Start tasks</b> sets ADO task #{task.id} to Active, then gets latest and compares "
                      "ACCP with Main.")
        return out + "<p>" + html_chip("All checks passed", "green") + state + "</p>"

    def _start_three_days(self) -> None:
        card = self.selected
        current = self.tree.currentItem()
        if card is None or card.entry is None or current is None:
            return
        planned: PlannedStep = (current.parent() or current).data(0, ROLE_STEP)
        task = planned.task
        missing = self._change_missing(card.entry)
        if planned.step.action != "change_email" or task is None or missing:
            if missing:
                QMessageBox.information(self, planned.step.name, "Can't start yet:\n\n\u2022 " + "\n\u2022 ".join(missing))
            return
        if task.state.lower() == "active":
            self._compare_changesets(card)
            return
        if not self._user_email:
            self._status("Your Azure DevOps user isn't loaded yet - try again in a moment.", "error")
            return
        email = self._user_email
        self._run(f"Setting task #{task.id} to Active\u2026", lambda: self._client().activate_task(task.id, email),
                  lambda active: self._on_task_activated(card, active),
                  lambda error: self._status(f"Couldn't activate task #{task.id}: {error}", "error"))

    def _on_task_activated(self, card: MigrationCard, task: WorkItem) -> None:
        if card.ticket is not None:
            self._children.pop(card.ticket.id, None)
        self._load_tasks(card)
        self._refresh_today()
        self._status(f"Task #{task.id} is {task.state} and assigned to {task.assigned_to or 'you'}.", "ok")
        self._compare_changesets(card)

    def _three_days_outstanding(self, entry: MigrationEntry) -> list[str]:
        """3 Days before items not done in the app this session; emails are checked in Outlook."""
        out = list(self._change_missing(entry))
        compared = self._tfs_results.get(entry.key)
        if not isinstance(compared, list):
            out.append("ACCP \u2192 Main changesets not compared")
        elif any(r.included for r in compared) and not isinstance(self._list_writes.get(entry.key), ListWrite):
            out.append("ACCP \u2192 Main changesets not written to the migration list")
        for component in TPA_COMPONENTS:
            key = (entry.key, component.key)
            pick = self._tpa_selected.get(key)
            if not isinstance(pick, ReleaseCommits):
                out.append(f"{component.title}: no release selected")
            elif pick.commits and not isinstance(self._tpa_writes.get(key), ListWrite):
                out.append(f"{component.title}: commits not written to {component.sheet}")
        result = self._changes.get(entry.key)
        sent = result.sent if isinstance(result, ChangeLookup) else {}
        out += [f"{kind.title} email not sent" for kind in KINDS if sent.get(kind.id) is None]
        return out

    def _resolve_three_days(self) -> None:
        card = self.selected
        current = self.tree.currentItem()
        if card is None or card.entry is None or current is None:
            return
        planned: PlannedStep = (current.parent() or current).data(0, ROLE_STEP)
        task = planned.task
        if planned.step.action != "change_email" or task is None or task.id in self._resolving:
            return
        if not self._user_email:
            self._status("Your Azure DevOps user isn't loaded yet - try again in a moment.", "error")
            return
        outstanding = self._three_days_outstanding(card.entry)
        text = f"Resolve ADO task #{task.id} ({task.title}) and assign it to you?"
        if outstanding:
            text += ("\n\nNot done in the app this session (they may have been done earlier or by hand):\n\u2022 "
                     + "\n\u2022 ".join(outstanding))
        default = QMessageBox.StandardButton.No if outstanding else QMessageBox.StandardButton.Yes
        answer = QMessageBox.question(self, planned.step.name, text,
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, default)
        if answer != QMessageBox.StandardButton.Yes:
            return
        email = self._user_email
        self._resolving.add(task.id)

        def done(resolved: WorkItem) -> None:
            self._resolving.discard(task.id)
            self._on_task_resolved(card, resolved)

        def failed(error: str) -> None:
            self._resolving.discard(task.id)
            self._status(f"Couldn't resolve task #{task.id}: {error}", "error")
            if card is self.selected:
                self._refresh_details(card)

        self._run(f"Resolving task #{task.id}\u2026", lambda: self._client().resolve_task(task.id, email), done, failed)
        self._refresh_details(card)

    # ---------- 3 days before: ACCP -> Main changesets ----------
    def _tfs_path(self, solution: TfsSolution) -> str:
        return self.state.get_setting(f"tfs_path.{solution.name}", solution.default_path)

    def _change_tfs_path(self, name: str) -> None:
        solution = next((s for s in SOLUTIONS if s.name == name), None)
        if solution is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, f"Local folder mapped to $/{solution.name}", self._tfs_path(solution))
        if not folder:
            return
        self.state.set_setting(f"tfs_path.{solution.name}", str(Path(folder)))
        if self.selected is not None:
            self._refresh_details(self.selected)

    def _list_url(self, entry: MigrationEntry) -> str:
        result = self._changes.get(entry.key)
        return result.folder.list_url if isinstance(result, ChangeLookup) else ""

    def _compare_changesets(self, card: MigrationCard) -> None:
        key = card.entry.key
        if key in self._tfs_running:
            return
        self._tfs_running.add(key)
        self._list_writes.pop(key, None)
        paths = {s.name: self._tfs_path(s) for s in SOLUTIONS}
        tf_exe = self.settings.tf_exe

        def fn() -> list[CompareResult]:
            details = lambda ids: self._client().get_changesets(ids)  # noqa: E731
            return [compare_solution(s, paths[s.name], details, tf_exe) for s in SOLUTIONS]

        def done(results: list[CompareResult]) -> None:
            self._tfs_running.discard(key)
            self._tfs_results[key] = results
            errors = [r for r in results if r.error]
            found = sum(len(r.included) for r in results)
            if errors:
                self._status(f"Compare finished with errors: {errors[0].error}", "error")
            else:
                self._status(f"{found} changeset{'s' if found != 1 else ''} in ACCP not merged into Main.", "ok")
            if card is self.selected:
                self._refresh_details(card)

        def failed(error: str) -> None:
            self._tfs_running.discard(key)
            self._tfs_results[key] = error
            self._status(f"Compare failed: {error}", "error")
            if card is self.selected:
                self._refresh_details(card)

        self._run("Getting latest and comparing ACCP with Main\u2026", fn, done, failed)
        if card is self.selected:
            self._refresh_details(card)

    def _tfs_html(self, entry: MigrationEntry) -> str:
        key = entry.key
        out = "<h4>Changesets to migrate \u00b7 ACCP \u2192 Main</h4>"
        if key in self._tfs_running:
            out += "<p>" + html_chip("Getting latest and comparing\u2026", "amber") + "</p>"
        results = self._tfs_results.get(key)
        if isinstance(results, str):
            out += "<p>" + html_chip("Compare failed", "red") + f" &nbsp;{html.escape(results)}</p>"
        by_name = {r.solution.name: r for r in results} if isinstance(results, list) else {}
        list_url = self._list_url(entry)
        if list_url:
            out += _rows([("Migration list", f"<a href='{html.escape(list_url)}'>"
                                             f"{html.escape(workbook_name(list_url))}</a>")])
        for solution in SOLUTIONS:
            path = self._tfs_path(solution)
            change = f" &nbsp;<a href='action:tfs-path/{solution.name}'>Change</a>"
            pairs = [("Local folder", html.escape(path) + change
                      + ("" if Path(path).is_dir() else " &nbsp;" + html_chip("not found", "red"))),
                     ("Branches", html.escape(f"{solution.source} \u2192 {solution.target}")),
                     ("Sheet", html.escape(solution.sheet))]
            result = by_name.get(solution.name)
            table = ""
            if result is not None:
                for get in result.gets:
                    text = (f"{get.updated} file{'s' if get.updated != 1 else ''} updated" if get.updated
                            else "up to date")
                    chip = html_chip(text, "green")
                    if get.problems:
                        chip += " &nbsp;" + html_chip(f"{len(get.problems)} conflict/warning line(s)", "amber")
                        chip += "<br>" + "<br>".join(html.escape(p) for p in get.problems[:5])
                    pairs.append((f"Get latest {get.branch.rsplit('/', 1)[-1]}", chip))
                if result.error:
                    pairs.append(("Error", html_chip(result.error, "red")))
                else:
                    included = result.included
                    pairs.append(("To migrate", html_chip(f"{len(included)} changeset{'s' if len(included) != 1 else ''}",
                                                         "blue" if included else "slate")))
                    if result.excluded:
                        pairs.append(("Left out", "[Do not merge]: " + ", ".join(
                            str(c.id) for c in result.excluded)))
                    table = self._changeset_table(included)
            out += f"<p><b>{html.escape(solution.name)}</b></p>" + _rows(pairs) + table
        write = self._list_writes.get(key)
        if key in self._list_writing:
            out += "<p>" + html_chip("Writing to the migration list in Excel\u2026", "amber") + "</p>"
        elif isinstance(write, str):
            out += "<p>" + html_chip("Couldn't write the migration list", "red") + f" &nbsp;{html.escape(write)}</p>"
        elif isinstance(write, ListWrite):
            rows = []
            for sheet in write.sheets:
                text = (f"added {len(sheet.added)} (rows {sheet.first_row}\u2013{sheet.last_row})" if sheet.added
                        else "nothing new")
                if sheet.skipped:
                    text += f", {len(sheet.skipped)} already listed"
                rows.append((sheet.sheet, html_chip(text, "green" if sheet.added else "slate")))
            out += (f"<h4>Written to {html.escape(write.workbook)}</h4>" + _rows(rows)
                    + "<p>Check the rows in Excel. The app doesn't save; AutoSave may already have saved them.</p>")
        return out

    @staticmethod
    def _changeset_table(changesets: list) -> str:
        if not changesets:
            return ""
        cells = "".join(
            f"<tr><td>{c.id}{'*' if c.partial else ''}</td><td>{html.escape(c.author)}</td>"
            f"<td>{html.escape(c.created.strftime('%m/%d/%Y %I:%M %p') if c.created else '')}</td>"
            f"<td>{html.escape(c.comment)}</td></tr>"
            for c in changesets)
        head = "<tr><td class='k'>Changeset</td><td class='k'>Checked in by</td><td class='k'>Date</td>" \
               "<td class='k'>Comment</td></tr>"
        return f"<table cellspacing='0' cellpadding='3'>{head}{cells}</table>"

    def _write_list(self) -> None:
        card = self.selected
        if card is None or card.entry is None:
            return
        key = card.entry.key
        results = self._tfs_results.get(key)
        url = self._list_url(card.entry)
        if not isinstance(results, list) or not url or key in self._list_writing:
            return
        sheets = {r.solution.sheet: changeset_rows(r.included) for r in results if not r.error}
        counts = "\n".join(f"\u2022 {len(rows)} to {sheet}" for sheet, rows in sheets.items())
        failed_names = [r.solution.name for r in results if r.error]
        warning = (f"\n\n{', '.join(failed_names)} failed to compare and will be skipped." if failed_names else "")
        answer = QMessageBox.question(
            self, "Write to migration list",
            f"Add the changesets to {workbook_name(url)}?\n\n{counts}{warning}\n\n"
            "Changesets already on a sheet are skipped. Excel opens the workbook and leaves it open for you "
            "to check; AutoSave may save the changes right away.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._list_writing.add(key)

        def done(result: ListWrite) -> None:
            self._list_writing.discard(key)
            self._list_writes[key] = result
            added = sum(len(s.added) for s in result.sheets)
            self._status(f"Added {added} changeset{'s' if added != 1 else ''} to {result.workbook}.", "ok")
            if card is self.selected:
                self._refresh_details(card)

        def failed(error: str) -> None:
            self._list_writing.discard(key)
            self._list_writes[key] = error
            self._status(f"Couldn't write the migration list: {error}", "error")
            if card is self.selected:
                self._refresh_details(card)

        self._run(f"Writing to {workbook_name(url)}\u2026", lambda: write_changesets(url, sheets), done, failed)
        self._refresh_details(card)

    # ---------- 3 days before: TPA releases ----------
    def _ensure_tpa(self, force: bool = False) -> None:
        for component in TPA_COMPONENTS:
            key = component.key
            if key in self._tpa_loading or (key in self._tpa_lists and not force):
                continue
            self._tpa_loading.add(key)

            def done(result: ReleaseList, key=key) -> None:
                self._tpa_loading.discard(key)
                self._tpa_lists[key] = result
                if self.selected is not None:
                    self._refresh_details(self.selected)

            def failed(error: str, key=key) -> None:
                self._tpa_loading.discard(key)
                self._tpa_lists[key] = error
                self._status(f"Couldn't load TPA releases: {error}", "error")
                if self.selected is not None:
                    self._refresh_details(self.selected)

            self._run(f"Loading {component.title} releases\u2026",
                      lambda c=component: TpaClient().list_releases(c), done, failed)
        if force and self.selected is not None:
            self._refresh_details(self.selected)

    @staticmethod
    def _stage_html(run: ReleaseRun) -> str:
        marks = {"succeeded": ("\u2713", SUCCESS), "succeededWithIssues": ("\u2713", WARNING),
                 "failed": ("\u2717", DANGER), "canceled": ("\u2013", MUTED), "skipped": ("\u2013", MUTED)}
        parts = []
        for stage in run.stages:
            mark, color = marks.get(stage.result, ("\u25cb", MUTED) if stage.state == "pending"
                                    else ("\u25d0", INFO))
            weight = "700" if stage.name.upper() == "ACCP" else "400"
            parts.append(f"<span style='color:{color}; font-weight:{weight}'>{html.escape(stage.name)} {mark}</span>")
        return " &nbsp;".join(parts)

    def _tpa_html(self, card: MigrationCard, missing: list[str]) -> str:
        entry = card.entry
        out = ""
        for component in TPA_COMPONENTS:
            listed = self._tpa_lists.get(component.key)
            name = listed.pipeline if isinstance(listed, ReleaseList) else "release pipeline"
            reload = "" if component.key in self._tpa_loading else \
                f" &nbsp;<a href='action:tpa-reload/{component.key}'>Reload</a>"
            out += f"<h4>{html.escape(component.title)} \u00b7 {html.escape(name)}</h4>"
            pick = (self._tpa_selected.get((entry.key, component.key)))
            busy = (entry.key, component.key) in self._tpa_busy
            if component.key in self._tpa_loading:
                out += "<p>" + html_chip("Loading pipeline runs\u2026", "amber") + "</p>"
            elif isinstance(listed, str):
                out += "<p>" + html_chip("Couldn't load", "red") + f" &nbsp;{html.escape(listed)}{reload}</p>"
            elif isinstance(listed, ReleaseList):
                chosen = pick.run.id if isinstance(pick, ReleaseCommits) else None
                rows = ""
                for run in listed.runs:
                    if run.id == chosen:
                        action = html_chip("Selected", "green")
                    elif run.accp_ok and not missing and not busy:
                        action = f"<a href='action:tpa-select/{component.key}/{run.id}'>Select</a>"
                    else:
                        action = ""
                    style = "" if run.accp_ok else f" style='color:{MUTED}'"
                    when = run.queued.strftime("%b %d %I:%M %p") if run.queued else ""
                    rows += (f"<tr{style}><td><a href='{html.escape(run.web_url)}'>#{html.escape(run.number)}</a></td>"
                             f"<td>{html.escape(run.commit[:8])}</td><td>{html.escape(when)}</td>"
                             f"<td>{self._stage_html(run)}</td><td>{action}</td></tr>")
                head = ("<tr><td class='k'>Run</td><td class='k'>Commit</td><td class='k'>Queued</td>"
                        "<td class='k'>Stages</td><td></td></tr>")
                accp = sum(1 for r in listed.runs if r.accp_ok)
                hint = ("Only runs with <b>ACCP \u2713</b> can be released. The package builder picks one; "
                        "it doesn't have to be the latest." if accp
                        else html_chip("No recent run has passed ACCP", "amber"))
                if missing:
                    hint += " &nbsp;<i>Selecting is available once all checks pass.</i>"
                out += (f"<p>{hint}{reload}</p><table cellspacing='0' cellpadding='3'>{head}{rows}</table>")
            out += self._tpa_selection_html(entry, component, pick, busy)
        return out

    def _tpa_selection_html(self, entry: MigrationEntry, component, pick, busy: bool) -> str:
        if busy and pick is None:
            return "<p>" + html_chip("Finding the commits since the previous PROD release\u2026", "amber") + "</p>"
        if isinstance(pick, str):
            return "<p>" + html_chip("Couldn't get the commits", "red") + f" &nbsp;{html.escape(pick)}</p>"
        if not isinstance(pick, ReleaseCommits):
            return ""
        count = len(pick.commits)
        pairs = [("Release", f"<b>#{html.escape(pick.run.number)}</b> \u00b7 commit {html.escape(pick.run.commit[:8])}"),
                 ("Previous PROD release", f"{html.escape(pick.baseline)} \u00b7 commit "
                                           f"{html.escape(pick.baseline_commit[:8])}"),
                 ("Commits to migrate", html_chip(f"{count} commit{'s' if count != 1 else ''}",
                                                  "blue" if count else "slate"))]
        for note in pick.notes:
            pairs.append(("Note", html_chip(note, "amber")))
        url = self._list_url(entry)
        key = (entry.key, component.key)
        if count:
            links = [f"<a href='action:tpa-copy/{component.key}'>Copy commits</a>"]
            if busy:
                links.append(html_chip("Writing to the migration list\u2026", "amber"))
            elif url:
                links.append(f"<a href='action:tpa-write/{component.key}'>Write to {html.escape(component.sheet)}</a>")
            else:
                links.append("<i>no migration list link (_migration.url) in the migration folder</i>")
            pairs.append(("Actions", " &nbsp;\u00b7&nbsp; ".join(links)))
        write = self._tpa_writes.get(key)
        if isinstance(write, str):
            pairs.append(("Migration list", html_chip("Couldn't write", "red") + f" &nbsp;{html.escape(write)}"))
        elif isinstance(write, ListWrite):
            sheet = write.sheets[0]
            text = (f"added {len(sheet.added)} to {sheet.sheet} (rows {sheet.first_row}\u2013{sheet.last_row})"
                    if sheet.added else f"nothing new on {sheet.sheet}")
            if sheet.skipped:
                text += f", {len(sheet.skipped)} already listed"
            pairs.append(("Migration list", html_chip(text, "green" if sheet.added else "slate")
                          + " &nbsp;Check it in Excel; AutoSave may already have saved it."))
        cells = "".join(
            f"<tr><td>{html.escape(c.id)}</td><td>{html.escape(c.author)}</td>"
            f"<td>{html.escape(c.date.strftime('%m/%d/%Y %I:%M %p') if c.date else '')}</td>"
            f"<td>{html.escape(c.message)}</td></tr>" for c in pick.commits)
        table = ("<table cellspacing='0' cellpadding='3'><tr><td class='k'>ID</td><td class='k'>Author</td>"
                 f"<td class='k'>Date</td><td class='k'>Message</td></tr>{cells}</table>") if cells else ""
        return _rows(pairs) + table

    def _tpa_component(self, key: str):
        return next((c for c in TPA_COMPONENTS if c.key == key), None)

    def _tpa_select(self, component_key: str, run_id: int) -> None:
        card = self.selected
        component = self._tpa_component(component_key)
        listed = self._tpa_lists.get(component_key)
        if card is None or card.entry is None or component is None or not isinstance(listed, ReleaseList):
            return
        run = next((r for r in listed.runs if r.id == run_id), None)
        key = (card.entry.key, component_key)
        if run is None or not run.accp_ok or key in self._tpa_busy or self._change_missing(card.entry):
            return
        if not self._confirm_builder(card.entry):
            return
        answer = QMessageBox.question(
            self, f"Release {component.title}",
            f"Release run #{run.number} (commit {run.commit[:8]}) of {listed.pipeline} to PROD?\n\n"
            "Its ACCP stage succeeded. The commits since the previous PROD release will be listed for the "
            "migration list.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._tpa_busy.add(key)
        self._tpa_selected.pop(key, None)
        self._tpa_writes.pop(key, None)

        def done(result: ReleaseCommits) -> None:
            self._tpa_busy.discard(key)
            self._tpa_selected[key] = result
            self._status(f"{component.title} #{run.number}: {len(result.commits)} commits since "
                         f"{result.baseline}.", "ok")
            if card is self.selected:
                self._refresh_details(card)

        def failed(error: str) -> None:
            self._tpa_busy.discard(key)
            self._tpa_selected[key] = error
            self._status(f"Couldn't get the {component.title} commits: {error}", "error")
            if card is self.selected:
                self._refresh_details(card)

        self._run(f"Finding {component.title} commits for #{run.number}\u2026",
                  lambda: TpaClient().release_commits(component, run), done, failed)
        self._refresh_details(card)

    @staticmethod
    def _excel_date(value) -> str:
        if value is None:
            return ""
        return f"{value.month}/{value.day}/{value.year} {value.strftime('%I').lstrip('0')}:{value:%M:%S %p}"

    def _tpa_copy(self, component_key: str) -> None:
        card = self.selected
        pick = self._tpa_selected.get((card.entry.key, component_key)) if card and card.entry else None
        if not isinstance(pick, ReleaseCommits) or not pick.commits:
            return
        text = "\r\n".join("\t".join((c.id, c.author, self._excel_date(c.date), c.message.replace("\t", " ")))
                           for c in pick.commits)
        QApplication.clipboard().setText(text)
        self._status(f"Copied {len(pick.commits)} commits (ID, Author, Date, Message) - paste into column A.", "ok")

    def _tpa_write(self, component_key: str) -> None:
        card = self.selected
        component = self._tpa_component(component_key)
        if card is None or card.entry is None or component is None:
            return
        key = (card.entry.key, component_key)
        pick = self._tpa_selected.get(key)
        url = self._list_url(card.entry)
        if not isinstance(pick, ReleaseCommits) or not pick.commits or not url or key in self._tpa_busy:
            return
        answer = QMessageBox.question(
            self, "Write to migration list",
            f"Add {len(pick.commits)} commits of #{pick.run.number} to {component.sheet} in "
            f"{workbook_name(url)}?\n\nCommits already on the sheet are skipped. Excel opens the workbook and "
            "leaves it open for you to check; AutoSave may save the changes right away.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        rows = [ListRow(c.id, c.author, c.date, c.message) for c in pick.commits]
        self._tpa_busy.add(key)

        def done(result: ListWrite) -> None:
            self._tpa_busy.discard(key)
            self._tpa_writes[key] = result
            added = sum(len(s.added) for s in result.sheets)
            self._status(f"Added {added} commits to {component.sheet}.", "ok")
            if card is self.selected:
                self._refresh_details(card)

        def failed(error: str) -> None:
            self._tpa_busy.discard(key)
            self._tpa_writes[key] = error
            self._status(f"Couldn't write the migration list: {error}", "error")
            if card is self.selected:
                self._refresh_details(card)

        self._run(f"Writing to {component.sheet}\u2026", lambda: write_changesets(url, {component.sheet: rows}),
                  done, failed)
        self._refresh_details(card)

    # ---------- 3 days before: migration list and code freeze emails ----------
    @staticmethod
    def _sprint(card: MigrationCard) -> str:
        """Sprint used in the migration list subject: the ADO ticket's sprint, one ahead of the schedule's."""
        found = re.search(r"Sprint (\d+)", card.ticket.iteration_path) if card.ticket else None
        return found[1] if found else ""

    def _email_blocker(self, card: MigrationCard, kind: str, missing: list[str]) -> str:
        entry = card.entry
        if missing:
            return "Not ready - see Tasks above."
        if kind == MIGRATION_LIST and not self._list_url(entry):
            return "No migration list link (_migration.url) in the migration folder."
        if kind == CODE_FREEZE and entry.code_freeze is None:
            return "No code freeze date in the schedule."
        return ""

    def _emails_html(self, card: MigrationCard, missing: list[str]) -> str:
        entry = card.entry
        result = self._changes.get(entry.key)
        sent = result.sent if isinstance(result, ChangeLookup) else {}
        sprint = self._sprint(card) or "?"
        subjects = {MIGRATION_LIST: list_subject(entry.prod_date, sprint),
                    CODE_FREEZE: "UniFi ACCP Refresh and Code Freeze"}
        notes = {MIGRATION_LIST: "Asks the team to review the migration list; links to "
                                 + (html.escape(workbook_name(self._list_url(entry))) or "the list") + ".",
                 CODE_FREEZE: "Code freeze at 5:00 AM Pacific " + html.escape(
                     f"{entry.code_freeze:%A %b %d, %Y}" if entry.code_freeze else "(no date)")
                     + f", in effect until PROD on {html.escape(f'{entry.prod_date:%A %b %d, %Y}')}."}
        rows = []
        for kind in KINDS:
            found = sent.get(kind.id)
            if found is not None:
                status = (html_chip(f"Already sent {found.when:%a %b %d, %I:%M %p}", "green")
                          + f" &nbsp;in {html.escape(found.folder)} &nbsp;<a href='action:open-sent/{kind.id}'>Open</a>")
            elif f"{entry.key}|{kind.id}" in self._drafting:
                status = html_chip("Opening in Outlook\u2026", "amber")
            else:
                blocker = self._email_blocker(card, kind.id, missing)
                status = (html_chip("Not sent", "slate") + " &nbsp;" + (
                    f"<i>{html.escape(blocker)}</i>" if blocker
                    else f"<a href='action:draft-email/{kind.id}'>Draft email</a>"))
            subject = found.subject if found is not None else subjects[kind.id]
            rows.append((kind.title, f"{status}<br><b>{html.escape(subject)}</b><br>{notes[kind.id]}"))
        return ("<h4>Emails</h4>" + _rows(rows)
                + "<p>Drafts open in Outlook from the team's templates for you to review and send. Emails already "
                  "in your Inbox or Sent Items for this migration aren't drafted again; use <b>Check email again</b> "
                  "after sending.</p>")

    def _draft_email(self, kind: str) -> None:
        card = self.selected
        if card is None or card.entry is None:
            return
        entry = card.entry
        result = self._changes.get(entry.key)
        key = f"{entry.key}|{kind}"
        if not isinstance(result, ChangeLookup) or key in self._drafting:
            return
        if result.sent.get(kind) is not None:
            self._status("That email was already sent for this migration.", "info")
            return
        blocker = self._email_blocker(card, kind, self._change_missing(entry))
        if blocker:
            self._status(blocker, "error")
            return
        templates = self.settings.email_templates
        if kind == MIGRATION_LIST:
            sprint = self._sprint(card)
            if not sprint:
                sprint, ok = QInputDialog.getText(self, "Migration list email",
                                                  "Sprint number for the subject (the ADO ticket has none):")
                sprint = sprint.strip()
                if not ok or not sprint:
                    return
            url = self._list_url(entry)
            build = lambda: migration_list_draft(templates, entry.prod_date, sprint, url)  # noqa: E731
        else:
            holidays, today = self.settings.holidays, date.today()
            build = lambda: code_freeze_draft(templates, entry.prod_date, entry.code_freeze, today, holidays)  # noqa: E731
        self._drafting.add(key)

        def done(_result) -> None:
            self._drafting.discard(key)
            self._status("Review the email in Outlook and click Send, then click Check email again.", "ok")
            if card is self.selected:
                self._refresh_details(card)

        def failed(error: str) -> None:
            self._drafting.discard(key)
            self._status(f"Couldn't open the email: {error}", "error")
            QMessageBox.warning(self, "Email", error)
            if card is self.selected:
                self._refresh_details(card)

        self._run("Opening the email in Outlook\u2026", lambda: show_draft(build()), done, failed)
        self._refresh_details(card)

    def _open_sent(self, kind: str) -> None:
        card = self.selected
        result = self._changes.get(card.entry.key) if card is not None and card.entry is not None else None
        found = result.sent.get(kind) if isinstance(result, ChangeLookup) else None
        if found is not None:
            self._run("Opening the email in Outlook\u2026", lambda: open_email(found.entry_id, found.store_id),
                      lambda _r: None)

    def _open_change_item(self, action: str) -> None:
        card = self.selected
        result = self._changes.get(card.entry.key) if card is not None and card.entry is not None else None
        if not isinstance(result, ChangeLookup):
            return
        if action == "open-email" and result.email is not None:
            email = result.email
            self._run("Opening the email in Outlook\u2026", lambda: open_email(email.entry_id, email.store_id),
                      lambda _r: None)
            return
        path = result.folder.path if action == "open-folder" else result.folder.proof_path
        try:
            open_path(path)
        except OSError as exc:
            self._status(f"Couldn't open {path}: {exc}", "error")

    def _confirm_builder(self, entry: MigrationEntry) -> bool:
        first = self.user_bar.first_name.lower()
        builders = [name for name in (entry.builder, entry.builder_backup) if name.strip()]
        if first and any(name.strip().lower().split(" ")[0] == first for name in builders):
            return True
        user = f"You ({self.user_bar.first_name})" if first else "Your Azure DevOps user could not be identified, so you"
        listed = " / ".join(builders) or "nobody"
        answer = QMessageBox.warning(
            self, "Not the package builder",
            f"{user} are not the package builder or backup for {migration_label(entry)}.\n"
            f"Package builder / backup: {listed}.\n\nContinue anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _ensure_browser(self) -> BrowserPane:
        if self.browser is None:
            self.browser = BrowserPane()
            self.browser.closed.connect(lambda: self.stack.setCurrentWidget(self.details))
            self.stack.addWidget(self.browser)
        return self.browser

    def _ensure_teams(self) -> BrowserPane:
        if self.teams_browser is None:
            self.teams_browser = BrowserPane()
            self.teams_browser.closed.connect(lambda: self.stack.setCurrentWidget(self.details))
            self.stack.addWidget(self.teams_browser)
            watch_loading(self.teams_browser.page())
            self.teams_browser.load(self.settings.teams_channel_url)
        return self.teams_browser

    def _on_teams_progress(self, text: str) -> None:
        self._status(text, "busy")
        if self.teams_browser is not None:
            self.teams_browser.set_runner_status(text)

    def _start_booking(self, mode: str) -> None:
        card = self.selected
        if (card is None or card.entry is None or self._runner is not None or self._booking is not None
                or self._downloading):
            return
        current = self.tree.currentItem()
        planned: PlannedStep | None = (current.parent() or current).data(0, ROLE_STEP) if current else None
        if planned is None or not planned.step.action:
            return
        if not self._confirm_builder(card.entry):
            return
        entry, ticket = card.entry, card.ticket
        task = planned.task
        if mode == "fill":
            if ticket is None:
                QMessageBox.warning(self, "No Azure DevOps ticket",
                                    "Link or create this migration's Azure DevOps ticket first, so the ServiceNow "
                                    "requests can be logged in its discussion.")
                return
            resolve = (f"When everything is logged, task #{task.id} is resolved and assigned to you." if task
                       else "There is no matching ADO task, so nothing will be resolved.")
            teams = (f"Then a post in Teams ({self.settings.teams_team} > {self.settings.teams_channel}) is "
                     "filled for you to review and Post yourself; it is logged when you confirm.\n"
                     if planned.step.action in TEAMS_POST_ACTIONS else "")
            answer = QMessageBox.question(
                self, "Submit real ServiceNow requests",
                f"This opens the real ServiceNow forms for {migration_label(entry)} and fills them.\n\n"
                "Review each form and click Request yourself - the app never submits for you.\n"
                f"Each REQ number is logged in the discussion of ADO #{ticket.id}.\n{teams}{resolve}\n\n"
                "Anything already logged in that discussion is skipped.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        names = self._missing_names(entry)
        ticket_id = ticket.id if mode == "fill" and ticket else None
        if planned.step.action in TEAMS_POST_ACTIONS:
            # Start Teams now so it loads while the ServiceNow part runs.
            self._ensure_teams()
        self._booking = {"mode": mode, "card": card, "entry": entry, "ticket_id": ticket_id, "task": task,
                         "action": planned.step.action, "step": planned.step.name,
                         "titles": [], "logged": set(), "skipped": [], "log": {}, "requests": {},
                         "teams_logged": False, "teams": None}

        def prepare():
            full = self._lookup_full_names(names)
            # Read the discussion fresh so a form submitted in an earlier run is never submitted twice.
            comments = self._client().get_comments(ticket_id) if ticket_id else []
            return full, comments

        def failed(error: str) -> None:
            self._booking = None
            self._status(f"Booking not started: {error}", "error")
            QMessageBox.critical(self, "PnC Migration Tool", f"Booking not started:\n{error}")

        self._run("Preparing ServiceNow booking\u2026", prepare, lambda result: self._launch_booking(*result), failed)

    def _launch_booking(self, full: dict[str, str], comments: list[Comment]) -> None:
        booking = self._booking
        card, entry, mode = booking["card"], booking["entry"], booking["mode"]
        self._full_names.update(full)
        if card.entry is not entry:
            self._booking = None
            return
        forms = self._step_forms(booking["action"], entry)
        if not forms:
            self._booking = None
            self._status("Nothing to fill - the ServiceNow request couldn't be built.", "error")
            return
        booking["titles"] = [form.title for form in forms]
        if booking["ticket_id"]:
            self._comments[booking["ticket_id"]] = comments
            already = _logged_forms(comments, booking["titles"])
            booking["logged"] |= already
            booking["skipped"] = [title for title in booking["titles"] if title in already]
            booking["requests"] = {title: _logged_request(comments, title) for title in already}
            booking["teams_logged"] = any(f"{booking['step']} {TEAMS_MARKER}" in text
                                          for text in _comment_texts(comments))
        remaining = [form for form in forms if form.title not in booking["logged"]]
        if not remaining:
            self._status("Every request is already logged in the ticket discussion.", "ok")
            self._after_submissions()
            return
        self._fetch_attachments(remaining)

    def _fetch_attachments(self, forms: list[FormSpec]) -> None:
        """Downloads each attachment fresh from SharePoint, one at a time, then starts the runner."""
        pending = next((a for form in forms for a in form.attachments if not a.content), None)
        if pending is None:
            self._run_forms(forms)
            return

        def done(path: str) -> None:
            content = Path(path).read_bytes()
            if not content:
                failed(f"{pending.name} downloaded empty from SharePoint.")
                return
            loaded = replace(pending, content=content)
            self._fetch_attachments([
                replace(form, attachments=tuple(loaded if a == pending else a for a in form.attachments))
                for form in forms
            ])

        def failed(error: str) -> None:
            self._booking = None
            self._status(f"Couldn't download {pending.name}: {error}", "error")
            QMessageBox.critical(self, "PnC Migration Tool",
                                 f"Couldn't download {pending.name} from SharePoint:\n{error}\n\nNothing was filled.")

        self._download(pending.url, ATTACHMENTS_DIR / pending.name,
                       f"Downloading {pending.name} from SharePoint\u2026", done, failed)

    def _run_forms(self, forms: list[FormSpec]) -> None:
        mode = self._booking["mode"]
        browser = self._ensure_browser()
        runner = ServiceNowRunner(browser.page(), forms, mode, self)
        runner.progress.connect(self._on_runner_progress)
        runner.waiting.connect(browser.show_next)
        runner.submitted.connect(self._on_request_submitted)
        runner.finished.connect(lambda reports: self._on_booking_finished(mode, reports))
        browser.next_clicked.connect(runner.advance)
        self._runner = runner
        self.stack.setCurrentWidget(browser)
        self._begin("Running ServiceNow booking\u2026")
        runner.start()

    def _on_request_submitted(self, title: str, number: str, link: str) -> None:
        booking, ticket_id = self._booking, self._booking["ticket_id"]
        shown = number or "number not read"
        text = (f"<b>{html.escape(title)}</b> {LOG_MARKER}: <a href=\"{html.escape(link)}\">{html.escape(shown)}</a>"
                f"<br>Submitted by {html.escape(self.user_bar.first_name or 'unknown user')} via PnC Migration Tool")

        def logged(comment: Comment) -> None:
            booking["logged"].add(title)
            booking["requests"][title] = number
            booking["log"][title] = (True, f"Logged in ADO #{ticket_id}")
            self._comments.setdefault(ticket_id, []).insert(0, comment)
            self._continue_runner()

        def failed(error: str) -> None:
            booking["log"][title] = (False, f"Not logged: {error}")
            QMessageBox.critical(
                self, "Couldn't log the ServiceNow request",
                f"{title} request {shown} was submitted but could not be logged in ADO #{ticket_id}:\n{error}\n\n"
                f"Add it to the ticket discussion manually:\n{link}")
            self._continue_runner()

        self._run(f"Logging {shown} in ADO #{ticket_id}\u2026",
                  lambda: self._client().add_comment(ticket_id, text), logged, failed)

    def _continue_runner(self) -> None:
        if self._runner is not None:
            self._runner.advance()

    def _after_submissions(self) -> None:
        booking = self._booking
        if booking is None:
            return
        if booking["mode"] != "fill":
            if booking["action"] in TEAMS_POST_ACTIONS:
                self._start_teams_post()
            else:
                self._booking = None
            return
        card, task = booking["card"], booking["task"]
        missing = [title for title in booking["titles"] if title not in booking["logged"]]
        if missing:
            self._booking = None
            self._status(f"Task left open - not submitted/logged: {', '.join(missing)}. Run again to finish.", "error")
            return
        if booking["action"] in TEAMS_POST_ACTIONS and not booking["teams_logged"]:
            self._start_teams_post()
            return
        self._booking = None
        if task is None:
            self._status("All requests logged; there is no ADO task to resolve.", "ok")
            return
        if not self._user_email:
            self._status(f"All requests logged, but task #{task.id} wasn't resolved: your ADO user is unknown.", "error")
            return
        email = self._user_email
        self._run(f"Resolving task #{task.id}\u2026", lambda: self._client().resolve_task(task.id, email),
                  lambda resolved: self._on_task_resolved(card, resolved),
                  lambda error: self._status(f"Couldn't resolve task #{task.id}: {error}", "error"))

    def _start_teams_post(self) -> None:
        booking = self._booking
        test = booking["mode"] != "fill"
        ticket = booking["card"].ticket
        comments = self._comments.get(ticket.id, []) if ticket else []
        request = next((n for n in (booking["requests"].get(t, "") or _logged_request(comments, t)
                                    for t in booking["titles"]) if n), "")
        if not test and not request:
            value, ok = QInputDialog.getText(
                self, "ServiceNow REQ number",
                "The REQ number couldn't be read from ServiceNow.\nEnter it for the Teams post (e.g. REQ0633173):")
            value = value.strip().upper()
            if not ok or not re.fullmatch(r"REQ\d+", value):
                self._booking = None
                self._status("Teams post not started - no valid REQ number. Task left open; run again to finish.",
                             "error")
                return
            request = value
        subject, message = refresh_post(booking["entry"], request)
        browser = self._ensure_teams()
        poster = TeamsPoster(browser.page(), self.settings.teams_channel_url, self.settings.teams_team,
                             self.settings.teams_channel,
                             subject, message, test, self)
        poster.progress.connect(self._on_teams_progress)
        booking["teams"] = (poster, subject)
        browser.next_clicked.connect(self._on_teams_confirm)
        browser.show_next(True, "Finish test  \u203a" if test else "I posted it  \u203a")
        self.stack.setCurrentWidget(browser)
        self._begin("Filling the Teams post\u2026")
        poster.start()

    def _start_teams_test(self) -> None:
        """Teams part only, as a test: no ServiceNow, nothing posted or logged."""
        card = self.selected
        current = self.tree.currentItem()
        planned: PlannedStep | None = (current.parent() or current).data(0, ROLE_STEP) if current else None
        if (card is None or card.entry is None or planned is None or planned.step.action not in TEAMS_POST_ACTIONS
                or self._runner is not None or self._booking is not None):
            return
        forms = self._step_forms(planned.step.action, card.entry)
        self._booking = {"mode": "test", "card": card, "entry": card.entry, "ticket_id": None, "task": planned.task,
                         "action": planned.step.action, "step": planned.step.name,
                         "titles": [form.title for form in forms], "logged": set(), "skipped": [], "log": {},
                         "requests": {}, "teams_logged": False, "teams": None}
        self._start_teams_post()

    def _on_teams_confirm(self) -> None:
        booking = self._booking
        poster, subject = booking["teams"]
        ticket_id = booking["ticket_id"]
        test = booking["mode"] != "fill"
        answer = QMessageBox.StandardButton.No if test else QMessageBox.question(
            self, "Teams post",
            f"Did you click Post in {self.settings.teams_team} > {self.settings.teams_channel}?\n\n"
            f"Yes: log it in ADO #{ticket_id} and resolve the task.\nNo: stop and leave the task open.\n"
            "Cancel: go back to Teams.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Cancel:
            return
        browser = self.teams_browser
        browser.next_clicked.disconnect(self._on_teams_confirm)
        browser.show_next(False)
        browser.set_runner_status("")
        poster.stop()
        poster.deleteLater()
        booking["teams"] = None
        self._end()
        if answer != QMessageBox.StandardButton.Yes:
            self._booking = None
            if test:
                self._status("Test run finished - nothing was posted or logged. Discard the Teams draft if it's "
                             "still open.", "ok")
            else:
                self._status("Teams post not confirmed - task left open. Run again to finish.", "error")
            return
        text = (f"<b>{html.escape(booking['step'])}</b> {TEAMS_MARKER}: {html.escape(subject)}"
                f"<br>Posted in {html.escape(self.settings.teams_team)} &gt; {html.escape(self.settings.teams_channel)} "
                f"by {html.escape(self.user_bar.first_name or 'unknown user')} via PnC Migration Tool")

        def logged(comment: Comment) -> None:
            booking["teams_logged"] = True
            self._comments.setdefault(ticket_id, []).insert(0, comment)
            self._after_submissions()

        def failed(error: str) -> None:
            self._booking = None
            self._status(f"Teams post not logged in ADO #{ticket_id}: {error}. Task left open.", "error")
            QMessageBox.critical(self, "Couldn't log the Teams post",
                                 f"The Teams post could not be logged in ADO #{ticket_id}:\n{error}")

        self._run(f"Logging the Teams post in ADO #{ticket_id}\u2026",
                  lambda: self._client().add_comment(ticket_id, text), logged, failed)

    def _on_task_resolved(self, card: MigrationCard, task: WorkItem) -> None:
        if card.ticket is not None:
            self._children.pop(card.ticket.id, None)
        self._load_tasks(card)
        self._refresh_today()
        self._status(f"Task #{task.id} is {task.state} and assigned to {task.assigned_to or 'you'}.", "ok")

    def _on_runner_progress(self, text: str) -> None:
        self._status(text, "busy")
        if self.browser is not None:
            self.browser.set_runner_status(text)

    def _on_booking_finished(self, mode: str, reports: list[dict]) -> None:
        browser, runner = self.browser, self._runner
        browser.next_clicked.disconnect(runner.advance)
        browser.show_next(False)
        browser.set_runner_status("")
        runner.deleteLater()
        self._runner = None
        self._end()

        body = ""
        booking = self._booking or {}
        for title in booking.get("skipped", []):
            body += f"<h4>{html.escape(title)}</h4><p>{html_chip('Already logged in the ticket discussion - skipped', 'slate')}</p>"
        for report in reports:
            body += f"<h4>{html.escape(report['form'])}</h4>"
            if report.get("error"):
                body += f"<p>{html_chip('Error', 'red')} {html.escape(report['error'])}</p>"
            if report.get("attach"):
                ok, text = report["attach"]
                body += f"<p>{html_chip(text, 'green' if ok else 'red')}</p>"
            if report.get("save"):
                body += f"<p>{html_chip(report['save'], 'green' if report['save'] == 'Saved as draft' else 'amber')}</p>"
            if report.get("submit"):
                submitted = report["submit"].startswith("Submitted")
                link = f" &nbsp;<a href='{html.escape(report['link'])}'>open</a>" if report.get("link") else ""
                body += f"<p>{html_chip(report['submit'], 'green' if submitted else 'amber')}{link}</p>"
            log = booking.get("log", {}).get(report["form"])
            if log:
                body += f"<p>{html_chip(log[1], 'green' if log[0] else 'red')}</p>"
            if report.get("date_format"):
                body += f"<p>Your ServiceNow date format: <b>{html.escape(report['date_format'])}</b></p>"
            rows = []
            for result in report.get("results", []):
                shown = f" &nbsp; now shows <b>{html.escape(result['shows'])}</b>" if result.get("shows") else ""
                rows.append((result.get("label", ""), _status_chip(str(result.get("status", ""))) + shown))
            body += _rows(rows) if rows else ""
        card = self.selected
        self.details.show_content(RUN_TITLES[mode], f"ServiceNow \u00b7 {booking.get('step', '')}", body,
                                  tone=card.tone if card else "current")
        self.stack.setCurrentWidget(self.details)
        self._status(RUN_TITLES[mode], "ok")
        self._after_submissions()

    def _open_in_browser(self, url: str) -> None:
        if QUrl(url).scheme() != "https":
            return
        browser = self._ensure_browser()
        browser.load(url)
        self.stack.setCurrentWidget(browser)
