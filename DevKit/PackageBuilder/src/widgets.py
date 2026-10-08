from __future__ import annotations

import html
import re
from datetime import date

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
    QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from .ado import WorkItem
from .config import DATA_DIR
from .schedule import MigrationEntry
from .steps import DONE_STATES
from .theme import AVATAR_COLORS, CHIPS, MUTED

_profile: QWebEngineProfile | None = None
_dots: dict[str, QIcon] = {}


def fmt_date(value: date | None) -> str:
    return value.strftime("%a %b %d, %Y") if value else "\u2014"


def relative_days(value: date) -> str:
    delta = (value - date.today()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    return f"in {delta} days" if delta > 0 else f"{-delta} days ago"


def person(name: str, backup: str) -> str:
    name = name or "\u2014"
    return f"{name}  (backup {backup})" if backup else name


def label(text: str = "", role: str | None = None) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setProperty("role", role)
    return widget


def button(text: str, primary: bool = False, role: str | None = None) -> QPushButton:
    widget = QPushButton(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        widget.setProperty("primary", True)
    if role:
        widget.setProperty("role", role)
    return widget


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def web_profile() -> QWebEngineProfile:
    global _profile
    if _profile is None:
        # A named profile keeps the SharePoint / Azure DevOps sign-in between runs.
        _profile = QWebEngineProfile("pnc-migration", QApplication.instance())
        _profile.setPersistentStoragePath(str(DATA_DIR / "web"))
        _profile.setCachePath(str(DATA_DIR / "web-cache"))
        # Teams rejects browsers that identify as QtWebEngine.
        _profile.setHttpUserAgent(re.sub(r"\s*QtWebEngine/\S+", "", _profile.httpUserAgent()))
    return _profile


def state_chip_color(state: str) -> str:
    state = state.lower()
    if state in DONE_STATES:
        return "green"
    if state in {"active", "in progress", "doing", "committed", "open"}:
        return "blue"
    if state in {"blocked", "removed", "on hold"}:
        return "red"
    if state in {"new", "to do", "proposed", "approved"}:
        return "slate"
    return "violet"


def chip(text: str, color: str) -> QLabel:
    widget = label(text)
    widget.setProperty("chip", color)
    return widget


def set_chip(widget: QLabel, text: str, color: str) -> None:
    widget.setText(text)
    widget.setProperty("chip", color)
    repolish(widget)


def html_chip(text: str, color: str) -> str:
    bg, fg = CHIPS[color]
    return f"<span style='background-color:{bg}; color:{fg}; font-weight:600;'>&nbsp;{html.escape(text)}&nbsp;</span>"


def dot_icon(color: str) -> QIcon:
    if color not in _dots:
        pixmap = QPixmap(20, 20)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(3, 3, 14, 14)
        painter.end()
        _dots[color] = QIcon(pixmap)
    return _dots[color]


def _skeleton(width: int | None, height: int = 12) -> QFrame:
    bar = QFrame(objectName="Skeleton")
    bar.setFixedHeight(height)
    if width:
        bar.setFixedWidth(width)
    return bar


class Avatar(QLabel):
    def __init__(self, size: int = 32):
        super().__init__()
        self._size = size
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_name(self, name: str) -> None:
        initials = "".join(part[0] for part in name.split()[:2]).upper() or "?"
        color = AVATAR_COLORS[sum(map(ord, name)) % len(AVATAR_COLORS)] if name else "#94a3b8"
        self.setText(initials)
        self.setStyleSheet(
            f"background: {color}; color: white; border-radius: {self._size // 2}px; font-weight: 700; font-size: 9pt;")


def display_name(name: str) -> str:
    """'Han, Bill' -> 'Bill Han'."""
    if "," in name:
        last, first = (part.strip() for part in name.split(",", 1))
        return f"{first} {last}".strip()
    return name.strip()


def migration_label(entry: MigrationEntry) -> str:
    return f"Migration {entry.prod_date:%b %d}"


class UserBar(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.first_name = ""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(10)
        layout.addStretch()
        self._roles = QHBoxLayout()
        self._roles.setSpacing(6)
        layout.addLayout(self._roles)
        self._avatar = Avatar(36)
        self._avatar.set_name("")
        layout.addWidget(self._avatar)
        text = QVBoxLayout()
        text.setSpacing(0)
        self._name = label("Connecting\u2026", "value")
        self._email = label("", "caption")
        text.addWidget(self._name)
        text.addWidget(self._email)
        layout.addLayout(text)

    def set_user(self, name: str, email: str) -> None:
        shown = display_name(name)
        self.first_name = shown.split()[0] if shown else ""
        self._avatar.set_name(shown)
        self._name.setText(shown or "Unknown user")
        self._email.setText(email)

    def set_error(self, error: str) -> None:
        self.first_name = ""
        self._avatar.set_name("")
        self._name.setText("Not connected to Azure DevOps")
        self._email.setText(error)

    def set_roles(self, roles: list[tuple[str, str]]) -> None:
        while self._roles.count():
            widget = self._roles.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        for text, color in roles:
            self._roles.addWidget(chip(text, color))


class PersonLine(QWidget):
    def __init__(self, role: str, compact: bool):
        super().__init__()
        self._role, self._compact = role, compact
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._avatar = Avatar(22 if compact else 26)
        self._text = label()
        # Let long names clip instead of widening the card.
        self._text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._avatar)
        layout.addWidget(self._text, 1)

    def set_people(self, name: str, backup: str) -> None:
        self._avatar.set_name(self._role)
        full = f"{self._role}: {name or '—'}" + (f"  ·  backup {backup}" if backup else "")
        self._text.setText((name or "—") if self._compact else full)
        self.setToolTip(full)


class MigrationCard(QFrame):
    """Selectable Current / Next migration card; the compact variant shows less detail."""

    clicked = Signal(object)

    def __init__(self, tone: str, badge: str, compact: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.tone, self.badge, self.compact = tone, badge, compact
        self.entry: MigrationEntry | None = None
        self.ticket: WorkItem | None = None
        self.ticket_note = ""
        self.setObjectName("MigrationTab")
        self.setProperty("tone", tone)
        self.setProperty("selected", False)
        self.setMinimumWidth(150)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)
        top = QHBoxLayout()
        top.addWidget(label(badge.upper(), "tabBadge"))
        top.addStretch()
        self._progress = label("", "caption")
        top.addWidget(self._progress)
        layout.addLayout(top)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)

        skeleton = QWidget()
        skeleton_layout = QVBoxLayout(skeleton)
        skeleton_layout.setContentsMargins(0, 4, 0, 0)
        skeleton_layout.setSpacing(8)
        skeleton_layout.addWidget(_skeleton(90, 18))
        for width in (130, 160, 140):
            skeleton_layout.addWidget(_skeleton(width, 10))
        skeleton_layout.addStretch()
        pulse = QGraphicsOpacityEffect(skeleton)
        skeleton.setGraphicsEffect(pulse)
        self._pulse = QPropertyAnimation(pulse, b"opacity", self)
        self._pulse.setDuration(1400)
        self._pulse.setStartValue(1.0)
        self._pulse.setKeyValueAt(0.5, 0.35)
        self._pulse.setEndValue(1.0)
        self._pulse.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._pulse.setLoopCount(-1)
        self._stack.addWidget(skeleton)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(4)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self._title = label("", "tabTitleSmall" if compact else "tabTitle")
        self._state = chip("ADO", "slate")
        title_row.addWidget(self._title)
        title_row.addStretch()
        title_row.addWidget(self._state, 0, Qt.AlignmentFlag.AlignVCenter)
        content_layout.addLayout(title_row)
        self._countdown = label("", "muted")
        content_layout.addWidget(self._countdown)
        content_layout.addSpacing(4)
        self._builder = PersonLine("Builder", compact)
        self._deployer = PersonLine("Deployer", compact)
        content_layout.addWidget(self._builder)
        content_layout.addWidget(self._deployer)
        self._ticket = label("", "caption")
        self._ticket.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        content_layout.addWidget(self._ticket)
        content_layout.addStretch()
        self._stack.addWidget(content)

        message = QWidget()
        message_layout = QVBoxLayout(message)
        message_layout.setContentsMargins(0, 0, 0, 0)
        message_layout.setSpacing(2)
        self._message_title = label("", "value")
        self._message = label("", "muted")
        self._message.setWordWrap(True)
        message_layout.addWidget(self._message_title)
        message_layout.addWidget(self._message)
        message_layout.addStretch()
        self._stack.addWidget(message)

        self.set_loading()

    def set_loading(self) -> None:
        self.entry = None
        self.ticket = None
        self.ticket_note = ""
        self._progress.setText("")
        self._stack.setCurrentIndex(0)
        self._pulse.start()
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_message(self, title: str, message: str) -> None:
        self._pulse.stop()
        self._message_title.setText(title)
        self._message.setText(message)
        self._stack.setCurrentIndex(2)

    def set_entry(self, entry: MigrationEntry) -> None:
        self._pulse.stop()
        self.entry = entry
        self._title.setText(f"{entry.prod_date:%b %d}")
        when = relative_days(entry.prod_date)
        if self.compact:
            self._countdown.setText(when.capitalize())
        else:
            freeze = f"  ·  code freeze {entry.code_freeze:%b %d}" if entry.code_freeze else ""
            self._countdown.setText(f"PROD {when}{freeze}")
        self._builder.set_people(entry.builder, entry.builder_backup)
        self._deployer.set_people(entry.deployer, entry.deployer_backup)
        self.set_ticket_status("ADO\u2026", "slate", "Loading Azure DevOps ticket\u2026")
        self._stack.setCurrentIndex(1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_ticket(self, ticket: WorkItem, others: int = 0) -> None:
        self.ticket = ticket
        self.ticket_note = f"{others} other matching ticket(s) were also found." if others else ""
        set_chip(self._state, ticket.state or "?", state_chip_color(ticket.state))
        self._state.setToolTip(f"#{ticket.id}  {ticket.title}")
        sprint = ticket.iteration_path.rsplit("\\", 1)[-1]
        self._ticket.setText(f"ADO #{ticket.id}" if self.compact else f"ADO #{ticket.id}  ·  {sprint}")

    def set_ticket_status(self, text: str, color: str, note: str) -> None:
        self.ticket = None
        self.ticket_note = note
        set_chip(self._state, text, color)
        self._state.setToolTip(note)
        self._ticket.setText("" if self.compact else note)

    def set_progress(self, done: int, total: int) -> None:
        self._progress.setText(f"{done}/{total}")

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        # Descendant rules depend on this property, so children need re-polishing too.
        for widget in (self, *self.findChildren(QFrame)):
            repolish(widget)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.entry is not None:
            self.clicked.emit(self)
        super().mousePressEvent(event)


class TodayBanner(QFrame):
    """What to do today across the loaded migrations."""

    picked = Signal(object, str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Today")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)
        self._header = label("", "tabBadge")
        layout.addWidget(self._header)
        self._rows = QVBoxLayout()
        self._rows.setSpacing(4)
        layout.addLayout(self._rows)
        self.show_message("Checking today's tasks\u2026")

    def _reset(self, state: str) -> None:
        self._header.setText(f"TODAY  \u00b7  {date.today():%a %b %d}".upper())
        while self._rows.count():
            widget = self._rows.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self.setProperty("state", state)

    def _add_item(self, text: str, card, step_name: str) -> None:
        item = button(text, role="todayItem")
        item.clicked.connect(lambda _checked=False: self.picked.emit(card, step_name))
        self._rows.addWidget(item)

    def _finish(self) -> None:
        for widget in (self, *self.findChildren(QFrame)):
            repolish(widget)

    def show_message(self, text: str) -> None:
        self._reset("idle")
        self._rows.addWidget(label(text, "value"))
        self._finish()

    def show_agenda(self, due: list, next_up: tuple | None) -> None:
        """due / next_up hold (card, PlannedStep) pairs."""
        today = date.today()
        self._reset("due" if due else "clear")
        for card, planned in due:
            when = "Overdue" if planned.due_date < today else "Do today"
            self._add_item(f"{when}  \u00b7  {planned.step.name}   \u2014   {migration_label(card.entry)}",
                           card, planned.step.name)
        if not due:
            self._rows.addWidget(label("No task today", "tabTitleSmall"))
            if next_up is not None:
                card, planned = next_up
                self._add_item(
                    f"Next: {planned.step.name}  \u00b7  {planned.due_date:%a %b %d} "
                    f"({relative_days(planned.due_date)})   \u2014   {migration_label(card.entry)}",
                    card, planned.step.name)
        self._finish()


class DetailsPane(QWidget):
    open_url = Signal(str)
    action = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QFrame(objectName="DetailsHeader")
        self._header.setProperty("tone", "current")
        head = QHBoxLayout(self._header)
        head.setContentsMargins(22, 14, 22, 14)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self._subtitle = label("", "cardBadge")
        self._title = label("Loading\u2026", "cardTitle")
        titles.addWidget(self._subtitle)
        titles.addWidget(self._title)
        head.addLayout(titles, 1)
        self._actions = QHBoxLayout()
        self._actions.setSpacing(8)
        head.addLayout(self._actions)
        layout.addWidget(self._header)

        self._body = QTextBrowser()
        self._body.setOpenLinks(False)
        self._body.anchorClicked.connect(self._on_link)
        self._body.document().setDocumentMargin(22)
        self._body.document().setDefaultStyleSheet(
            f"td.k {{ color: {MUTED}; padding-right: 22px; font-weight: 600; }} td {{ padding: 6px 0; }}"
            "h4 { color: #7c3aed; margin-top: 18px; } a { color: #2563eb; font-weight: 600; }"
        )
        layout.addWidget(self._body, 1)

    def show_content(self, title: str, subtitle: str, body: str, tone: str = "current",
                     actions: tuple = ()) -> None:
        """actions: (action id, button text, enabled, tooltip) tuples, emitted through `action`."""
        self._title.setText(title)
        self._subtitle.setText(subtitle.upper())
        self._body.setHtml(body)
        while self._actions.count():
            widget = self._actions.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        for action_id, text, enabled, tip in actions:
            widget = button(text)
            widget.setEnabled(enabled)
            widget.setToolTip(tip)
            widget.clicked.connect(lambda _checked=False, a=action_id: self.action.emit(a))
            self._actions.addWidget(widget)
        self._header.setProperty("tone", tone)
        repolish(self._header)

    def _on_link(self, url: QUrl) -> None:
        if url.scheme() == "https":
            self.open_url.emit(url.toString())
        elif url.scheme() == "action":
            self.action.emit(url.path())


class BrowserPane(QWidget):
    closed = Signal()
    next_clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        details = button("\u2039  Details")
        details.clicked.connect(self.closed.emit)
        back = button("Back")
        reload = button("Reload")
        self._address = QLineEdit()
        self._address.setReadOnly(True)
        external = button("Open externally")
        for widget in (details, back, reload):
            bar.addWidget(widget)
        bar.addWidget(self._address, 1)
        bar.addWidget(external)
        layout.addLayout(bar)

        runner_row = QHBoxLayout()
        self._runner_status = label("", "muted")
        self._runner_status.setWordWrap(True)
        self._next = button("Next form  \u203a")
        self._next.clicked.connect(self.next_clicked.emit)
        self._next.hide()
        runner_row.addWidget(self._runner_status, 1)
        runner_row.addWidget(self._next)
        layout.addLayout(runner_row)

        self._view = QWebEngineView(self)
        self._view.setPage(QWebEnginePage(web_profile(), self._view))
        layout.addWidget(self._view, 1)

        back.clicked.connect(self._view.back)
        reload.clicked.connect(self._view.reload)
        external.clicked.connect(lambda: QDesktopServices.openUrl(self._view.url()))
        self._view.urlChanged.connect(lambda url: self._address.setText(url.toString()))

    def load(self, url: str) -> None:
        self._view.setUrl(QUrl(url))

    def page(self) -> QWebEnginePage:
        return self._view.page()

    def set_runner_status(self, text: str) -> None:
        self._runner_status.setText(text)

    def show_next(self, visible: bool, text: str = "Next form  \u203a") -> None:
        self._next.setText(text)
        self._next.setVisible(visible)
