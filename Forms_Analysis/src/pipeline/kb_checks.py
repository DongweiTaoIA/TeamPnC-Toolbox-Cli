"""Quick sanity queries against the kb schema."""

import sys

import pyodbc

import load_sql as L

QUERIES = {
    "documents by product": """
        SELECT Product, COUNT(*) AS docs, SUM(CAST(InScope AS INT)) AS in_scope,
               SUM(CAST(IsExtracted AS INT)) AS extracted, SUM(CAST(IsFillable AS INT)) AS fillable
        FROM kb.Document GROUP BY Product ORDER BY Product""",
    "blocks by type": "SELECT BlockType, COUNT(*) AS n FROM kb.Block GROUP BY BlockType ORDER BY n DESC",
    "E7024E first blocks (json)": """
        SELECT TOP 4 b.Seq, b.BlockType, JSON_VALUE(b.RunsJson, '$[0].text') AS first_run
        FROM kb.Block b JOIN kb.Document d ON d.DocumentId = b.DocumentId
        WHERE d.FileName = 'Contract_E7024JAN2026E.pdf' AND b.BlockType <> 'region' ORDER BY b.Seq""",
    "most reused paragraphs (exact text)": """
        SELECT TOP 8 COUNT(DISTINCT b.DocumentId) AS docs, COUNT(DISTINCT d.Product) AS products,
               LEFT(MIN(b.PlainText), 90) AS sample
        FROM kb.Block b JOIN kb.Document d ON d.DocumentId = b.DocumentId
        WHERE b.BlockType IN ('item', 'paragraph', 'definition') AND LEN(b.PlainText) > 80
        GROUP BY b.TextHash ORDER BY docs DESC""",
    "most common page-1 fields": """
        SELECT TOP 8 FieldName, COUNT(DISTINCT DocumentId) AS docs FROM kb.FormField
        GROUP BY FieldName ORDER BY docs DESC""",
}


def main() -> int:
    cfg = L.load_env()
    with pyodbc.connect(L.conn_str(cfg, cfg.get("KB_SQL_DATABASE", "FormsKB"))) as cn:
        for title, sql in QUERIES.items():
            cur = cn.execute(sql)
            print(f"\n== {title}")
            print("  " + " | ".join(c[0] for c in cur.description))
            for row in cur.fetchall():
                print("  " + " | ".join(str(v) for v in row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
