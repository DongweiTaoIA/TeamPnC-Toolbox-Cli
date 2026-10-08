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
    QFrame, QHBoxLayout, QHeaderView, QInputDialog, QMainWindow, QMessageBox, QProgressBar, QSplitter,
    QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .ado import AdoClient, AdoError, Comment, TicketCreationResult, WorkItem
from .booking import FormSpec, booking_forms, refresh_forms, refresh_post
from .config import DATA_DIR, load_settings
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
        if step.action and card is not None and card.entry is not None:
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
