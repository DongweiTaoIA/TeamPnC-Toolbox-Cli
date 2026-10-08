"""Send one question to /api/chat and print the streamed answer."""

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8010"
question = sys.argv[1] if len(sys.argv) > 1 else "What does towing service cover?"
doc_id = int(sys.argv[2]) if len(sys.argv) > 2 else None

body = json.dumps({"messages": [{"role": "user", "content": question}], "documentId": doc_id}).encode()
req = urllib.request.Request(f"{BASE}/api/chat", data=body, headers={"Content-Type": "application/json"})
start = time.time()
first = None
with urllib.request.urlopen(req, timeout=300) as r:
    for raw in r:
        evt = json.loads(raw)
        if evt["type"] == "sources":
            print(f"[{time.time() - start:.1f}s] sources:", [(s["id"], s["product"], s["formNo"], s["docs"]) for s in evt["items"]])
        elif evt["type"] == "token":
            first = first or time.time()
            print(evt["text"], end="", flush=True)
        else:
            print(f"\n[{evt['type']}] {evt.get('message', evt.get('model', ''))}")
print(f"\nfirst token {first - start:.1f}s, total {time.time() - start:.1f}s" if first else "")
