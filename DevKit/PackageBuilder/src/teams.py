from __future__ import annotations

import json
import logging
import time

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

TIMEOUT_S = 300
MAIN_WORLD = 0
DIAG_EVERY_S = 15
# The new post's editor resets itself while it opens; type after it settles, then check and retry.
SETTLE_MS = 1500
RECHECK_MS = 1200
FILL_ATTEMPTS = 6
log = logging.getLogger("pnc.teams")


def _safe(url: QUrl) -> str:
    # Query strings / fragments can carry sign-in codes; keep them out of the log.
    return url.toString().split("?", 1)[0].split("#", 1)[0]


def watch_loading(page: QWebEnginePage) -> None:
    """Logs how long each navigation of the Teams page takes."""
    started = [time.monotonic()]

    def on_started() -> None:
        started[0] = time.monotonic()
        log.info("load started: %s", _safe(page.requestedUrl()))

    def elapsed() -> float:
        return time.monotonic() - started[0]

    page.loadStarted.connect(on_started)
    page.urlChanged.connect(lambda url: log.info("+%.1fs url: %s", elapsed(), _safe(url)))
    page.loadFinished.connect(lambda ok: log.info("+%.1fs load finished (ok=%s)", elapsed(), ok))

# Shared by both scripts. Editors that existed before we opened the post (e.g. reply boxes) are tagged
# data-pnc-old and never typed into; the new post is recognised by its subject field or by being untagged.
_HELPERS = """
  function visible(el) { return !!(el && el.getClientRects().length); }
  function editors() {
    return Array.prototype.filter.call(document.querySelectorAll('[contenteditable=true]'), visible);
  }
  function hint(el) {
    return [el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.getAttribute('data-placeholder')].join(' ');
  }
  function findSubject() {
    return Array.prototype.find.call(document.querySelectorAll('input, [contenteditable=true]'),
      function (el) { return visible(el) && /subject/i.test(hint(el)); });
  }
  function findBody(subject) {
    var candidates = editors().filter(function (el) { return el !== subject && !(subject && subject.contains(el)); });
    if (subject) {
      // Stay inside the new post's card so we never type into another post's reply box.
      for (var up = subject.parentElement, depth = 0; up && depth < 8; up = up.parentElement, depth++) {
        var near = candidates.filter(function (el) { return up.contains(el); });
        if (near.length) return near[0];
      }
      return null;
    }
    return candidates.filter(function (el) { return !el.hasAttribute('data-pnc-old'); }).pop() || null;
  }
"""

# Returns a state name; clicks "Post in channel" (never Post) at most 3 times.
_STEP_TEMPLATE = """
(function (where) {
""" + _HELPERS + """
  var webApp = Array.prototype.find.call(document.querySelectorAll('a, button'), function (el) {
    return /use the web app instead/i.test(el.textContent || '');
  });
  if (webApp) { webApp.click(); return 'launcher'; }
  // In the channel view the conversation header is exactly the channel name and there is a Posts tab;
  // "general" alone appears all over Teams (e.g. "Hi General").
  var heads = Array.prototype.map.call(document.querySelectorAll('h1, h2'), function (h) {
    return (h.textContent || '').trim().toLowerCase();
  });
  var postsTab = Array.prototype.some.call(document.querySelectorAll('[role=tab], button'), function (t) {
    return /^posts$/i.test((t.textContent || '').trim());
  });
  if (heads.indexOf(where.channel.toLowerCase()) < 0 || !postsTab) {
    // Teams titles look like "Chat | <conversation> | Microsoft Teams" once the app is up.
    return / \\| /.test(document.title || '') ? 'other-channel' : 'loading';
  }
  var state = window.__pncPost || (window.__pncPost = {tries: 0, last: 0});
  if (state.tries) {
    var subject = findSubject();
    if (subject || findBody(null)) return 'compose';
    if (Date.now() - state.last < 4000) return 'opening';
    if (state.tries >= 3) return 'no-compose';
  }
  var start = Array.prototype.filter.call(document.querySelectorAll('button, [role=button], div, span'), function (el) {
    return /^\\s*(post in channel|start a post|new post|new conversation)\\s*$/i.test(el.textContent || '') && visible(el);
  }).pop();
  if (!start) return 'no-start';
  editors().forEach(function (el) { el.setAttribute('data-pnc-old', ''); });
  var target = start.closest('button, [role=button]') || start;
  ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function (type) {
    var Ev = type.indexOf('pointer') === 0 ? PointerEvent : MouseEvent;
    target.dispatchEvent(new Ev(type, {bubbles: true, cancelable: true, view: window}));
  });
  state.tries++;
  state.last = Date.now();
  return 'opening';
})(__WHERE__)
"""

