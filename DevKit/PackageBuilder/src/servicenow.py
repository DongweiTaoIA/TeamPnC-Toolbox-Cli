from __future__ import annotations

import base64
import json
import time

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage

from .booking import FormSpec

READY_TIMEOUT_S = 180
SAVE_WAIT_MS = 6000
MAIN_WORLD = 0

# Service Portal keeps its GlideForm on the catalog item's Angular scope.
_FIND_GFORM = """
function findGForm() {
  if (window.g_form && window.g_form.getFieldNames) return window.g_form;
  if (!window.angular) return null;
  var nodes = document.querySelectorAll('sp-variable-layout, [sp-variable-layout], .ng-scope');
  for (var i = 0; i < nodes.length; i++) {
    try {
      var scope = angular.element(nodes[i]).scope();
      if (scope && typeof scope.getGlideForm === 'function') return scope.getGlideForm();
    } catch (e) {}
  }
  return null;
}
"""

READY_JS = _FIND_GFORM + """
(function () {
  var gf = findGForm();
  if (gf && gf.getFieldNames && gf.getFieldNames().length) return true;
  return /not authorized or record is not valid/i.test(document.body ? document.body.innerText : '') ? 'invalid' : false;
})()
"""

_FILL_TEMPLATE = _FIND_GFORM + """
(function (spec) {
  window.__pncFill = '';
  function norm(s) { return String(s || '').replace(/[*:]/g, '').replace(/\\s+/g, ' ').trim().toLowerCase(); }
  function fmtDate(iso, fmt) {
    var m = /^(\\d{4})-(\\d{2})-(\\d{2})(?: (\\d{2}):(\\d{2}):(\\d{2}))?$/.exec(iso);
    if (!m || !fmt) return iso;
    var H = +(m[4] || 0), h12 = H % 12 || 12;
    var parts = {yyyy: m[1], yy: m[1].slice(2), MM: m[2], M: String(+m[2]), dd: m[3], d: String(+m[3]),
                 HH: m[4] || '00', H: String(H), hh: String(h12).padStart(2, '0'), h: String(h12),
                 mm: m[5] || '00', ss: m[6] || '00', a: H < 12 ? 'AM' : 'PM'};
    return fmt.replace(/yyyy|yy|MM|M|dd|d|HH|H|hh|h|mm|ss|a/g, function (t) { return parts[t]; });
  }
  var gf = findGForm();
  if (!gf) { window.__pncFill = JSON.stringify({error: 'Form not found on the page'}); return 'started'; }
  var fields = (gf.getFieldNames() || []).map(function (name) {
    var f = (gf.getField && gf.getField(name)) || {};
    return {name: name, label: norm(f.label || (gf.getLabelOf && gf.getLabelOf(name)) || name), field: f};
  });
  function find(label) {
    var l = norm(label);
    return fields.find(function (x) { return x.label === l; }) ||
           fields.find(function (x) { return x.label.indexOf(l) === 0; });
  }
  function pickUnique(list, wanted, labelOf) {
    var exact = list.filter(function (x) { return norm(labelOf(x)) === wanted; });
    if (exact.length === 1) return exact[0];
    var partial = list.filter(function (x) {
      var l = norm(labelOf(x));
      return l.indexOf(wanted) === 0 || l.split(/[\\s,]+/).indexOf(wanted) >= 0;
    });
    return partial.length === 1 ? partial[0] : null;
  }

  async function lookupRef(f, name, query) {
    var table = (f.ed && f.ed.reference) || f.refTable;
    if (!table) return {error: 'unknown dropdown type'};
    var resp = await fetch('/api/now/table/' + encodeURIComponent(table) +
      '?sysparm_limit=20&sysparm_fields=sys_id,name&sysparm_query=' + encodeURIComponent(query),
      {credentials: 'same-origin', headers: {Accept: 'application/json', 'X-UserToken': window.g_ck || ''}});
    if (!resp.ok) return {error: 'name lookup failed (' + resp.status + ')'};
    var row = pickUnique((await resp.json()).result || [], norm(name), function (r) { return r.name; });
    return row ? {row: row} : {error: '"' + name + '" not found'};
  }

  async function fillPerson(target, f, name, out) {
    var wanted = norm(name);
    function blank(reason) { gf.setValue(target.name, ''); out.status = reason + ' - left blank'; return out; }
    if (!wanted) return blank('no name in the schedule');
    if (f.choices && f.choices.length) {
      var choice = pickUnique(f.choices, wanted, function (c) { return c.label; });
      if (!choice) return blank('"' + name + '" is not in the list');
      gf.setValue(target.name, choice.value);
      out.status = 'filled'; out.shows = choice.label; return out;
    }
    var safe = String(name).replace(/[\\^=]/g, '');
    var found = await lookupRef(f, name, 'active=true^nameSTARTSWITH' + safe + '^ORfirst_name=' + safe);
    if (found.error) return blank(found.error);
    gf.setValue(target.name, found.row.sys_id, found.row.name);
    out.status = 'filled'; out.shows = found.row.name; return out;
  }

  async function fillReference(target, f, name, out) {
    var found = await lookupRef(f, name, 'nameSTARTSWITH' + String(name).replace(/[\\^=]/g, ''));
    if (found.error) { out.status = found.error + ' - select manually'; return out; }
    gf.setValue(target.name, found.row.sys_id, found.row.name);
    out.status = 'filled'; out.shows = found.row.name; return out;
  }

  async function fillOne(item) {
    var out = {label: item.label}, value = item.value, target = find(item.label);
    if (!target && item.appendTo) {
      var host = find(item.appendTo);
      if (host) {
        var current = gf.getValue(host.name) || '';
        gf.setValue(host.name, (current ? current + '\\n\\n' : '') + item.label + ':\\n' + value);
        out.status = 'appended to ' + item.appendTo; return out;
      }
    }
    if (!target) { out.status = 'not found'; return out; }
    var f = target.field || {}, type = String(f.type || '').toLowerCase();
    if (item.kind === 'person') return fillPerson(target, f, value, out);
    if (item.kind === 'reference') return fillReference(target, f, value, out);
    if (item.kind === 'date' || item.kind === 'datetime') {
      var fmt = item.kind === 'date' ? (window.g_user_date_format || 'yyyy-MM-dd')
                                     : (window.g_user_date_time_format || 'yyyy-MM-dd HH:mm:ss');
      var text = fmtDate(value, fmt);
      gf.setValue(target.name, text);
      var back = String(gf.getValue(target.name) || '');
      out.value = text; out.shows = back;
      out.status = norm(back) === norm(text) ? 'filled' : 'check value';
      return out;
    }
    if (typeof value === 'boolean') {
      gf.setValue(target.name, value ? 'true' : 'false');
    } else if (f.choices && f.choices.length) {
      var choice = f.choices.find(function (c) { return norm(c.label) === norm(value) || norm(c.value) === norm(value); });
      if (!choice) { out.status = 'choice not found'; return out; }
      gf.setValue(target.name, choice.value);
    } else if (type.indexOf('reference') >= 0 || (f.ed && f.ed.reference)) {
      out.status = 'reference field - select manually'; return out;
    } else {
      gf.setValue(target.name, value);
    }
    out.status = 'filled'; return out;
  }

  (async function () {
    var results = [];
    for (var i = 0; i < spec.length; i++) {
      try { results.push(await fillOne(spec[i])); }
      catch (e) { results.push({label: spec[i].label, status: 'error: ' + e.message}); }
    }
    window.__pncFill = JSON.stringify({results: results,
      dateFormat: window.g_user_date_format || '', dateTimeFormat: window.g_user_date_time_format || ''});
  })();
  return 'started';
})(__SPEC__)
"""

