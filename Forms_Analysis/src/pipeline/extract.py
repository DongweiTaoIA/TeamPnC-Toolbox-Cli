"""Phase 2: extract page-1 layout and policy-page content blocks from form PDFs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF_ROOT = ROOT / "pdf"
OUT_DIR = ROOT / "output" / "extract"
ASSET_DIR = ROOT / "output" / "assets"

MARKER_RE = re.compile(r"^\s*(\d{1,2}\.|[a-z]{1,2}\)|[IVXivx]{1,5}\)|[•●▪\-–])(?=\s|$)")
XREF_RE = re.compile(
    r"Sections?\s+\d+(?:\.\d+)?|Sections?\s+[A-H]\b\.?|[Cc]lauses?\s+\d+\s*(?:–|-|to|à)\s*\d+|Page\s+1|Page\s+un"
)
DEFINITION_SEP = ("–", "-", ":")


# ---------------------------------------------------------------- data types

@dataclass
class Run:
    text: str
    bold: bool = False
    heavy: bool = False
    underline: bool = False
    size: float = 0.0


@dataclass
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    runs: list[Run]
    marker: str = ""
    marker_x: float | None = None
    full: bool = False

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    @property
    def cx(self) -> float:
        return (self.start + self.x1) / 2

    @property
    def start(self) -> float:
        return self.marker_x if self.marker_x is not None else self.x0


@dataclass
class Block:
    type: str
    runs: list[Run] = field(default_factory=list)
    level: int = 0
    marker: str = ""
    page: int = 0
    col: int = 0
    last_y0: float = 0.0
    rows: list[list[str]] = field(default_factory=list)


# ---------------------------------------------------------------- helpers

def weight(font: str, flags: int) -> tuple[bool, bool]:
    f = font.lower()
    heavy = "black" in f or "heavy" in f
    return heavy or "bold" in f or bool(flags & 16), heavy


def norm_space(s: str) -> str:
    return re.sub(r"[ \t\u00a0]+", " ", s)


def marker_style(m: str) -> str:
    if m[0].isdigit():
        return "decimal"
    if m in ("•", "●", "▪", "-", "–"):
        return "bullet"
    if re.fullmatch(r"[IVX]+\)", m):
        return "upper-roman"
    if re.fullmatch(r"[ivx]+\)", m) and m not in ("i)", "v)", "x)"):
        return "lower-roman"
    return "lower-alpha"


def merge_runs(runs: list[Run]) -> list[Run]:
    out: list[Run] = []
    for r in runs:
        if not r.text:
            continue
        if out and (out[-1].bold, out[-1].heavy, out[-1].underline) == (r.bold, r.heavy, r.underline):
            out[-1].text += r.text
        else:
            out.append(Run(r.text, r.bold, r.heavy, r.underline, r.size))
    # Keep styled runs tight: move edge spaces into the neighbouring plain text.
    for i, r in enumerate(out):
        if not (r.bold or r.underline):
            continue
        if r.text[:1].isspace() and i > 0:
            out[i - 1].text += " "
        if r.text[-1:].isspace() and i < len(out) - 1:
            out[i + 1].text = " " + out[i + 1].text
        r.text = r.text.strip()
    out = [r for r in out if r.text]
    while out and not out[0].text.strip():
        out.pop(0)
    while out and not out[-1].text.strip():
        out.pop()
    if out:
        out[0].text = out[0].text.lstrip()
        out[-1].text = out[-1].text.rstrip()
    return out


def plain(runs: list[Run]) -> str:
    return "".join(r.text for r in runs)


def underline_segments(page: pymupdf.Page) -> list[pymupdf.Rect]:
    segs = []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) < 0.6 and abs(p1.x - p2.x) > 3:
                    segs.append(pymupdf.Rect(min(p1.x, p2.x), p1.y - 0.3, max(p1.x, p2.x), p1.y + 0.3))
            elif it[0] == "re":
                r = it[1]
                if r.height < 1.5 and r.width > 3:
                    segs.append(r)
    return segs


# ---------------------------------------------------------------- page 1

def safe_on_state(w: pymupdf.Widget):
    if w.field_type not in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
        return None
    try:
        return w.on_state()
    except TypeError:  # widget without appearance states
        return None


def extract_page1(doc: pymupdf.Document, page: pymupdf.Page) -> dict:
    fields = []
    for w in page.widgets() or []:
        fields.append({
            "name": w.field_name,
            "type": w.field_type_string,
            "rect": [round(v, 1) for v in w.rect],
            "value": w.field_value if isinstance(w.field_value, (str, bool, int, float)) else str(w.field_value),
            "options": w.choice_values or None,
            "flags": w.field_flags,
            "font_size": w.text_fontsize,
            "max_len": w.text_maxlen or None,
            "on_state": safe_on_state(w),
        })

    text = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            for s in ln["spans"]:
                if not s["text"].strip():
                    continue
                bold, heavy = weight(s["font"], s["flags"])
                text.append({
                    "bbox": [round(v, 1) for v in s["bbox"]],
                    "text": s["text"],
                    "font": s["font"].split("+")[-1],
                    "size": round(s["size"], 1),
                    "bold": bold,
                    "heavy": heavy,
                    "color": f"#{s['color']:06x}",
                })

    images = []
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for img in page.get_images(full=True):
        xref = img[0]
        try:
            data = doc.extract_image(xref)
        except Exception:
            continue
        sha1 = hashlib.sha1(data["image"], usedforsecurity=False).hexdigest()
        asset = ASSET_DIR / f"{sha1}.{data['ext']}"
        if not asset.exists():
            asset.write_bytes(data["image"])
        for r in page.get_image_rects(xref):
            images.append({"bbox": [round(v, 1) for v in r], "sha1": sha1, "ext": data["ext"],
                           "width": data["width"], "height": data["height"]})

    drawings = []
    for d in page.get_drawings():
        for it in d["items"]:
            kind = {"l": "line", "re": "rect"}.get(it[0])
            if not kind:
                continue
            geom = [it[1].x, it[1].y, it[2].x, it[2].y] if kind == "line" else list(it[1])
            drawings.append({
                "type": kind,
                "geom": [round(v, 1) for v in geom],
                "stroke": d.get("color"),
                "fill": d.get("fill"),
                "width": round(d.get("width") or 0, 2),
            })

    return {"size": [page.rect.width, page.rect.height], "fields": fields, "text": text,
            "images": images, "drawings": drawings}


# ---------------------------------------------------------------- policy pages: lines

def read_lines(page: pymupdf.Page) -> list[Line]:
    segs = underline_segments(page)
    lines = []
    for b in page.get_text("rawdict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b["lines"]:
            runs = []
            for s in ln["spans"]:
                bold, heavy = weight(s["font"], s["flags"])
                for text, under in split_underlined(s["chars"], segs):
                    runs.append(Run(re.sub(r"[\t\r\n]", " ", text), bold, heavy, under, s["size"]))
            if not "".join(r.text for r in runs).strip():
                continue
            x0, y0, x1, y1 = ln["bbox"]
            lines.append(Line(page.number + 1, x0, y0, x1, y1, runs))
    return lines


def split_underlined(chars: list[dict], segs: list[pymupdf.Rect]) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for ch in chars:
        x0, _, x1, y1 = ch["bbox"]
        cx = (x0 + x1) / 2
        under = bool(segs) and ch["c"].strip() != "" and any(
            y1 - 2.5 <= s.y0 <= y1 + 2.0 and s.x0 - 0.5 <= cx <= s.x1 + 0.5 for s in segs)
        # Spaces between two underlined words stay underlined.
        if not ch["c"].strip() and out and out[-1][1]:
            under = True
        if out and out[-1][1] == under:
            out[-1] = (out[-1][0] + ch["c"], under)
        else:
            out.append((ch["c"], under))
    return out


def strip_page_furniture(pages: list[list[Line]], heights: list[float], body_size: float) -> tuple[list[list[Line]], list[str]]:
    """Drop small-font lines at the top/bottom edge (form code, page x/y)."""
    furniture = []
    out = []
    for lines, h in zip(pages, heights):
        keep = []
        for ln in lines:
            small = max(r.size for r in ln.runs) < body_size - 0.4
            edge = ln.y0 > h - 45 or ln.y1 < 30
            if small and edge:
                furniture.append(norm_space(ln.text).strip())
            else:
                keep.append(ln)
        out.append(keep)
    return out, furniture


def merge_markers(lines: list[Line]) -> list[Line]:
    """Attach a marker-only line ('1.', 'a)', '•') to the text line on the same baseline."""
    lines = sorted(lines, key=lambda l: (l.y0, l.x0))
    markers: dict[int, int] = {}
    taken: set[int] = set()
    for i, ln in enumerate(lines):
        t = ln.text.strip()
        m = MARKER_RE.match(t)
        if not (m and m.group(0).strip() == t):
            continue
        best = None
        for j, other in enumerate(lines):
            if j == i or j in taken or j in markers:
                continue
            if abs(other.y0 - ln.y0) < 2.5 and 0 <= other.x0 - ln.x1 < 25:
                if best is None or other.x0 < lines[best].x0:
                    best = j
        if best is not None:
            markers[i] = best
            taken.add(best)

    out = []
    for i, ln in enumerate(lines):
        if i in taken:
            continue
        if i in markers:
            p = lines[markers[i]]
            p.marker, p.marker_x = ln.text.strip(), ln.x0
            out.append(p)
            continue
        m = MARKER_RE.match(ln.text)
        # Marker inside the same line ('10.\t Front ...').
        if m and not ln.marker:
            ln.marker, ln.marker_x = m.group(1), ln.x0
            cut = m.end()
            for r in ln.runs:
                take = min(cut, len(r.text))
                r.text, cut = r.text[take:], cut - take
                if cut <= 0:
                    break
        out.append(ln)
    return out


# ---------------------------------------------------------------- bands and columns

def classify_full_width(lines: list[Line], cx0: float, cx1: float) -> None:
    width = cx1 - cx0
    mid = (cx0 + cx1) / 2

    def isolated(ln: Line) -> bool:
        h = ln.y1 - ln.y0
        return not any(o is not ln and min(o.y1, ln.y1) - max(o.y0, ln.y0) > 0.5 * min(h, o.y1 - o.y0)
                       for o in lines)

    for ln in lines:
        ln.full = (ln.x1 - ln.start) > 0.6 * width or (abs(ln.cx - mid) < 4 and isolated(ln))
    # Short last line of a full-width paragraph (only directly under a genuinely wide line).
    wide = [l for l in lines if (l.x1 - l.start) > 0.6 * width]
    for ln in lines:
        if ln.full:
            continue
        above = [o for o in wide if 0 < ln.y0 - o.y0 < 11 and abs(o.x0 - ln.x0) < 1.5]
        below = [o for o in lines if o is not ln and not o.full and 0 < o.y0 - ln.y0 < 11
                 and abs(o.x0 - ln.x0) < 1.5]
        if above and not below and isolated(ln):
            ln.full = True


def find_columns(lines: list[Line], cx0: float, cx1: float) -> list[tuple[float, float]]:
    lo, hi = int(cx0), int(cx1) + 1
    cover = [0] * (hi - lo + 1)
    for ln in lines:
        for x in range(int(ln.start) - lo, int(ln.x1) - lo + 1):
            if 0 <= x < len(cover):
                cover[x] += 1
    cols, start, gap = [], None, 0
    for i, c in enumerate(cover):
        if c:
            if start is None:
                start = i
            elif gap >= 5:
                cols.append((start + lo, i - gap + lo))
                start = i
            gap = 0
        elif start is not None:
            gap += 1
    if start is not None:
        cols.append((start + lo, len(cover) - 1 - gap + lo))
    return cols or [(cx0, cx1)]


def layout_page(lines: list[Line], cx0: float, cx1: float,
                prev_cols: list[tuple[float, float]] | None = None
                ) -> tuple[list[tuple[int, list[list[Line]]]], list[tuple[float, float]] | None]:
    """Return bands top-to-bottom as (columns, [lines per column]) and the last column grid."""
    classify_full_width(lines, cx0, cx1)
    full = sorted([l for l in lines if l.full], key=lambda l: l.y0)
    rest = [l for l in lines if not l.full]
    bands: list[tuple[int, list[list[Line]]]] = []
    edges = [-1e9] + [l.y0 for l in full] + [1e9]
    grid = prev_cols
    for i in range(len(edges) - 1):
        seg = [l for l in rest if edges[i] <= l.y0 < edges[i + 1]]
        if seg:
            cols = find_columns(seg, cx0, cx1)
            # A short band may only touch some columns of the running grid; keep the grid.
            if grid and len(cols) < len(grid) and all(
                    any(abs(a - g[0]) < 3 for g in grid) for a, _ in cols):
                cols = grid
            grid = cols
            per_col = [[] for _ in cols]
            idx = 0
            for l in seg:
                idx = max((k for k, (a, _) in enumerate(cols) if l.start >= a - 2), default=0)
                per_col[idx].append(l)
            bands.append((len(cols), [sorted(c, key=lambda l: l.y0) for c in per_col]))
        if i < len(full):
            bands.append((1, [[full[i]]]))
    # Merge consecutive full-width single-line bands.
    merged: list[tuple[int, list[list[Line]]]] = []
    for n, cols in bands:
        if merged and n == 1 and merged[-1][0] == 1 and all(l.full for l in cols[0]) \
                and all(l.full for l in merged[-1][1][0]):
            merged[-1][1][0].extend(cols[0])
        else:
            merged.append((n, cols))
    return merged, grid


# ---------------------------------------------------------------- blocks

class BlockBuilder:
    def __init__(self, pitch: float, indent: float, body_size: float):
        self.pitch, self.indent, self.body_size = pitch, indent, body_size
        self.blocks: list[dict] = []
        self.cur: Block | None = None
        self.prev_line: Line | None = None
        self.columns = 0
        self.in_defs = False

    def region(self, columns: int, page: int) -> None:
        if columns != self.columns:
            self.flush()
            self.blocks.append({"type": "region", "columns": columns, "page": page})
            self.columns = columns

    def flush(self) -> None:
        c = self.cur
        self.cur = None
        if not c:
            return
        if c.type == "table":
            self.blocks.append({"type": "table", "rows": c.rows, "page": c.page})
            return
        runs = merge_runs(c.runs)
        if not runs:
            return
        b: dict = {"type": c.type, "page": c.page}
        if c.type in ("heading", "item", "paragraph"):
            b["level"] = c.level
        if c.marker:
            b["marker"], b["style"] = c.marker, marker_style(c.marker)
        if c.type in ("sectionTitle", "heading"):
            self.in_defs = is_defs_title(plain(runs))
        if self.in_defs and c.type in ("paragraph", "item"):
            term, body = split_definition(runs)
            if not term:
                term, body = split_means(runs)
            if term:
                b["type"], b["term"] = "definition", [run_dict(r) for r in term]
                runs = body
        if b["type"] in ("item", "paragraph"):
            lead, rest = split_run_in(runs)
            if lead:
                b["runIn"] = [run_dict(r) for r in lead]
                runs = rest
        b["runs"] = [run_dict(r) for r in runs]
        refs = XREF_RE.findall(plain(runs))
        if refs:
            b["xrefs"] = refs
        self.blocks.append(b)

    def add_table_row(self, cells: list[Line], col: int) -> None:
        if not (self.cur and self.cur.type == "table" and self.cur.col == col):
            self.flush()
            self.cur = Block("table", page=cells[0].page, col=col)
            self.cur.header_x = [c.x0 for c in cells]  # type: ignore[attr-defined]
        hx: list[float] = self.cur.header_x  # type: ignore[attr-defined]
        row = [""] * len(hx)
        for c in cells:
            k = max((i for i, x in enumerate(hx) if c.x0 >= x - 2), default=0)
            row[k] = norm_space((row[k] + " " + c.text)).strip()
        self.cur.rows.append(row)
        self.cur.last_y0 = cells[0].y0

    def add_line(self, ln: Line, col_x: float, col: int, first_in_col: bool, full: bool) -> None:
        styled = [r for r in ln.runs if r.text.strip()]
        txt = ln.text.strip()
        heavy = all(r.heavy for r in styled)
        caps = all(r.bold for r in styled) and is_caps(txt)
        level = round(((ln.marker_x if ln.marker else ln.x0) - col_x) / self.indent)
        if full:
            kind = "sectionTitle" if (heavy or caps) and txt == txt.upper() else "lead"
        elif heavy or (caps and not ln.marker and len(txt) < 90):
            kind = "heading"
        elif ln.marker:
            kind = "item"
        else:
            kind = "paragraph"

        c = self.cur
        if (kind == "heading" and not heavy and c is not None and c.type in ("item", "paragraph")
                and c.col == col and c.page == ln.page and ln.y0 - c.last_y0 <= self.pitch + 1.0):
            kind = c.type
        start_new = (
            c is None
            or bool(ln.marker)
            or c.type == "table"
            or (kind in ("sectionTitle", "heading")) != (c.type in ("sectionTitle", "heading"))
            or (kind == "lead") != (c.type == "lead")
        )
        if not start_new and c is not None:
            if first_in_col or c.col != col or c.page != ln.page:
                start_new = plain(c.runs).rstrip().endswith((".", ":", ";"))
            elif ln.y0 - c.last_y0 > self.pitch + 1.0:
                start_new = True
            elif c.type == "paragraph" and kind == "paragraph" and level != c.level and not c.marker:
                start_new = True
            elif kind == "paragraph" and level == 0 and self.defs_active() and styled and styled[0].bold \
                    and re.search(r"\s[–-]\s", txt[:100]):
                # Dense definition lists have no paragraph gap; each '**Term** –' line starts a new entry.
                start_new = True

        if start_new:
            self.flush()
            self.cur = Block(kind, level=max(level, 0), marker=ln.marker, page=ln.page, col=col)
            self.cur.runs.extend(ln.runs)
        else:
            joiner = "" if plain(c.runs).endswith(("-", "/", "–")) else " "
            last = c.runs[-1] if c.runs else Run("")
            c.runs.append(Run(joiner, last.bold, last.heavy, last.underline, last.size))
            c.runs.extend(ln.runs)
            c.page, c.col = ln.page, col
        self.cur.last_y0 = ln.y0

    def defs_active(self) -> bool:
        c = self.cur
        if c is not None and c.type in ("sectionTitle", "heading"):
            return is_defs_title(plain(c.runs))
        return self.in_defs


def is_defs_title(text: str) -> bool:
    t = text.upper()
    return "DEFINITION" in t or "DÉFINITION" in t


def split_means(runs: list[Run]) -> tuple[list[Run], list[Run]]:
    """'**“Accidental Death”** means …' (certificate-style definitions)."""
    if not runs or not runs[0].bold:
        return [], runs
    m = re.match(r"\s*[“\"«]\s*([^”\"»]{2,80}?)\s*[”\"»]", runs[0].text)
    rest = plain(runs[1:]).lstrip().lower()
    if not m or not rest.startswith(("means", "désigne", "signifie", "s’entend", "or “", "ou «")):
        return [], runs
    return [Run(m.group(1), True)], runs[1:]


def is_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 3 and sum(c.isupper() for c in letters) / len(letters) > 0.8


def run_dict(r: Run) -> dict:
    d: dict = {"text": norm_space(r.text)}
    if r.bold:
        d["b"] = True
    if r.underline:
        d["u"] = True
    if r.heavy:
        d["heavy"] = True
    return d


def split_run_in(runs: list[Run]) -> tuple[list[Run], list[Run]]:
    """'Hybrid Electric:' or 'Per Repair Visit –' bold lead at the start of an item."""
    lead, i = [], 0
    while i < len(runs) and runs[i].bold:
        lead.append(runs[i])
        i += 1
    text = plain(lead).strip()
    if not lead or len(text) > 90:
        return [], runs
    rest = runs[i:]
    if not plain(rest).strip():
        return [], runs
    if text.endswith(":"):
        return lead, rest
    nxt = plain(rest).lstrip()
    if nxt.startswith(("–", "- ")) and len(text) < 60:
        rest = [Run(r.text, r.bold, r.heavy, r.underline, r.size) for r in rest]
        rest[0].text = rest[0].text.lstrip().lstrip("–-").lstrip()
        return lead, rest
    return [], runs


def split_definition(runs: list[Run]) -> tuple[list[Run], list[Run]]:
    """'**Term** – body' (terms may contain ' and ' / ', ' between bold runs)."""
    text = plain(runs)
    m = re.search(r"\s[–-]\s", text)
    if not m or m.start() > 80 or not runs[0].bold:
        return [], runs
    cut, term, body = m.start(), [], []
    pos = 0
    for r in runs:
        end = pos + len(r.text)
        if end <= cut:
            term.append(r)
        elif pos >= m.end():
            body.append(r)
        else:
            if pos < cut:
                term.append(Run(r.text[: cut - pos], r.bold, r.heavy, r.underline, r.size))
            if end > m.end():
                body.append(Run(r.text[max(m.end() - pos, 0):], r.bold, r.heavy, r.underline, r.size))
        pos = end
    return merge_runs(term), merge_runs(body)


# ---------------------------------------------------------------- defined terms

def mark_defined_terms(blocks: list[dict]) -> list[str]:
    terms: set[str] = set()
    for b in blocks:
        if b["type"] == "definition":
            for r in b["term"]:
                if r.get("b"):
                    for t in re.split(r",|\bet\b|\band\b", r["text"]):
                        t = re.sub(r"[()]", "", t).strip(" .").lower()
                        if t:
                            terms.add(t)
    if not terms:
        return []
    words_terms = sorted((t.split() for t in terms), key=len, reverse=True)

    def covered(text: str) -> bool:
        words = re.sub(r"[’']s\b|[.,;:()’']", " ", text.lower()).split()
        i = 0
        while i < len(words):
            for t in words_terms:
                if words[i:i + len(t)] == t or (len(t) == len(words[i:i + len(t)])
                                                 and words[i:i + len(t)][:-1] == t[:-1]
                                                 and words[i + len(t) - 1] in (t[-1] + "s", t[-1] + "x")):
                    i += len(t)
                    break
            else:
                return False
        return bool(words)

    for b in blocks:
        if b["type"] in ("sectionTitle", "heading"):
            continue
        for key in ("runs", "runIn"):
            for r in b.get(key, []):
                if r.get("b") and not r.get("heavy") and covered(r["text"]):
                    r["term"] = True
    return sorted(terms)


# ---------------------------------------------------------------- document

def extract_policy(doc: pymupdf.Document, first: int) -> dict:
    pages = [read_lines(doc[i]) for i in range(first, doc.page_count)]
    all_runs = [r for p in pages for l in p for r in l.runs if r.text.strip()]
    if not all_runs:
        return {"blocks": [], "terms": [], "furniture": [], "stats": {}}
    sizes = Counter()
    for r in all_runs:
        sizes[round(r.size, 1)] += len(r.text)
    body_size = sizes.most_common(1)[0][0]
    pages, furniture = strip_page_furniture(pages, [doc[i].rect.height for i in range(first, doc.page_count)],
                                            body_size)
    pages = [merge_markers(p) for p in pages]

    # Typical line pitch and marker-to-text indent step.
    gaps = []
    for p in pages:
        by_x = sorted(p, key=lambda l: (round(l.x0), l.y0))
        for a, b in zip(by_x, by_x[1:]):
            if abs(a.x0 - b.x0) < 1 and 0 < b.y0 - a.y0 < 20:
                gaps.append(b.y0 - a.y0)
    pitch = statistics.median(gaps) if gaps else body_size * 1.15
    diffs = [l.x0 - l.marker_x for p in pages for l in p
             if l.marker and l.marker_x is not None and l.x0 - l.marker_x > 3]
    indent = statistics.median(diffs) if diffs else 13.5

    builder = BlockBuilder(pitch, indent, body_size)
    page_cols = []
    grid = None
    for p in pages:
        if not p:
            continue
        cx0, cx1 = min(l.start for l in p), max(l.x1 for l in p)
        bands, grid = layout_page(p, cx0, cx1, grid)
        page_cols.append([n for n, _ in bands])
        for n, cols in bands:
            builder.region(n, cols[0][0].page if cols and cols[0] else 0)
            col_x = None
            for ci, col in enumerate(cols):
                if not col:
                    continue
                col_x = min(l.start for l in col)
                rows = group_rows(col)
                for k, row in enumerate(rows):
                    if len(row) > 1 and not any(l.full for l in row):
                        builder.add_table_row(row, ci)
                    else:
                        builder.add_line(row[0], col_x, ci, k == 0, row[0].full)
    builder.flush()
    blocks = builder.blocks
    terms = mark_defined_terms(blocks)

    def nonspace(s: str) -> int:
        return len(re.sub(r"\s", "", s))

    src_chars = sum(nonspace(doc[i].get_text()) for i in range(first, doc.page_count)) \
        - sum(nonspace(f) for f in furniture)
    out_chars = 0
    for b in blocks:
        for key in ("runs", "runIn", "term"):
            out_chars += sum(nonspace(r["text"]) for r in b.get(key, []))
        out_chars += nonspace(b.get("marker", ""))
        out_chars += sum(nonspace(c) for row in b.get("rows", []) for c in row)
    stats = {
        "body_size": body_size,
        "pitch": round(pitch, 2),
        "indent": round(indent, 2),
        "page_bands": page_cols,
        "src_chars": src_chars,
        "out_chars": out_chars,
        "coverage": round(out_chars / max(src_chars, 1), 4),
        "blocks": Counter(b["type"] for b in blocks),
        "underlined_runs": sum(1 for b in blocks for r in b.get("runs", []) if r.get("u")),
        "term_runs": sum(1 for b in blocks for k in ("runs", "runIn") for r in b.get(k, []) if r.get("term")),
    }
    return {"blocks": blocks, "terms": terms, "furniture": sorted(set(furniture)), "stats": stats}


def group_rows(col: list[Line]) -> list[list[Line]]:
    rows: list[list[Line]] = []
    for l in sorted(col, key=lambda l: (l.y0, l.x0)):
        if rows and abs(rows[-1][0].y0 - l.y0) < 1.5:
            rows[-1].append(l)
        else:
            rows.append([l])
    return [sorted(r, key=lambda l: l.x0) for r in rows]


def extract_file(path: Path, product: str) -> dict:
    with pymupdf.open(path) as doc:
        first_has_fields = bool(list(doc[0].widgets() or []))
        result = {
            "product": product,
            "file": path.name,
            "pages": doc.page_count,
            "page1": extract_page1(doc, doc[0]) if first_has_fields else None,
        }
        result["policy"] = extract_policy(doc, 1 if first_has_fields else 0)
    return result


def in_scope_files(inventory: Path) -> list[str]:
    with inventory.open(encoding="utf-8-sig") as fh:
        return [f"{r['product']}/{r['file_name']}" for r in csv.DictReader(fh) if r["in_scope"] == "True"]


SUMMARY_FIELDS = ["product", "file", "pages", "fields", "blocks", "coverage", "bands", "terms",
                  "term_runs", "underlined", "tables", "error"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="*", help="'<product>/<file>.pdf' under --root (default: all in-scope files)")
    ap.add_argument("--root", default=str(DEFAULT_PDF_ROOT), help="folder with one sub-folder per product")
    ap.add_argument("--inventory", default=str(ROOT / "output" / "inventory.csv"))
    args = ap.parse_args()

    root = Path(args.root)
    names = args.files or in_scope_files(Path(args.inventory))
    summary = []
    for name in names:
        product, _, file_name = name.replace("\\", "/").rpartition("/")
        try:
            res = extract_file(root / product / file_name, product)
        except Exception as ex:  # keep batch going; report at the end
            summary.append({"product": product, "file": file_name, "error": f"{type(ex).__name__}: {ex}"})
            continue
        out = OUT_DIR / product / (Path(file_name).stem + ".json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=dict), encoding="utf-8")
        st = res["policy"]["stats"]
        summary.append({
            "product": product,
            "file": file_name,
            "pages": res["pages"],
            "fields": len(res["page1"]["fields"]) if res["page1"] else 0,
            "blocks": sum(st.get("blocks", {}).values()) if st else 0,
            "coverage": st.get("coverage", ""),
            "bands": st.get("page_bands", ""),
            "terms": len(res["policy"]["terms"]),
            "term_runs": st.get("term_runs", ""),
            "underlined": st.get("underlined_runs", ""),
            "tables": st.get("blocks", {}).get("table", 0) if st else 0,
            "error": "",
        })
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "_summary.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS, restval="")
        w.writeheader()
        w.writerows(summary)

    if len(summary) <= 5:
        for s in summary:
            print(s)
        return 0
    print(f"{'product':8} {'files':>5} {'policy':>6} {'err':>3} {'cov min':>7} {'cov med':>7} {'terms med':>9}")
    for product in sorted({s["product"] for s in summary}):
        ps = [s for s in summary if s["product"] == product]
        cov = [s["coverage"] for s in ps if isinstance(s.get("coverage"), float)]
        terms = [s["terms"] for s in ps if s.get("blocks")]
        print(f"{product:8} {len(ps):5} {sum(bool(s.get('blocks')) for s in ps):6} "
              f"{sum(bool(s.get('error')) for s in ps):3} "
              f"{(min(cov) if cov else 0):7.3f} {(statistics.median(cov) if cov else 0):7.3f} "
              f"{(statistics.median(terms) if terms else 0):9}")
    for s in summary:
        if s.get("error"):
            print("ERROR", s["product"], s["file"], s["error"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
