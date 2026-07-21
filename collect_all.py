"""
Run all subscription collectors and write data/latest.json.

Usage:
  python collect_all.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collectors import antigravity, chatgpt, cursor, gemini, grok, zai  # noqa: E402
from collectors.common import write_snapshot  # noqa: E402


def main() -> int:
    providers = [
        zai.collect(),
        cursor.collect(),
        grok.collect(),
        chatgpt.collect(),
        gemini.collect(),
        antigravity.collect(),
    ]
    path = write_snapshot(providers)
    snap = json.loads(path.read_text())
    print(f"Wrote {path}")
    print(f"generated_at={snap.get('generated_at')}")
    for name, p in snap.get("providers", {}).items():
        wins = p.get("windows") or []
        summary = ", ".join(
            f"{w.get('name')}={w.get('pct_used')}%" for w in wins
        ) or p.get("status")
        print(f"  {name}: {p.get('status')} | {summary}")
    if snap.get("alerts"):
        print("alerts:")
        for a in snap["alerts"]:
            print(f"  [{a.get('level')}] {a.get('provider')}: {a.get('message')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