FILL_RESULT_JS = "window.__pncFill || ''"
FILL_TIMEOUT_S = 30
ATTACH_TIMEOUT_S = 30

# Hands the files to the form's "Add attachments" zone (or the portal's paperclip), as if the user picked them.
_ATTACH_TEMPLATE = """
(function (files) {
  var dt = new DataTransfer();
  files.forEach(function (f) {
    var bin = atob(f.data), bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    dt.items.add(new File([bytes], f.name, {type: 'text/plain'}));
  });
  function pick(input) {
    input.files = dt.files;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
    return 'started';
  }
  var choose = Array.prototype.find.call(document.querySelectorAll('a, button, label, span'),
    function (el) { return /^choose a file$/i.test((el.innerText || '').trim()); });
  if (choose) {
    for (var zone = choose, depth = 0; zone && depth < 6; zone = zone.parentElement, depth++) {
      var input = zone.querySelector('input[type=file]');
      if (input) return pick(input);
    }
    ['dragenter', 'dragover', 'drop'].forEach(function (type) {
      choose.dispatchEvent(new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: dt}));
    });
    return 'started';
  }
  var fallback = document.querySelector('sp-attachment-button input[type=file]') ||
                 document.querySelector('input[type=file]');
  return fallback ? pick(fallback) : 'no-input';
})(__FILES__)
"""

_ATTACHED_TEMPLATE = """
(function (names) {
  var text = document.body ? document.body.innerText : '';
  return names.every(function (n) { return text.indexOf(n) >= 0; });
})(__NAMES__)
"""

# Only ever clicks "Save as Draft"; anything that would submit the request is refused.
SAVE_DRAFT_JS = """
(function () {
  var forbidden = /^(request|submit|order( now)?|checkout)$/i;
  var candidates = Array.prototype.slice.call(document.querySelectorAll('button, a, input[type=button], input[type=submit]'));
  var target = candidates.find(function (el) { return /save as draft/i.test((el.innerText || el.value || '').trim()); });
  if (!target) return 'not-found';
  if (forbidden.test((target.innerText || target.value || '').trim())) return 'blocked';
  target.click();
  return 'clicked';
})()
"""

