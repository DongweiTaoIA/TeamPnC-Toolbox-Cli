"""Phase 1: inventory of form PDFs (file-name metadata + basic PDF facts)."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import pymupdf

DEFAULT_ROOT = r"C:\repos\tfs\DP_Unifi_Resources\Dev\_ACCP\Forms\Src"
OUTPUT_DIR = Path(__file__).resolve().parents[2] / "output"

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

# Prefix typos / merged variants that belong to the Contract family.
CONTRACT_ALIASES = {"CONTRACT", "CONTRAC", "CONTACT", "MRGCONTRACT"}

BODY_RE = re.compile(
    r"^(?P<series>[A-Z]{1,2})?(?P<num>\d{2,4})"
    r"(?P<variant>.*?)"
    r"(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    r"(?P<year>\d{4}|\d{2})"
    r"(?P<rest>.*)$"
)

PAGE_SIZES = {(612, 792): "Letter", (612, 1008): "Legal", (792, 1224): "Tabloid", (595, 842): "A4"}
SCANNED_MIN_CHARS = 50
SCOPE_MIN_YEAR = 2019


@dataclass
class Row:
    product: str
    file_name: str
    prefix: str = ""
    family: str = ""
    form_no: str = ""
    variant: str = ""
    variant_raw: str = ""
    month: int | None = None
    year: int | None = None
    rev_date: str = ""
    lang: str = ""
    group_key: str = ""
    is_latest: bool = False
    in_scope: bool = False
    parse_ok: bool = False
    notes: list[str] = field(default_factory=list)
    # PDF facts
    pages: int | None = None
    page_size: str = ""
    is_fillable: bool = False
    field_count: int = 0
    field_pages: str = ""
    text_chars_p1: int = 0
    text_chars_rest: int = 0
    scanned_pages: str = ""
    fonts: str = ""
    size_kb: int = 0
    error: str = ""


def parse_name(path: Path, product: str) -> Row:
    row = Row(product=product, file_name=path.name)
    stem = path.stem.replace(" ", "")
    prefix, sep, body = stem.partition("_")
    if not sep or not re.fullmatch(r"[A-Za-z0-9]+", prefix) or prefix[0].isdigit():
        # 'Contract184AOCT2019E' has no separator between prefix and form number.
        m = re.match(r"([A-Za-z]{3,}?)(?=[A-Z]{0,2}\d)(.+)$", stem)
        prefix, body = (m.group(1), m.group(2)) if m and m.group(1).upper() in CONTRACT_ALIASES else ("", stem)
    row.prefix = prefix
    up = prefix.upper()
    row.family = "Contract" if up in CONTRACT_ALIASES else (prefix.upper() if prefix else "")
    if up in CONTRACT_ALIASES and up != "CONTRACT":
        row.notes.append(f"prefix '{prefix}' treated as Contract")

    m = BODY_RE.match(body.upper())
    if not m:
        row.notes.append("name not parsed")
        return row

    year = int(m["year"])
    if year < 100:
        year += 2000
    row.form_no = (m["series"] or "") + m["num"]
    row.variant_raw = m["variant"]
    row.month, row.year = MONTHS[m["mon"]], year
    row.rev_date = date(year, row.month, 1).isoformat()

    rest = m["rest"].rstrip("-_ ")
    if rest[-1:] in ("E", "F"):
        row.lang = rest[-1]
        if len(rest) > 1:
            row.notes.append(f"extra text after date: '{m['rest']}'")
    else:
        if rest:
            row.notes.append(f"extra text after date: '{m['rest']}'")
        row.notes.append("no language suffix")
    if m["rest"] != rest:
        row.notes.append(f"trailing chars stripped: '{m['rest']}'")

    # A lone E/F before the date is a language marker, not a product variant.
    row.variant = "" if row.variant_raw in ("E", "F") else row.variant_raw
    if row.variant_raw in ("E", "F") and row.lang and row.variant_raw != row.lang:
        row.notes.append(f"variant '{row.variant_raw}' disagrees with language '{row.lang}'")

    row.group_key = "|".join([row.product, row.family, row.form_no, row.variant, row.lang or "?"])
    row.parse_ok = True
    return row


def page_size_name(rect: pymupdf.Rect) -> str:
    w, h = round(rect.width), round(rect.height)
    key = (min(w, h), max(w, h))
    name = PAGE_SIZES.get(key, f"{w}x{h}")
    return name + (" (landscape)" if w > h else "")


def inspect_pdf(path: Path, row: Row) -> None:
    row.size_kb = round(path.stat().st_size / 1024)
    try:
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                row.error = "password protected"
                return
            row.pages = doc.page_count
            row.page_size = page_size_name(doc[0].rect) if doc.page_count else ""
            fonts: set[str] = set()
            field_pages, scanned = [], []
            for page in doc:
                widgets = list(page.widgets() or [])
                if widgets:
                    row.field_count += len(widgets)
                    field_pages.append(str(page.number + 1))
                chars = len(page.get_text("text").strip())
                if page.number == 0:
                    row.text_chars_p1 = chars
                else:
                    row.text_chars_rest += chars
                if chars < SCANNED_MIN_CHARS and page.get_images():
                    scanned.append(str(page.number + 1))
                for f in page.get_fonts():
                    # Drop subset tag, e.g. "ABCDEF+MyriadPro-Cond" -> "MyriadPro-Cond".
                    fonts.add(f[3].split("+", 1)[-1])
            row.is_fillable = row.field_count > 0
            row.field_pages = ",".join(field_pages)
            row.scanned_pages = ",".join(scanned)
            row.fonts = ";".join(sorted(fonts))
    except Exception as ex:  # corrupt or unsupported file; keep going
        row.error = f"{type(ex).__name__}: {ex}"


def mark_latest(rows: list[Row]) -> None:
    groups: dict[str, list[Row]] = {}
    for r in rows:
        if r.parse_ok:
            groups.setdefault(r.group_key, []).append(r)
    for members in groups.values():
        # Newest revision wins; prefer canonical 'Contract_' prefix on ties.
        members.sort(key=lambda r: (r.rev_date, r.prefix.upper() == r.family.upper()), reverse=True)
        members[0].is_latest = True
        top = members[0]
        for other in members[1:]:
            if other.rev_date == top.rev_date:
                other.notes.append(f"duplicate of {top.file_name}")


def mark_scope(rows: list[Row]) -> None:
    """Option A: current forms only (latest, 2019+, not merged copies, not the SI2 generation)."""
    for r in rows:
        r.in_scope = (
            r.is_latest
            and not r.error
            and (r.year or 0) >= SCOPE_MIN_YEAR
            and r.prefix.upper() != "MRGCONTRACT"
            and "SI2" not in r.variant
        )


def write_csv(rows: list[Row], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(rows[0]).keys())
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            d = asdict(r)
            d["notes"] = "; ".join(r.notes)
            w.writerow(d)


def summarize(rows: list[Row], skipped: list[str]) -> None:
    print(f"PDF files: {len(rows)}   skipped non-PDF: {len(skipped)} {skipped}")
    print(f"{'product':8} {'pdfs':>5} {'parsed':>6} {'latest':>6} {'scope':>5} {'fill':>5} {'scan':>4} {'err':>3}  "
          "scope families / page counts")
    for product in sorted({r.product for r in rows}):
        pr = [r for r in rows if r.product == product]
        sc = [r for r in pr if r.in_scope]
        fam = dict(Counter(r.family or "-" for r in sc).most_common(4))
        pages = dict(sorted(Counter(r.pages for r in sc if r.pages).items()))
        print(f"{product:8} {len(pr):5} {sum(r.parse_ok for r in pr):6} {sum(r.is_latest for r in pr):6} "
              f"{len(sc):5} {sum(r.is_fillable for r in sc):5} {sum(bool(r.scanned_pages) for r in sc):4} "
              f"{sum(bool(r.error) for r in pr):3}  {fam} {pages}")
    scope = [r for r in rows if r.in_scope]
    print(f"\nIn scope total: {len(scope)}")
    fonts = Counter(f for r in scope for f in r.fonts.split(";") if f)
    print("Top fonts (in scope):", dict(fonts.most_common(12)))
    unparsed = [f"{r.product}/{r.file_name}" for r in rows if not r.parse_ok]
    print(f"\nUnparsed names ({len(unparsed)}):", unparsed[:60])
    errs = [f"{r.product}/{r.file_name}: {r.error}" for r in rows if r.error]
    if errs:
        print("Errors:", errs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=os.environ.get("FORMS_ROOT", DEFAULT_ROOT),
                    help="folder that contains one sub-folder per product")
    ap.add_argument("--products", nargs="*", help="limit to these product folders")
    ap.add_argument("--out", default=str(OUTPUT_DIR / "inventory.csv"))
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"Root folder not found: {root}", file=sys.stderr)
        return 1

    rows, skipped = [], []
    for folder in sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name.lower()):
        if args.products and folder.name not in args.products:
            continue
        for p in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_file():
                continue
            if p.suffix.lower() != ".pdf":
                skipped.append(f"{folder.name}/{p.name}")
                continue
            row = parse_name(p, folder.name)
            inspect_pdf(p, row)
            rows.append(row)

    mark_latest(rows)
    mark_scope(rows)
    write_csv(rows, Path(args.out))
    summarize(rows, skipped)
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
