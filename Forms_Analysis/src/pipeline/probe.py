"""Dump raw PDF structure (lines, fonts, drawings, widgets) for inspection."""

import sys
from collections import Counter
from pathlib import Path

import pymupdf

SRC = Path(r"C:\repos\tfs\DP_Unifi_Resources\Dev\_ACCP\Forms\Src\EW")


def main(name: str, out: Path) -> None:
    doc = pymupdf.open(SRC / name)
    with out.open("w", encoding="utf-8") as fh:
        for page in doc:
            fh.write(f"\n===== PAGE {page.number + 1} {page.rect} rot={page.rotation}\n")
            d = page.get_text("dict")
            styles = Counter()
            for b in d["blocks"]:
                if b["type"] != 0:
                    fh.write(f"[IMG] {tuple(round(v) for v in b['bbox'])}\n")
                    continue
                for ln in b["lines"]:
                    parts = []
                    for s in ln["spans"]:
                        styles[(s["font"], round(s["size"], 1))] += len(s["text"])
                        bold = "B" if s["flags"] & 16 else ""
                        parts.append(f"<{s['font'].split('+')[-1]}|{s['size']:.1f}{bold}>{s['text']}")
                    x0, y0, x1, y1 = ln["bbox"]
                    fh.write(f"{x0:6.1f} {y0:6.1f} {x1:6.1f} | {''.join(parts)}\n")
            fh.write("-- styles (font,size)->chars: " + str(styles.most_common(12)) + "\n")
            lines = [dr for dr in page.get_drawings()
                     for it in dr["items"] if it[0] in ("l", "re")]
            fh.write(f"-- drawings with line/rect items: {len(lines)}\n")
            for w in page.widgets() or []:
                fh.write(f"[FIELD] {w.field_type_string:10} {w.field_name!r} {tuple(round(v) for v in w.rect)}\n")


if __name__ == "__main__":
    name = sys.argv[1]
    out = Path(__file__).resolve().parents[2] / "output" / "probe" / (Path(name).stem + ".txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    main(name, out)
    print(out)