SAVE_MESSAGES = {
    "clicked": "Saved as draft",
    "not-found": "Save as Draft button not found - nothing was submitted; save it manually",
    "blocked": "Refused to click a submit button",
}

# Detects the confirmation page shown after the user clicks Request, and reads the REQ number.
SUBMITTED_JS = """
(function () {
  if (window.__pncReq) return window.__pncReq;
  if (window.__pncReqPending) return '';
  var params = new URLSearchParams(location.search);
  var page = params.get('id') || '';
  var sysId = params.get('table') === 'sc_request' ? (params.get('sys_id') || '') : '';
  var number = '';
  if (!sysId) {
    if (['order_status', 'ticket', 'sc_request'].indexOf(page) < 0) return '';
    var found = (document.body ? document.body.innerText : '').match(/\\bREQ\\d{5,}\\b/g) || [];
    var unique = found.filter(function (n, i) { return found.indexOf(n) === i; });
    if (unique.length !== 1) return '';
    number = unique[0];
  }
  window.__pncReqPending = true;
  var query = sysId ? 'sys_id=' + sysId : 'number=' + number;
  fetch('/api/now/table/sc_request?sysparm_limit=1&sysparm_fields=sys_id,number&sysparm_query=' + encodeURIComponent(query),
        {credentials: 'same-origin', headers: {Accept: 'application/json', 'X-UserToken': window.g_ck || ''}})
    .then(function (r) { return r.ok ? r.json() : {result: []}; })
    .then(function (d) {
      var row = (d.result || [])[0] || {};
      window.__pncReq = JSON.stringify({sys_id: row.sys_id || sysId, number: row.number || number});
    })
    .catch(function () { window.__pncReq = JSON.stringify({sys_id: sysId, number: number}); });
  return '';
})()
"""


def fill_js(form: FormSpec) -> str:
    spec = [{"label": f.label, "value": f.value, "appendTo": f.append_to, "kind": f.kind} for f in form.fields]
    return _FILL_TEMPLATE.replace("__SPEC__", json.dumps(spec))


def attach_js(form: FormSpec) -> str:
    files = [{"name": a.name, "data": base64.b64encode(a.content).decode("ascii")} for a in form.attachments]
    return _ATTACH_TEMPLATE.replace("__FILES__", json.dumps(files))


def attached_js(form: FormSpec) -> str:
    return _ATTACHED_TEMPLATE.replace("__NAMES__", json.dumps([a.name for a in form.attachments]))


def _parse(raw) -> dict:
    try:
        data = json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        return {"error": "Unexpected response from the page"}
    return data if isinstance(data, dict) else {}


