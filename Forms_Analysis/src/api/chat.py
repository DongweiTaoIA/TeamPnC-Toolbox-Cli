"""AI chat over the knowledge base: keyword retrieval from kb.Block + local Ollama streaming."""

from __future__ import annotations

import json
import os
import re
from typing import AsyncIterator, Callable, Literal

import httpx
from pydantic import BaseModel, Field

OLLAMA_URL = os.environ.get("KB_OLLAMA_URL", "http://127.0.0.1:11434")
MAX_SOURCES = 10

STOPWORDS = set("""
a about above after again all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how i
if in into is it its itself just me more most my no nor not now of off on once only or other our out over own same she
should so some such than that the their them then there these they this those through to too under until up very was
we were what when where which while who whom why will with would you your yours form forms contract contracts tell show
give list find which what please
le la les un une des du de et ou en au aux ce ces cet cette dans par pour sur avec sans est sont qui que quoi quel
quelle quels quelles votre vos notre nos leur leurs pas plus mais comme être avoir fait faire formulaire contrat
""".split())


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=20)
    documentId: int | None = None


def keywords(text: str, limit: int = 6) -> list[str]:
    words = re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’-]{2,}", text.lower())
    seen: dict[str, None] = {}
    for w in words:
        w = w.strip("'’-")
        if len(w) >= 4 and w not in STOPWORDS:
            seen.setdefault(w, None)
    # Longer words are usually more specific.
    return sorted(seen, key=len, reverse=True)[:limit]


def retrieve(query: Callable[..., list[dict]], question: str, document_id: int | None) -> tuple[list[dict], dict | None]:
    kws = keywords(question)
    doc = None
    if document_id:
        rows = query("""
            SELECT DocumentId AS id, Product AS product, FormNo AS formNo, Variant AS variant, Lang AS lang,
                   FileName AS fileName, RevDate AS revDate, PageCount AS pageCount, FieldCount AS fieldCount
            FROM kb.Document WHERE DocumentId = ?""", document_id)
        doc = rows[0] if rows else None

    sources: list[dict] = []
    if kws:
        score_sql = " + ".join("CASE WHEN b.PlainText LIKE ? THEN 1 ELSE 0 END" for _ in kws)
        like = [f"%{k}%" for k in kws]
        need = 1 if len(kws) == 1 else 2
        if doc:
            sources += query(f"""
                SELECT TOP 6 b.DocumentId AS documentId, b.Seq AS seq, b.BlockType AS type, b.PlainText AS text,
                       CONVERT(VARCHAR(64), b.TextHash, 2) AS textHash, ({score_sql}) AS score
                FROM kb.Block b
                WHERE b.DocumentId = ? AND b.PlainText IS NOT NULL AND ({score_sql}) >= ?
                ORDER BY score DESC, b.Seq""", *like, doc["id"], *like, 1)
        sources += query(f"""
            WITH hits AS (
                SELECT b.TextHash, b.PlainText, b.BlockType, b.DocumentId, ({score_sql}) AS score
                FROM kb.Block b
                WHERE b.BlockType NOT IN ('region', 'sectionTitle') AND b.PlainText IS NOT NULL
                  AND LEN(b.PlainText) > 40 AND ({score_sql}) >= ?
            ), ranked AS (
                SELECT TextHash, MAX(score) AS score, COUNT(DISTINCT DocumentId) AS docs,
                       MIN(DocumentId) AS documentId, MIN(BlockType) AS type
                FROM hits GROUP BY TextHash
            )
            SELECT TOP 8 r.documentId, r.type, r.score, r.docs, CONVERT(VARCHAR(64), r.TextHash, 2) AS textHash,
                   (SELECT TOP 1 PlainText FROM kb.Block x WHERE x.TextHash = r.TextHash) AS text
            FROM ranked r ORDER BY r.score DESC, r.docs DESC""", *like, *like, need)

    seen, unique = set(), []
    for s in sources:
        if s["textHash"] in seen:
            continue
        seen.add(s["textHash"])
        unique.append(s)
    unique = unique[:MAX_SOURCES]
    if unique:
        ids = sorted({s["documentId"] for s in unique})
        marks = ",".join("?" * len(ids))
        meta = {r["id"]: r for r in query(f"""
            SELECT DocumentId AS id, Product AS product, FormNo AS formNo, Variant AS variant, Lang AS lang,
                   FileName AS fileName FROM kb.Document WHERE DocumentId IN ({marks})""", *ids)}
        for i, s in enumerate(unique, start=1):
            s["id"] = f"S{i}"
            s.update({k: meta[s["documentId"]][k] for k in ("product", "formNo", "variant", "lang", "fileName")})
            s.setdefault("docs", 1)
    return unique, doc


