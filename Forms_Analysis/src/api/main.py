"""Read-only HTTP API over the FormsKB knowledge base (kb schema)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

import httpx
import pymupdf
import pyodbc
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse

ROOT = Path(__file__).resolve().parents[2]
PDF_ROOT = (ROOT / "pdf").resolve()
sys.path.insert(0, str(ROOT / "src" / "pipeline"))
import load_sql  # noqa: E402
import chat  # noqa: E402

CFG = load_sql.load_env()
CONN_STR = load_sql.conn_str(CFG, CFG.get("KB_SQL_DATABASE", "FormsKB"))
LLM_MODEL = CFG.get("KB_LLM_MODEL", "llama3.2:3b")
chat.OLLAMA_URL = CFG.get("KB_OLLAMA_URL", chat.OLLAMA_URL)

app = FastAPI(title="Forms KB API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5175", "http://127.0.0.1:5175"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


def query(sql: str, *params) -> list[dict]:
    with pyodbc.connect(CONN_STR) as cn:
        cur = cn.execute(sql, *params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def one(sql: str, *params) -> dict:
    rows = query(sql, *params)
    if not rows:
        raise HTTPException(status_code=404, detail="Not found")
    return rows[0]


def parse_json(value: str | None):
    return json.loads(value) if value else None


# ---------------------------------------------------------------- dashboard

@app.get("/api/stats")
def stats() -> dict:
    products = query("""
        SELECT Product AS product, COUNT(*) AS documents,
               SUM(CAST(InScope AS INT)) AS inScope, SUM(CAST(IsExtracted AS INT)) AS extracted,
               SUM(CASE WHEN IsExtracted = 1 AND IsFillable = 1 THEN 1 ELSE 0 END) AS fillable
        FROM kb.Document GROUP BY Product ORDER BY Product""")
    totals = one("""
        SELECT (SELECT COUNT(*) FROM kb.Document) AS documents,
               (SELECT COUNT(*) FROM kb.Document WHERE IsExtracted = 1) AS extracted,
               (SELECT COUNT(*) FROM kb.Block WHERE BlockType <> 'region') AS blocks,
               (SELECT COUNT(DISTINCT TextHash) FROM kb.Block
                 WHERE BlockType IN ('item', 'paragraph', 'definition', 'lead')) AS distinctTexts,
               (SELECT COUNT(*) FROM kb.FormField) AS fields,
               (SELECT COUNT(DISTINCT FieldName) FROM kb.FormField) AS distinctFields,
               (SELECT COUNT(*) FROM kb.DefinedTerm) AS terms,
               (SELECT COUNT(*) FROM kb.Asset) AS assets""")
    block_types = query("""
        SELECT BlockType AS type, COUNT(*) AS count FROM kb.Block
        WHERE BlockType <> 'region' GROUP BY BlockType ORDER BY count DESC""")
    languages = query("""
        SELECT ISNULL(Lang, '?') AS lang, COUNT(*) AS count FROM kb.Document
        WHERE IsExtracted = 1 GROUP BY Lang""")
    return {"totals": totals, "products": products, "blockTypes": block_types, "languages": languages}


@app.get("/api/products")
def products() -> list[dict]:
    return query("""
        SELECT Product AS code, COUNT(*) AS forms FROM kb.Document
        WHERE IsExtracted = 1 GROUP BY Product ORDER BY Product""")


# ---------------------------------------------------------------- documents

SORTS = {
    "fileName": "d.FileName", "product": "d.Product", "formNo": "d.FormNo", "revDate": "d.RevDate",
    "lang": "d.Lang", "pageCount": "d.PageCount", "fieldCount": "d.FieldCount", "blocks": "blocks",
}


@app.get("/api/documents")
def documents(
    product: str | None = None,
    lang: Literal["E", "F"] | None = None,
    family: str | None = None,
    search: str | None = Query(None, max_length=100),
    scope: Literal["extracted", "all"] = "extracted",
    sortBy: str = "revDate",
    sortDir: Literal["asc", "desc"] = "desc",
    page: int = Query(1, ge=1),
    pageSize: int = Query(50, ge=1, le=200),
) -> dict:
    where, params = ["1 = 1"], []
    if scope == "extracted":
        where.append("d.IsExtracted = 1")
    if product:
        where.append("d.Product = ?")
        params.append(product)
    if lang:
        where.append("d.Lang = ?")
        params.append(lang)
    if family:
        where.append("d.Family = ?")
        params.append(family)
    if search:
        where.append("(d.FileName LIKE ? OR d.FormNo LIKE ? OR d.Variant LIKE ?)")
        params += [f"%{search}%"] * 3
    order = SORTS.get(sortBy, "d.RevDate")
    direction = "ASC" if sortDir == "asc" else "DESC"
    where_sql = " AND ".join(where)

    total = one(f"SELECT COUNT(*) AS n FROM kb.Document d WHERE {where_sql}", *params)["n"]
    items = query(f"""
        SELECT d.DocumentId AS id, d.Product AS product, d.FileName AS fileName, d.Family AS family,
               d.FormNo AS formNo, d.Variant AS variant, d.RevDate AS revDate, d.Lang AS lang,
               d.PageCount AS pageCount, d.FieldCount AS fieldCount, d.IsFillable AS isFillable,
               d.Coverage AS coverage, ISNULL(b.blocks, 0) AS blocks, ISNULL(t.terms, 0) AS terms
        FROM kb.Document d
        OUTER APPLY (SELECT COUNT(*) AS blocks FROM kb.Block x
                     WHERE x.DocumentId = d.DocumentId AND x.BlockType <> 'region') b
        OUTER APPLY (SELECT COUNT(*) AS terms FROM kb.DefinedTerm x WHERE x.DocumentId = d.DocumentId) t
        WHERE {where_sql}
        ORDER BY {order} {direction}, d.DocumentId
        OFFSET ? ROWS FETCH NEXT ? ROWS ONLY""", *params, (page - 1) * pageSize, pageSize)
    return {"items": items, "totalCount": total, "page": page, "pageSize": pageSize,
            "hasMore": page * pageSize < total}


@app.get("/api/documents/{doc_id}")
def document(doc_id: int) -> dict:
    d = one("""
        SELECT DocumentId AS id, Product AS product, FileName AS fileName, Prefix AS prefix, Family AS family,
               FormNo AS formNo, Variant AS variant, RevDate AS revDate, Lang AS lang, PageCount AS pageCount,
               PageSize AS pageSize, IsFillable AS isFillable, FieldCount AS fieldCount, Fonts AS fonts,
               SizeKb AS sizeKb, Notes AS notes, IsExtracted AS isExtracted, BodyFontSize AS bodyFontSize,
               LinePitch AS linePitch, IndentStep AS indentStep, Coverage AS coverage, PageBands AS pageBands,
               Furniture AS furniture
        FROM kb.Document WHERE DocumentId = ?""", doc_id)
    d["pageBands"] = parse_json(d["pageBands"])
    d["furniture"] = parse_json(d["furniture"])
    d["terms"] = [r["term"] for r in query(
        "SELECT Term AS term FROM kb.DefinedTerm WHERE DocumentId = ? ORDER BY Term", doc_id)]
    # Other language / other revisions of the same form number.
    d["related"] = query("""
        SELECT r.DocumentId AS id, r.FileName AS fileName, r.Lang AS lang, r.RevDate AS revDate,
               r.Variant AS variant, r.IsExtracted AS isExtracted
        FROM kb.Document r JOIN kb.Document d ON d.DocumentId = ?
        WHERE r.Product = d.Product AND r.FormNo = d.FormNo AND r.DocumentId <> d.DocumentId
        ORDER BY r.RevDate DESC, r.Variant, r.Lang""", doc_id)
    return d


@app.get("/api/documents/{doc_id}/blocks")
def blocks(doc_id: int) -> list[dict]:
    rows = query("""
        SELECT Seq AS seq, BlockType AS type, PageNo AS page, Columns AS columns, Level AS level,
               Marker AS marker, MarkerStyle AS style, TermJson, RunInJson, RunsJson, RowsJson, Xrefs,
               CONVERT(VARCHAR(64), TextHash, 2) AS textHash
        FROM kb.Block WHERE DocumentId = ? ORDER BY Seq""", doc_id)
    for r in rows:
        r["term"] = parse_json(r.pop("TermJson"))
        r["runIn"] = parse_json(r.pop("RunInJson"))
        r["runs"] = parse_json(r.pop("RunsJson"))
        r["rows"] = parse_json(r.pop("RowsJson"))
        r["xrefs"] = parse_json(r.pop("Xrefs"))
    return rows


@app.get("/api/documents/{doc_id}/page1")
def page1(doc_id: int) -> dict:
    layout = query("SELECT PageWidth AS width, PageHeight AS height FROM kb.PageLayout WHERE DocumentId = ?",
                   doc_id)
    fields = query("""
        SELECT Seq AS seq, FieldName AS name, FieldType AS type, X0 AS x0, Y0 AS y0, X1 AS x1, Y1 AS y1,
               DefaultValue AS value, Options AS options, FontSize AS fontSize, MaxLen AS maxLen
        FROM kb.FormField WHERE DocumentId = ? ORDER BY Seq""", doc_id)
    for f in fields:
        f["options"] = parse_json(f["options"])
    assets = query("""
        SELECT a.Sha1 AS sha1, a.Ext AS ext, da.X0 AS x0, da.Y0 AS y0, da.X1 AS x1, da.Y1 AS y1
        FROM kb.DocumentAsset da JOIN kb.Asset a ON a.Sha1 = da.Sha1
        WHERE da.DocumentId = ? ORDER BY da.Seq""", doc_id)
    return {"size": layout[0] if layout else None, "fields": fields, "assets": assets}


def pdf_path(doc_id: int) -> Path:
    d = one("SELECT Product, FileName FROM kb.Document WHERE DocumentId = ?", doc_id)
    path = (PDF_ROOT / d["Product"] / d["FileName"]).resolve()
    if PDF_ROOT not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="PDF not available")
    return path


@app.get("/api/documents/{doc_id}/pdf")
def pdf(doc_id: int) -> FileResponse:
    path = pdf_path(doc_id)
    return FileResponse(path, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{path.name}"'})


@app.get("/api/documents/{doc_id}/pages/{page_no}.png")
def page_image(doc_id: int, page_no: int, zoom: float = Query(1.5, ge=0.5, le=3)) -> Response:
    with pymupdf.open(pdf_path(doc_id)) as doc:
        if not 1 <= page_no <= doc.page_count:
            raise HTTPException(status_code=404, detail="Page not found")
        pix = doc[page_no - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), annots=False)
        return Response(pix.tobytes("png"), media_type="image/png",
                        headers={"Cache-Control": "max-age=3600"})


@app.get("/api/assets/{sha1}")
def asset(sha1: str) -> Response:
    if len(sha1) != 40 or not all(c in "0123456789abcdef" for c in sha1):
        raise HTTPException(status_code=400, detail="Invalid asset id")
    a = one("SELECT Ext, Bytes FROM kb.Asset WHERE Sha1 = ?", sha1)
    media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(a["Ext"], "application/octet-stream")
    return Response(bytes(a["Bytes"]), media_type=media, headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------- text library

@app.get("/api/texts")
def texts(
    search: str | None = Query(None, max_length=200),
    product: str | None = None,
    lang: Literal["E", "F"] | None = None,
    type: Literal["item", "paragraph", "definition", "lead", "heading", "table"] | None = None,
    minDocs: int = Query(1, ge=1),
    page: int = Query(1, ge=1),
    pageSize: int = Query(50, ge=1, le=200),
) -> dict:
    """Distinct texts (exact match on normalized text hash) with reuse counts."""
    where, params = ["b.BlockType NOT IN ('region', 'sectionTitle')", "b.PlainText IS NOT NULL"], []
    if search:
        where.append("b.PlainText LIKE ?")
        params.append(f"%{search}%")
    if product:
        where.append("d.Product = ?")
        params.append(product)
    if lang:
        where.append("d.Lang = ?")
        params.append(lang)
    if type:
        where.append("b.BlockType = ?")
        params.append(type)
    where_sql = " AND ".join(where)
    base = f"""
        SELECT b.TextHash, COUNT(DISTINCT b.DocumentId) AS docs, COUNT(DISTINCT d.Product) AS products,
               MIN(b.BlockType) AS type, MIN(d.Lang) AS lang, MIN(LEN(b.PlainText)) AS length,
               STRING_AGG(CAST(d.Product AS VARCHAR(MAX)), ',') WITHIN GROUP (ORDER BY d.Product) AS productList
        FROM kb.Block b JOIN kb.Document d ON d.DocumentId = b.DocumentId
        WHERE {where_sql}
        GROUP BY b.TextHash HAVING COUNT(DISTINCT b.DocumentId) >= ?"""
    total = one(f"SELECT COUNT(*) AS n FROM ({base}) g", *params, minDocs)["n"]
    items = query(f"""
        WITH g AS ({base})
        SELECT CONVERT(VARCHAR(64), g.TextHash, 2) AS textHash, g.docs, g.products, g.type, g.lang, g.length,
               g.productList, s.PlainText AS text
        FROM g CROSS APPLY (SELECT TOP 1 PlainText FROM kb.Block x WHERE x.TextHash = g.TextHash) s
        ORDER BY g.docs DESC, g.length DESC
        OFFSET ? ROWS FETCH NEXT ? ROWS ONLY""", *params, minDocs, (page - 1) * pageSize, pageSize)
    for i in items:
        i["productList"] = sorted(set((i["productList"] or "").split(","))) if i["productList"] else []
    return {"items": items, "totalCount": total, "page": page, "pageSize": pageSize,
            "hasMore": page * pageSize < total}


@app.get("/api/texts/{text_hash}/documents")
def text_documents(text_hash: str) -> list[dict]:
    if len(text_hash) != 64 or not all(c in "0123456789ABCDEF" for c in text_hash.upper()):
        raise HTTPException(status_code=400, detail="Invalid text hash")
    return query("""
        SELECT DISTINCT d.DocumentId AS id, d.Product AS product, d.FileName AS fileName, d.FormNo AS formNo,
               d.Variant AS variant, d.Lang AS lang, d.RevDate AS revDate
        FROM kb.Block b JOIN kb.Document d ON d.DocumentId = b.DocumentId
        WHERE b.TextHash = CONVERT(BINARY(32), ?, 2)
        ORDER BY d.Product, d.FormNo, d.RevDate DESC""", text_hash.upper())


# ---------------------------------------------------------------- page-1 field catalogue

@app.get("/api/fields")
def field_catalog(
    search: str | None = Query(None, max_length=100),
    product: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(100, ge=1, le=500),
) -> dict:
    where, params = ["1 = 1"], []
    if search:
        where.append("f.FieldName LIKE ?")
        params.append(f"%{search}%")
    if product:
        where.append("d.Product = ?")
        params.append(product)
    where_sql = " AND ".join(where)
    base = f"""
        SELECT f.FieldName AS name, COUNT(DISTINCT f.DocumentId) AS docs, COUNT(DISTINCT d.Product) AS products,
               MIN(f.FieldType) AS type, MAX(f.FieldType) AS typeMax,
               STRING_AGG(CAST(d.Product AS VARCHAR(MAX)), ',') AS productList
        FROM kb.FormField f JOIN kb.Document d ON d.DocumentId = f.DocumentId
        WHERE {where_sql} GROUP BY f.FieldName"""
    total = one(f"SELECT COUNT(*) AS n FROM ({base}) g", *params)["n"]
    items = query(f"{base} ORDER BY docs DESC, name OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
                  *params, (page - 1) * pageSize, pageSize)
    for i in items:
        i["productList"] = sorted(set((i.pop("productList") or "").split(",")))
        i["mixedTypes"] = i["type"] != i.pop("typeMax")
    return {"items": items, "totalCount": total, "page": page, "pageSize": pageSize,
            "hasMore": page * pageSize < total}


# ---------------------------------------------------------------- AI chat

@app.get("/api/chat/status")
async def chat_status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            tags = (await client.get(f"{chat.OLLAMA_URL}/api/tags")).json()
        models = [m["name"] for m in tags.get("models", [])]
        return {"available": LLM_MODEL in models, "model": LLM_MODEL, "models": models}
    except httpx.HTTPError:
        return {"available": False, "model": LLM_MODEL, "models": []}


@app.post("/api/chat")
def chat_endpoint(req: chat.ChatRequest) -> StreamingResponse:
    question = next((m.content for m in reversed(req.messages) if m.role == "user"), None)
    if not question or not question.strip():
        raise HTTPException(status_code=400, detail="A user message is required")
    sources, doc = chat.retrieve(query, question, req.documentId)
    summary = chat.corpus_summary(query)
    return StreamingResponse(chat.stream_answer(req, sources, doc, summary, LLM_MODEL),
                             media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
