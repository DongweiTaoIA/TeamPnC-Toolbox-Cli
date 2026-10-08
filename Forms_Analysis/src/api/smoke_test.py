"""Smoke test: call every API endpoint once and report status + a short summary."""

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        body = r.read()
        ctype = r.headers.get("content-type", "")
        return r.status, (json.loads(body) if "json" in ctype else body)


def main() -> int:
    failures = 0
    checks = [
        "/api/stats", "/api/products", "/api/documents?product=EW&search=E7024&pageSize=5",
        "/api/texts?minDocs=50&pageSize=3", "/api/fields?pageSize=3",
    ]
    results = {}
    for path in checks:
        try:
            status, body = get(path)
            results[path] = body
            size = len(body["items"]) if isinstance(body, dict) and "items" in body else len(body)
            print(f"OK  {status} {path}  ({size} items)")
        except Exception as ex:
            failures += 1
            print(f"ERR {path}: {ex}")

    docs = results.get("/api/documents?product=EW&search=E7024&pageSize=5", {}).get("items", [])
    texts = results.get("/api/texts?minDocs=50&pageSize=3", {}).get("items", [])
    if docs:
        doc_id = docs[0]["id"]
        for path in (f"/api/documents/{doc_id}", f"/api/documents/{doc_id}/blocks",
                     f"/api/documents/{doc_id}/page1", f"/api/documents/{doc_id}/pdf",
                     f"/api/documents/{doc_id}/pages/1.png"):
            try:
                status, body = get(path)
                size = len(body) if not isinstance(body, dict) else len(body.keys())
                print(f"OK  {status} {path}  ({size})")
            except Exception as ex:
                failures += 1
                print(f"ERR {path}: {ex}")
    if texts:
        path = f"/api/texts/{texts[0]['textHash']}/documents"
        try:
            status, body = get(path)
            print(f"OK  {status} {path}  ({len(body)} documents)")
        except Exception as ex:
            failures += 1
            print(f"ERR {path}: {ex}")
    for path in ("/api/documents/999999", "/api/assets/xyz", "/api/texts/zz/documents"):
        try:
            get(path)
            failures += 1
            print(f"ERR {path}: expected 4xx")
        except urllib.error.HTTPError as ex:
            print(f"OK  {ex.code} {path}  (expected error)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