def corpus_summary(query: Callable[..., list[dict]]) -> str:
    rows = query("""
        SELECT Product AS p, COUNT(*) AS n, SUM(CASE WHEN Lang = 'E' THEN 1 ELSE 0 END) AS e,
               SUM(CASE WHEN Lang = 'F' THEN 1 ELSE 0 END) AS f
        FROM kb.Document WHERE IsExtracted = 1 GROUP BY Product ORDER BY n DESC""")
    return "; ".join(f"{r['p']}: {r['n']} forms ({r['e']} English, {r['f']} French)" for r in rows)


SYSTEM_PROMPT = """You are the assistant of "Forms Studio", a knowledge base of insurance contract forms \
(extended warranty, GAP, credit insurance, etc.) extracted from PDF files. Each form has a fillable page 1 \
and policy pages with terms and conditions.

Rules:
- Answer using ONLY the context below. If the answer is not in the context, say you could not find it in the \
knowledge base and suggest a better search term.
- Cite the sources you use with their tags, e.g. [S1], [S3]. Never invent a source tag.
- Be concise. Use short bullet points when listing items. Quote contract wording exactly when the user asks \
what a form says.
- Reply in the language of the user's question (English or French).
- You cannot change or publish forms; you only explain and compare existing text."""


def build_context(sources: list[dict], doc: dict | None, summary: str) -> str:
    parts = [f"Knowledge base: {summary}."]
    if doc:
        parts.append(
            f"The user is currently viewing form {doc['formNo']}{doc['variant'] or ''} "
            f"(product {doc['product']}, language {doc['lang']}, file {doc['fileName']}, "
            f"{doc['pageCount']} pages, {doc['fieldCount']} page-1 fields).")
    if sources:
        parts.append("Sources:")
        for s in sources:
            used = f", same text in {s['docs']} forms" if s.get("docs", 1) > 1 else ""
            parts.append(f"[{s['id']}] form {s['formNo']}{s['variant'] or ''} {s['product']} {s['lang']}"
                         f" ({s['type']}{used}): {s['text'][:1200]}")
    else:
        parts.append("Sources: none found for this question.")
    return "\n".join(parts)


async def stream_answer(req: ChatRequest, sources: list[dict], doc: dict | None, summary: str,
                        model: str) -> AsyncIterator[bytes]:
    def line(obj: dict) -> bytes:
        return (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")

    yield line({"type": "sources", "items": [
        {k: s.get(k) for k in ("id", "documentId", "product", "formNo", "variant", "lang", "fileName", "type",
                               "docs", "textHash")} | {"text": s["text"][:400]}
        for s in sources]})

    messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n\nContext:\n" + build_context(sources, doc, summary)}]
    messages += [m.model_dump() for m in req.messages[-8:]]
    payload = {"model": model, "messages": messages, "stream": True,
               "options": {"temperature": 0.2, "num_ctx": 8192}}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5)) as client:
            async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")[:300]
                    yield line({"type": "error", "message": f"LLM error {resp.status_code}: {body}"})
                    return
                async for raw in resp.aiter_lines():
                    if not raw:
                        continue
                    chunk = json.loads(raw)
                    text = chunk.get("message", {}).get("content", "")
                    if text:
                        yield line({"type": "token", "text": text})
                    if chunk.get("done"):
                        break
    except httpx.ConnectError:
        yield line({"type": "error", "message": f"Cannot reach Ollama at {OLLAMA_URL}. Is it running?"})
        return
    yield line({"type": "done", "model": model})