# Types whatever part of the post is still missing (safe to run repeatedly); never presses Enter or clicks Post.
_FILL_TEMPLATE = """
(function (post) {
""" + _HELPERS + """
  function typeInto(el, lines) {
    el.focus();
    // Replace whatever is there (e.g. a half-typed attempt the editor reset).
    window.getSelection().selectAllChildren(el);
    lines.forEach(function (line, i) {
      if (i) document.execCommand('insertLineBreak');
      document.execCommand('insertText', false, line);
    });
  }
  function has(el, text) { return (el.innerText || el.value || '').indexOf(text) >= 0; }
  function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  // Rich editors may ignore scripted typing but handle paste themselves.
  function pasteInto(el, lines) {
    el.focus();
    window.getSelection().selectAllChildren(el);
    var dt = new DataTransfer();
    dt.setData('text/plain', lines.join('\\n'));
    dt.setData('text/html', lines.map(esc).join('<br>'));
    el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
  }
  function describe(el) {
    return {tag: el.tagName, tid: el.getAttribute('data-tid'), label: el.getAttribute('aria-label'),
            role: el.getAttribute('role'), cls: String(el.className || '').slice(0, 60),
            focused: document.activeElement === el || el.contains(document.activeElement),
            docFocus: document.hasFocus()};
  }
  var subject = findSubject();
  var body = findBody(subject);
  if (!body) return JSON.stringify({subject: subject ? 'waiting' : 'none', body: 'no-box'});
  var lines = post.body.split('\\n'), out = {};
  if (!subject) {
    lines.unshift(post.subject);
    out.subject = 'none';
  } else if (has(subject, post.subject)) {
    out.subject = 'already';
  } else if (subject.tagName === 'INPUT') {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(subject, post.subject);
    subject.dispatchEvent(new Event('input', {bubbles: true}));
    out.subject = 'typed';
  } else {
    typeInto(subject, [post.subject]);
    out.subject = 'typed';
  }
  if (has(body, lines[0]) && has(body, lines[lines.length - 1])) {
    out.body = 'already';
  } else {
    var tries = window.__pncBodyTries = (window.__pncBodyTries || 0) + 1;
    if (tries % 2) { typeInto(body, lines); out.body = 'typed'; }
    else { pasteInto(body, lines); out.body = 'pasted'; }
    out.box = describe(body);
  }
  return JSON.stringify(out);
})(__POST__)
"""

# Snapshot of what the page looks like, for the log when a step is stuck.
_DIAG_TEMPLATE = """
(function (where) {
""" + _HELPERS + """
  var team = where.team.toLowerCase();
  var labels = Array.prototype.map.call(document.querySelectorAll('[aria-label], [title]'), function (el) {
    return (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '');
  }).join(' ').toLowerCase();
  var starts = Array.prototype.filter.call(document.querySelectorAll('button, [role=button], div, span'), function (el) {
    return /^\\s*(post in channel|start a post|new post|new conversation)\\s*$/i.test(el.textContent || '');
  });
  var last = starts[starts.length - 1];
  var clickable = last ? (last.closest('button, [role=button]') || last) : null;
  return JSON.stringify({
    page: location.origin + location.pathname,
    title: document.title,
    readyState: document.readyState,
    teamInText: (document.body ? document.body.textContent : '').toLowerCase().indexOf(team) >= 0,
    teamInLabels: labels.indexOf(team) >= 0,
    headings: Array.prototype.slice.call(document.querySelectorAll('h1, h2, [role=heading]'), 0, 6)
      .map(function (e) { return (e.textContent || '').trim().slice(0, 50); }),
    editors: editors().length,
    oldEditors: document.querySelectorAll('[data-pnc-old]').length,
    subjectField: !!findSubject(),
    startCandidates: starts.length,
    startVisible: last ? visible(last) : null,
    startHtml: clickable ? clickable.outerHTML.slice(0, 300) : '',
    iframes: document.querySelectorAll('iframe').length
  });
})(__WHERE__)
"""


