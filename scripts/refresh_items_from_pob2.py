"""Extract item bases and uniques from a PathOfBuilding-PoE2 checkout.

Writes two new datasets (the existing .datc64-derived base_items.json is left untouched):
    data/game/base_items/item_bases_pob2.json   {name -> {type, tags, implicit, weapon, armour, req, ...}}
    data/game/uniques/uniques.json              [{name, base, source, league, implicits, mods, variants, ...}]

Usage:
    python scripts/refresh_items_from_pob2.py --pob2 C:/path/to/PathOfBuilding-PoE2 [--write]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASES_OUT = ROOT / "data" / "game" / "base_items" / "item_bases_pob2.json"
UNIQ_OUT = ROOT / "data" / "game" / "uniques" / "uniques.json"
UNIQ_META = ROOT / "data" / "game" / "uniques" / "metadata.json"

HEADER_KEYS = {"Source", "League", "Variant", "Implicits", "Requires Level", "Version", "Sockets",
               "Limited to", "Selected Variant", "Selected Alt Variant", "Has Alt Variant",
               "Selected Alt Variant Two", "Has Alt Variant Two", "Selected Alt Variant Three",
               "Has Alt Variant Three", "Radius", "Upgrade", "Cluster Jewel", "Talisman Tier",
               "Item Level", "Quality", "Armour", "Evasion", "Energy Shield", "Ward"}


def lua_bases(path: Path) -> dict:
    """Bases/*.lua are `return function(itemBases) itemBases[...] = {...} end` chunk factories."""
    import lupa
    from src.parsers.lua_skill_data import _lua_to_python

    L = lupa.LuaRuntime(unpack_returned_tuples=True)
    body = re.sub(r"^\s*---@.*$", "", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    factory = L.execute(body)
    tbl = L.table()
    factory(tbl)
    return _lua_to_python(tbl)


def norm_base(name: str, b: dict) -> dict:
    out = {"name": name, "type": b.get("type"), "quality": b.get("quality"), "socket_limit": b.get("socketLimit")}
    tags = b.get("tags")
    out["tags"] = sorted(k for k, v in tags.items() if v) if isinstance(tags, dict) else []
    if b.get("implicit"):
        out["implicit"] = b["implicit"]
    for key in ("weapon", "armour", "flask", "charm", "jewel", "req"):
        v = b.get(key)
        if isinstance(v, dict) and v:
            out[key] = {k: v[k] for k in sorted(v)}
    if b.get("hidden"):
        out["hidden"] = True
    if b.get("subType"):
        out["sub_type"] = b["subType"]
    return out


def parse_unique_block(block: str, category: str) -> dict:
    lines = [l.rstrip() for l in block.strip("\n").split("\n") if l.strip()]
    if len(lines) < 2:
        return {}
    rec = {"name": lines[0].strip(), "base": lines[1].strip(), "category": category,
           "variants": [], "implicits": [], "mods": [], "grants_skill": []}
    n_implicit = 0
    body: list[str] = []
    for l in lines[2:]:
        m = re.match(r"^([A-Z][A-Za-z ]+?):\s*(.*)$", l)
        key = m.group(1) if m else None
        if key == "Variant":
            rec["variants"].append(m.group(2)); continue
        if key == "Implicits":
            n_implicit = int(m.group(2) or 0); continue
        if key == "Grants Skill":
            rec["grants_skill"].append(m.group(2)); continue
        if key in HEADER_KEYS:
            rec[key.lower().replace(" ", "_")] = m.group(2); continue
        body.append(l)
    rec["implicits"], rec["mods"] = body[:n_implicit], body[n_implicit:]
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pob2", required=True, type=Path)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    bases = {}
    for f in sorted((args.pob2 / "src" / "Data" / "Bases").glob("*.lua")):
        for name, b in lua_bases(f).items():
            if isinstance(b, dict):
                bases[name] = norm_base(name, b)
    print(f"item bases: {len(bases)} across {len(set(b['type'] for b in bases.values()))} types")

    uniques = []
    udir = args.pob2 / "src" / "Data" / "Uniques"
    for f in sorted(list(udir.glob("*.lua")) + list((udir / "Special").glob("*.lua"))):
        text = f.read_text(encoding="utf-8")
        for block in re.findall(r"\[\[(.*?)\]\]", text, flags=re.S):
            rec = parse_unique_block(block, f.stem)
            if rec:
                uniques.append(rec)
    print(f"uniques: {len(uniques)} from {udir.name}/*.lua (+Special)")
    if not args.write:
        print("(dry run)")
        return 0

    commit = subprocess.check_output(["git", "-C", str(args.pob2), "rev-parse", "HEAD"], text=True).strip()
    date = subprocess.check_output(["git", "-C", str(args.pob2), "log", "-1", "--format=%cI"], text=True).strip()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prov = {"source_repo": "PathOfBuilding-PoE2", "source_ref": "dev", "source_commit": commit,
            "source_commit_date": date, "extracted_at": stamp, "extractor": "scripts/refresh_items_from_pob2.py"}

    BASES_OUT.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"metadata": {**prov, "record_count": len(bases), "source_files": "src/Data/Bases/*.lua"},
                       "item_bases": dict(sorted(bases.items()))}, ensure_ascii=False, indent=1)
    BASES_OUT.write_text(text, encoding="utf-8", newline="\n")
    UNIQ_OUT.parent.mkdir(parents=True, exist_ok=True)
    utext = json.dumps({"metadata": {**prov, "record_count": len(uniques), "source_files": "src/Data/Uniques/**/*.lua"},
                        "uniques": uniques}, ensure_ascii=False, indent=1)
    UNIQ_OUT.write_text(utext, encoding="utf-8", newline="\n")
    UNIQ_META.write_text(json.dumps({**prov, "dataset": "uniques", "filename": UNIQ_OUT.name, "record_count": len(uniques),
                                     "sha256": hashlib.sha256(utext.encode()).hexdigest()}, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {BASES_OUT.relative_to(ROOT)} ({len(text)/1e6:.1f} MB) and {UNIQ_OUT.relative_to(ROOT)} ({len(utext)/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