class ServiceNowRunner(QObject):
    """Opens each form in the embedded browser and fills it, or fills it and saves a draft.

    mode: "test" (fill + Save as Draft) or "fill" (fill; the user reviews and clicks Request,
    then the REQ number is emitted via `submitted` and the runner waits for `advance()`).
    """

    progress = Signal(str)
    waiting = Signal(bool)
    submitted = Signal(str, str, str)  # form title, REQ number, request URL
    finished = Signal(list)

    def __init__(self, page: QWebEnginePage, forms: list[FormSpec], mode: str, parent: QObject | None = None):
        super().__init__(parent)
        self._page, self._forms, self._mode = page, forms, mode
        self._index = -1
        self._reports: list[dict] = []
        self._current: dict = {}
        self._deadline = 0.0
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._check_ready)
        self._fill_poll = QTimer(self)
        self._fill_poll.setInterval(500)
        self._fill_poll.timeout.connect(self._check_filled)
        self._attach_poll = QTimer(self)
        self._attach_poll.setInterval(1000)
        self._attach_poll.timeout.connect(self._check_attached)
        self._submit_poll = QTimer(self)
        self._submit_poll.setInterval(1500)
        self._submit_poll.timeout.connect(self._check_submitted)

    @property
    def form(self) -> FormSpec:
        return self._forms[self._index]

    def start(self) -> None:
        self._next()

    def advance(self) -> None:
        self.waiting.emit(False)
        if self._submit_poll.isActive():
            self._submit_poll.stop()
            self._report(submit="Not submitted (skipped)", **self._current)
        self._next()

    def _step_text(self, text: str) -> str:
        return f"{self.form.title} form ({self._index + 1}/{len(self._forms)}): {text}"

    def _report(self, **details) -> None:
        self._reports.append({"form": self.form.title, "url": self.form.url, **details})

    def _next(self) -> None:
        self._index += 1
        if self._index >= len(self._forms):
            self.finished.emit(self._reports)
            return
        self.progress.emit(self._step_text("opening\u2026 (sign in if asked)"))
        self._page.load(QUrl(self.form.url))
        self._deadline = time.monotonic() + READY_TIMEOUT_S
        self._poll.start()

    def _check_ready(self) -> None:
        self._page.runJavaScript(READY_JS, MAIN_WORLD, self._on_ready)

    def _on_ready(self, ready) -> None:
        if not self._poll.isActive():
            return
        if ready is True:
            self._poll.stop()
            self._act()
        elif ready == "invalid":
            self._poll.stop()
            self._report(error="ServiceNow says the catalog item is not valid or you are not authorized "
                               "- check this form's URL in config.json")
            self._next()
        elif time.monotonic() > self._deadline:
            self._poll.stop()
            self._report(error="The form did not load in time (sign-in needed?)")
            self._next()

    def _act(self) -> None:
        self.progress.emit(self._step_text("filling\u2026"))
        self._page.runJavaScript(fill_js(self.form), MAIN_WORLD, self._on_fill_started)

    def _on_fill_started(self, _started) -> None:
        # Name lookups are asynchronous in the page, so poll for the result.
        self._deadline = time.monotonic() + FILL_TIMEOUT_S
        self._fill_poll.start()

    def _check_filled(self) -> None:
        self._page.runJavaScript(FILL_RESULT_JS, MAIN_WORLD, self._on_fill_result)

    def _on_fill_result(self, raw) -> None:
        if not self._fill_poll.isActive():
            return
        if raw:
            self._fill_poll.stop()
            self._on_filled(raw)
        elif time.monotonic() > self._deadline:
            self._fill_poll.stop()
            self._on_filled(json.dumps({"error": "Filling the form timed out"}))

    def _on_filled(self, raw) -> None:
        data = _parse(raw)
        self._current = {"results": data.get("results", []), "error": data.get("error"),
                         "date_format": data.get("dateTimeFormat") or data.get("dateFormat") or ""}
        if self.form.attachments and not data.get("error"):
            self.progress.emit(self._step_text("attaching files\u2026"))
            self._page.runJavaScript(attach_js(self.form), MAIN_WORLD, self._on_attach_started)
        else:
            self._after_fill()

    def _on_attach_started(self, status) -> None:
        if status != "started":
            self._current["attach"] = (False, "Attachment button not found - attach the files manually")
            self._after_fill()
            return
        self._deadline = time.monotonic() + ATTACH_TIMEOUT_S
        self._attach_poll.start()

    def _check_attached(self) -> None:
        self._page.runJavaScript(attached_js(self.form), MAIN_WORLD, self._on_attach_probe)

    def _on_attach_probe(self, attached) -> None:
        if not self._attach_poll.isActive():
            return
        names = ", ".join(a.name for a in self.form.attachments)
        if attached is True:
            self._attach_poll.stop()
            self._current["attach"] = (True, f"Attached {names}")
            self._after_fill()
        elif time.monotonic() > self._deadline:
            self._attach_poll.stop()
            self._current["attach"] = (False, f"Couldn't confirm the upload of {names} - check the attachments")
            self._after_fill()

    def _after_fill(self) -> None:
        if self._mode == "test":
            self.progress.emit(self._step_text("saving as draft\u2026"))
            self._page.runJavaScript(SAVE_DRAFT_JS, MAIN_WORLD, self._on_saved)
        else:
            self.progress.emit(self._step_text(
                "filled - review it and click Request yourself. Press Next form to skip without submitting."))
            self.waiting.emit(True)
            self._submit_poll.start()

    def _check_submitted(self) -> None:
        self._page.runJavaScript(SUBMITTED_JS, MAIN_WORLD, self._on_submit_probe)

    def _on_submit_probe(self, raw) -> None:
        if not raw or not self._submit_poll.isActive():
            return
        self._submit_poll.stop()
        self.waiting.emit(False)
        data = _parse(raw)
        number, sys_id = str(data.get("number") or ""), str(data.get("sys_id") or "")
        base = QUrl(self.form.url)
        origin = f"{base.scheme()}://{base.host()}"
        link = (f"{origin}/sp?id=ticket&table=sc_request&sys_id={sys_id}&view=sp" if sys_id
                else f"{origin}/sc_request_list.do?sysparm_query=number%3D{number}")
        self._report(submit=f"Submitted {number or '(number not read)'}", request=number, link=link, **self._current)
        self.progress.emit(self._step_text(f"submitted {number} - logging it in Azure DevOps\u2026"))
        self.submitted.emit(self.form.title, number, link)

    def _on_saved(self, status) -> None:
        self._report(save=SAVE_MESSAGES.get(status, str(status)), **self._current)
        # Give ServiceNow time to save before navigating away.
        QTimer.singleShot(SAVE_WAIT_MS, self._next)