class TeamsPoster(QObject):
    """Opens the Teams channel in the embedded browser and fills a new post; the user clicks Post."""

    progress = Signal(str)

    def __init__(self, page: QWebEnginePage, url: str, team: str, channel: str, subject: str, body: str,
                 test: bool = False, parent: QObject | None = None):
        super().__init__(parent)
        self._page, self._url = page, url
        self._reopened = False
        self._where = f"{team} > {channel}"
        self._done = ("Finish test" if test else "I posted it")
        self._next = ("Test run: do NOT click Post - discard the draft in Teams, then click Finish test." if test
                      else "Review it, click Post in Teams, then click I posted it.")
        self._states = {
            "launcher": "Skipping the Teams launcher page\u2026",
            "loading": f"Waiting for Teams to load {self._where}\u2026 sign in if asked.",
            "other-channel": f"Teams opened something else - click {channel} in Teams yourself.",
            "no-start": f"On {self._where}, but the 'Post in channel' box isn't shown yet\u2026",
            "opening": "Opening a new post\u2026",
        }
        self._last_state = ""
        self._started = 0.0
        self._last_diag = 0.0
        self._fill_attempts = 0
        where = json.dumps({"team": team, "channel": channel})
        self._step_js = _STEP_TEMPLATE.replace("__WHERE__", where)
        self._diag_js = _DIAG_TEMPLATE.replace("__WHERE__", where)
        self._fill_js = _FILL_TEMPLATE.replace("__POST__", json.dumps({"subject": subject, "body": body}))
        self._deadline = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._poll)

    def start(self) -> None:
        self.progress.emit(f"Looking for {self._where}\u2026 sign in to Teams if asked.")
        self._started = time.monotonic()
        log.info("post started; Teams page is at %s", _safe(self._page.url()))
        self._page.runJavaScript("window.__pncPost = null; window.__pncBodyTries = 0;", MAIN_WORLD)
        self._deadline = time.monotonic() + TIMEOUT_S
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._fill_attempts = FILL_ATTEMPTS

    def _poll(self) -> None:
        self._page.runJavaScript(self._step_js, MAIN_WORLD, self._on_step)

    def _on_step(self, state) -> None:
        if not self._timer.isActive():
            return
        elapsed = time.monotonic() - self._started
        if state != self._last_state:
            log.info("+%.1fs state: %s", elapsed, state)
        if state in ("no-start", "no-compose", "opening", "other-channel") and (
                state != self._last_state or time.monotonic() - self._last_diag > DIAG_EVERY_S):
            self._last_diag = time.monotonic()
            self._page.runJavaScript(self._diag_js, MAIN_WORLD,
                                     lambda raw, s=state: log.info("+%.1fs page while %s: %s", elapsed, s, raw))
        if state == "other-channel" and not self._reopened:
            # Teams sometimes drops the deep link while starting up; opening it once more lands on the channel.
            self._reopened = True
            self._last_state = "loading"
            log.info("+%.1fs not on the channel; opening the channel link again", elapsed)
            self.progress.emit(self._states["loading"])
            self._page.load(QUrl(self._url))
            return
        if state == "compose":
            self._timer.stop()
            self._fill_attempts = 0
            self.progress.emit("New post opened - filling it\u2026")
            QTimer.singleShot(SETTLE_MS, self._fill)
        elif state == "no-compose":
            self._timer.stop()
            self.progress.emit("Clicked 'Post in channel' but no new post opened (discard any open draft first). "
                               f"Start the post yourself, then click {self._done}.")
        elif time.monotonic() > self._deadline:
            self._timer.stop()
            self.progress.emit(f"Gave up on {self._where} ({self._states.get(state, state)}) - write the post "
                               f"yourself, then click {self._done}.")
        elif state != self._last_state:
            self._last_state = state
            self.progress.emit(self._states.get(state, str(state)))

    def _fill(self) -> None:
        if self._fill_attempts >= FILL_ATTEMPTS:
            return
        self._fill_attempts += 1
        # The editor only takes input while the browser view has keyboard focus.
        view = QWebEngineView.forPage(self._page)
        if view is not None:
            view.setFocus()
        self._page.runJavaScript(self._fill_js, MAIN_WORLD, self._on_filled)

    def _on_filled(self, raw) -> None:
        log.info("+%.1fs fill attempt %d: %s", time.monotonic() - self._started, self._fill_attempts, raw)
        try:
            data = json.loads(raw) if isinstance(raw, str) else {}
        except ValueError:
            data = {}
        subject, body = data.get("subject"), data.get("body")
        if subject in ("already", "none") and body == "already":
            note = "" if subject == "already" else " (no subject field - subject is the first line)"
            self.progress.emit(f"Post filled{note}. {self._next}")
            return
        if self._fill_attempts < FILL_ATTEMPTS:
            # Re-run after a moment: it types anything the editor dropped and confirms the rest stuck.
            QTimer.singleShot(RECHECK_MS, self._fill)
            return
        missing = " and ".join(name for name, value in (("subject", subject), ("message", body))
                                if value not in ("already", "none")) or "the post"
        self.progress.emit(f"Couldn't fill the {missing} - type it yourself. {self._next}")
