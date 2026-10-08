"""Load inventory + extraction output into SQL Server (schema kb). Full reload, re-runnable."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

import pyodbc

ROOT = Path(__file__).resolve().parents[2]
EXTRACT_DIR = ROOT / "output" / "extract"
ASSET_DIR = ROOT / "output" / "assets"
SCHEMA_SQL = ROOT / "src" / "db" / "kb_schema.sql"
BATCH = 2000


def load_env() -> dict[str, str]:
    """KB_SQL_* settings from .env (git-ignored), overridden by real environment variables."""
    cfg: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    cfg.update({k: v for k, v in os.environ.items() if k.startswith("KB_SQL_")})
    return cfg


def conn_str(cfg: dict[str, str], database: str) -> str:
    server = cfg.get("KB_SQL_SERVER", "localhost,1433")
    trust = cfg.get("KB_SQL_TRUST_CERT", "yes" if server.lower().startswith(("localhost", "127.0.0.1")) else "no")
    parts = [
        f"DRIVER={{{cfg.get('KB_SQL_DRIVER', 'ODBC Driver 18 for SQL Server')}}}",
        f"SERVER={server}",
        f"DATABASE={database}",
        "Encrypt=yes",
        f"TrustServerCertificate={trust}",
    ]
    if cfg.get("KB_SQL_USER"):
        parts += [f"UID={cfg['KB_SQL_USER']}", f"PWD={{{cfg.get('KB_SQL_PASSWORD', '')}}}"]
    else:
        parts.append("Trusted_Connection=yes")
    return ";".join(parts)


def ensure_database(cfg: dict[str, str], database: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_]+", database):
        raise ValueError(f"Invalid database name: {database!r}")
    with pyodbc.connect(conn_str(cfg, "master"), autocommit=True) as cn:
        cn.execute(f"IF DB_ID(N'{database}') IS NULL CREATE DATABASE [{database}]")


def run_script(cn: pyodbc.Connection, path: Path) -> None:
    for batch in re.split(r"^\s*GO\s*$", path.read_text(encoding="utf-8"), flags=re.MULTILINE | re.IGNORECASE):
        if batch.strip():
            cn.execute(batch)
    cn.commit()


def b(v: str) -> int:
    return 1 if v == "True" else 0


def n(v: str):
    return v if v not in ("", None) else None


def jdump(v) -> str | None:
    return json.dumps(v, ensure_ascii=False) if v not in (None, [], {}) else None


def plain(runs: list[dict] | None) -> str:
    return "".join(r["text"] for r in runs or [])


def executemany(cur: pyodbc.Cursor, sql: str, rows: list[tuple]) -> None:
    for i in range(0, len(rows), BATCH):
        cur.executemany(sql, rows[i:i + BATCH])


def load(cn: pyodbc.Connection, inventory: Path) -> None:
    cur = cn.cursor()
    cur.fast_executemany = True
    cur.execute("DELETE FROM kb.Document")

    with inventory.open(encoding="utf-8-sig") as fh:
        inv = list(csv.DictReader(fh))
    executemany(cur, """
        INSERT INTO kb.Document (Product, FileName, Prefix, Family, FormNo, Variant, VariantRaw, RevDate, Lang,
            GroupKey, IsLatest, InScope, ParseOk, PageCount, PageSize, IsFillable, FieldCount, FieldPages, Fonts,
            SizeKb, Notes, Error)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
        (r["product"], r["file_name"], n(r["prefix"]), n(r["family"]), n(r["form_no"]), n(r["variant"]),
         n(r["variant_raw"]), n(r["rev_date"]), n(r["lang"]), n(r["group_key"]), b(r["is_latest"]),
         b(r["in_scope"]), b(r["parse_ok"]), n(r["pages"]), n(r["page_size"]), b(r["is_fillable"]),
         int(r["field_count"] or 0), n(r["field_pages"]), n(r["fonts"])[:2000] if r["fonts"] else None,
         n(r["size_kb"]), n(r["notes"]), n(r["error"]))
        for r in inv])
    ids = {(p, f): i for i, p, f in cur.execute("SELECT DocumentId, Product, FileName FROM kb.Document").fetchall()}
    print(f"documents: {len(ids)}")

    known_assets = {r[0] for r in cur.execute("SELECT Sha1 FROM kb.Asset").fetchall()}
    upd, layouts, fields, doc_assets, new_assets, blocks, terms = [], [], [], [], {}, [], []
    for path in sorted(EXTRACT_DIR.glob("*/*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        doc_id = ids.get((data["product"], data["file"]))
        if doc_id is None:
            continue
        pol = data["policy"]
        st = pol.get("stats") or {}
        upd.append((st.get("body_size"), st.get("pitch"), st.get("indent"), st.get("coverage"),
                    jdump(st.get("page_bands")), jdump(pol.get("furniture")), doc_id))

        p1 = data.get("page1")
        if p1:
            layouts.append((doc_id, p1["size"][0], p1["size"][1],
                            json.dumps(p1["text"], ensure_ascii=False), json.dumps(p1["drawings"])))
            for k, f in enumerate(p1["fields"]):
                x0, y0, x1, y1 = f["rect"]
                val = f.get("value")
                fields.append((doc_id, k, f["name"], f["type"], x0, y0, x1, y1,
                               None if val in (None, "") else str(val)[:1000], jdump(f.get("options")),
                               f.get("flags"), f.get("font_size"), f.get("max_len"),
                               None if f.get("on_state") is None else str(f["on_state"])))
            for k, img in enumerate(p1["images"]):
                if img["sha1"] not in known_assets and img["sha1"] not in new_assets:
                    file = ASSET_DIR / f"{img['sha1']}.{img['ext']}"
                    if file.exists():
                        new_assets[img["sha1"]] = (img["sha1"], img["ext"], img.get("width"), img.get("height"),
                                                   file.read_bytes())
                x0, y0, x1, y1 = img["bbox"]
                doc_assets.append((doc_id, k, img["sha1"], x0, y0, x1, y1))

        columns = None
        for k, blk in enumerate(pol["blocks"]):
            t = blk["type"]
            if t == "region":
                columns = blk["columns"]
            if t == "table":
                text = "\n".join(" | ".join(row) for row in blk.get("rows", []))
            else:
                text = " ".join(s for s in (plain(blk.get("term")), plain(blk.get("runIn")), plain(blk.get("runs")))
                                if s)
            blocks.append((doc_id, k, t, blk.get("page"), columns, blk.get("level"), blk.get("marker"),
                           blk.get("style"), jdump(blk.get("term")), jdump(blk.get("runIn")), jdump(blk.get("runs")),
                           jdump(blk.get("rows")), jdump(blk.get("xrefs")), text or None))
        terms.extend((doc_id, t[:200]) for t in dict.fromkeys(pol.get("terms", [])))

    executemany(cur, """
        UPDATE kb.Document SET IsExtracted = 1, BodyFontSize = ?, LinePitch = ?, IndentStep = ?, Coverage = ?,
            PageBands = ?, Furniture = ? WHERE DocumentId = ?""", upd)
    if new_assets:
        executemany(cur, "INSERT INTO kb.Asset (Sha1, Ext, Width, Height, Bytes) VALUES (?,?,?,?,?)",
                    list(new_assets.values()))
    executemany(cur, """
        INSERT INTO kb.PageLayout (DocumentId, PageWidth, PageHeight, TextJson, DrawingJson)
        VALUES (?,?,?,?,?)""", layouts)
    executemany(cur, """
        INSERT INTO kb.FormField (DocumentId, Seq, FieldName, FieldType, X0, Y0, X1, Y1, DefaultValue, Options,
            Flags, FontSize, MaxLen, OnState) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", fields)
    executemany(cur, """
        INSERT INTO kb.DocumentAsset (DocumentId, Seq, Sha1, X0, Y0, X1, Y1) VALUES (?,?,?,?,?,?,?)""",
                doc_assets)
    executemany(cur, """
        INSERT INTO kb.Block (DocumentId, Seq, BlockType, PageNo, Columns, Level, Marker, MarkerStyle, TermJson,
            RunInJson, RunsJson, RowsJson, Xrefs, PlainText) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", blocks)
    executemany(cur, "INSERT INTO kb.DefinedTerm (DocumentId, Term) VALUES (?,?)", terms)
    cn.commit()
    print(f"extracted: {len(upd)}  layouts: {len(layouts)}  fields: {len(fields)}  "
          f"assets new: {len(new_assets)}  placements: {len(doc_assets)}  blocks: {len(blocks)}  terms: {len(terms)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inventory", default=str(ROOT / "output" / "inventory.csv"))
    args = ap.parse_args()

    cfg = load_env()
    database = cfg.get("KB_SQL_DATABASE", "FormsKB")
    if cfg.get("KB_SQL_USER") and not cfg.get("KB_SQL_PASSWORD"):
        print("KB_SQL_PASSWORD is not set. Add it to .env (see .env.example).", file=sys.stderr)
        return 1
    ensure_database(cfg, database)
    with pyodbc.connect(conn_str(cfg, database)) as cn:
        run_script(cn, SCHEMA_SQL)
        load(cn, Path(args.inventory))
    return 0


if __name__ == "__main__":
    sys.exit(main())
