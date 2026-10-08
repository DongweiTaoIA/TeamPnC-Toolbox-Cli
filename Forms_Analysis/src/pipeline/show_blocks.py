"""Print extracted blocks as readable text for review."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def runs_text(runs: list[dict]) -> str:
    out = []
    for r in runs:
        t = r["text"]
        if r.get("u"):
            t = f"_{t}_"
        if r.get("term"):
            t = f"[{t}]"
        elif r.get("b"):
            t = f"**{t}**"
        out.append(t)
    return "".join(out)


def main(stem: str) -> Path:
    data = json.loads((ROOT / "output" / "extract" / f"{stem}.json").read_text(encoding="utf-8"))
    pol = data["policy"]
    lines: list[str] = []
    print = lines.append  # noqa: A001
    print(f"TERMS: {pol['terms']}")
    print(f"FURNITURE: {pol['furniture']}")
    for b in pol["blocks"]:
        t = b["type"]
        if t == "region":
            print(f"\n----- region columns={b['columns']} (p{b['page']})")
            continue
        if t == "table":
            print("  TABLE")
            for row in b["rows"]:
                print("   | " + " | ".join(row))
            continue
        ind = "  " * b.get("level", 0)
        head = f"{b['marker']} " if b.get("marker") else ""
        run_in = f"<<{runs_text(b['runIn'])}>> " if b.get("runIn") else ""
        term = f"DEF<{runs_text(b['term'])}> " if b.get("term") else ""
        xr = f"   xrefs={b['xrefs']}" if b.get("xrefs") else ""
        print(f"{ind}{t.upper()[:4]:4} {head}{term}{run_in}{runs_text(b['runs'])}{xr}")
    out = ROOT / "output" / "extract" / f"{stem}.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


if __name__ == "__main__":
    for s in sys.argv[1:]:
        sys.stdout.write(f"{main(s)}\n")
