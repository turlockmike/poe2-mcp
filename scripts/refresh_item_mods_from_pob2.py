"""Extract the human-readable item mod table from a PathOfBuilding-PoE2 checkout.

Writes data/game/mods/item_mods_pob2.json: {mod_id -> {type, affix, text[], level, group,
spawn_weights{item_tag: weight}, mod_tags[], trade_hashes{}}}. Complements mods.json (which
is .datc64-derived and carries stat ids + ranges but no text or spawn weights).

Usage:
    python scripts/refresh_item_mods_from_pob2.py --pob2 C:/path/to/PathOfBuilding-PoE2 [--write]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "game" / "mods" / "item_mods_pob2.json"
SOURCES = ["src/Data/ModItem.lua", "src/Data/ModItemExclusive.lua", "src/Data/ModCorrupted.lua",
           "src/Data/ModFlask.lua", "src/Data/ModCharm.lua", "src/Data/ModJewel.lua"]


def lua_table(path: Path) -> dict:
    import lupa
    from src.parsers.lua_skill_data import _lua_to_python

    L = lupa.LuaRuntime(unpack_returned_tuples=True)
    body = re.sub(r"^\s*---@.*$", "", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    return _lua_to_python(L.execute(body))


def as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v[k] for k in sorted(v, key=lambda x: int(x))]
    return []


def norm(mod_id: str, m: dict, source: str) -> dict:
    text = [m[k] for k in sorted((k for k in m if isinstance(k, int) or (isinstance(k, str) and k.isdigit())), key=int)]
    if not text and isinstance(m.get("1"), str):
        text = [m["1"]]
    keys, vals = as_list(m.get("weightKey")), as_list(m.get("weightVal"))
    rec = {
        "mod_id": mod_id,
        "source": source,
        "type": m.get("type"),
        "affix": m.get("affix"),
        "text": [t for t in text if isinstance(t, str)],
        "level": m.get("level"),
        "group": m.get("group"),
        "spawn_weights": {k: v for k, v in zip(keys, vals) if isinstance(k, str)},
        "mod_tags": [t for t in as_list(m.get("modTags")) if isinstance(t, str)],
        "stat_order": [s for s in as_list(m.get("statOrder")) if isinstance(s, (int, float))],
    }
    th = m.get("tradeHashes")
    if isinstance(th, dict):
        rec["trade_hashes"] = {str(k): [t for t in as_list(v) if isinstance(t, str)] for k, v in th.items()}
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pob2", required=True, type=Path)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    mods: dict = {}
    for rel in SOURCES:
        p = args.pob2 / rel
        if not p.exists():
            print(f"  (skip, missing) {rel}")
            continue
        t = lua_table(p)
        n = 0
        for mod_id, m in t.items():
            if isinstance(m, dict):
                mods[str(mod_id)] = norm(str(mod_id), m, Path(rel).stem)
                n += 1
        print(f"  {rel:<36} {n:6d} mods")
    print(f"total {len(mods)} mods | by type: {dict(Counter(r['type'] for r in mods.values()))}")
    if not args.write:
        print("(dry run)")
        return 0

    commit = subprocess.check_output(["git", "-C", str(args.pob2), "rev-parse", "HEAD"], text=True).strip()
    date = subprocess.check_output(["git", "-C", str(args.pob2), "log", "-1", "--format=%cI"], text=True).strip()
    text = json.dumps({"metadata": {
        "dataset": "item_mods_pob2", "source_repo": "PathOfBuilding-PoE2", "source_ref": "dev",
        "source_commit": commit, "source_commit_date": date, "source_files": SOURCES,
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "extractor": "scripts/refresh_item_mods_from_pob2.py", "record_count": len(mods),
    }, "mods": dict(sorted(mods.items()))}, ensure_ascii=False, indent=1)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text)/1e6:.1f} MB) sha256 {hashlib.sha256(text.encode()).hexdigest()[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
