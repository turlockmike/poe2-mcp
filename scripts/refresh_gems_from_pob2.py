"""Refresh data/game/skill_gems/skill_gems.json (the Gems.lua x Skills/*.lua joined view)
from a PathOfBuilding-PoE2 checkout.

Usage:
    python scripts/refresh_gems_from_pob2.py --pob2 C:/path/to/PathOfBuilding-PoE2 [--write]

Run refresh_skill_gems_from_pob2.py first (or in the same session): this script reads the
granted-effect data straight from the same Lua files, so both datasets come from one commit.
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

from src.parsers.lua_skill_data import parse_skills_lua  # noqa: E402

OUT = ROOT / "data" / "game" / "skill_gems" / "skill_gems.json"
META = ROOT / "data" / "game" / "skill_gems" / "metadata.json"
VERSION = ROOT / "data" / "game" / "version.json"
SKIP = {"SkillAssets.lua"}


def git(pob2: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(pob2), *args], text=True).strip()


def load_gems_lua(path: Path) -> dict:
    """Gems.lua is a plain `return { [id] = {...}, ... }` table with no helper calls."""
    import lupa

    L = lupa.LuaRuntime(unpack_returned_tuples=True)
    body = re.sub(r"^\s*---@.*$", "", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    from src.parsers.lua_skill_data import _lua_to_python

    return _lua_to_python(L.execute(body))


def snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def level_entry(lv: dict) -> dict:
    out = {}
    for k, v in lv.items():
        if k == "cost" and isinstance(v, dict) and v:
            (ctype, cval), *_ = v.items()
            out["cost"] = {"type": ctype, "value": cval}
        elif isinstance(v, (int, float, str, bool)):
            out[snake(k)] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "storedUses" else v
    return out


def stat_sets_of(effect: dict) -> list:
    out = []
    for i, ss in enumerate(effect.get("statSets") or [], start=1):
        if not isinstance(ss, dict):
            continue
        rec = {"index": i}
        for src, dst in (("label", "label"), ("baseEffectiveness", "base_effectiveness"),
                         ("incrementalEffectiveness", "incremental_effectiveness"),
                         ("damageIncrementalEffectiveness", "damage_incremental_effectiveness")):
            if src in ss:
                rec[dst] = ss[src]
        if ss.get("stats"):
            rec["stats"] = [s for s in ss["stats"] if isinstance(s, str)]
        if ss.get("constantStats"):
            rec["constant_stats"] = {k: v for k, v in ss["constantStats"].items()} if isinstance(ss["constantStats"], dict) else ss["constantStats"]
        out.append(rec)
    return out


def granted_effect(effect_id: str, effects: dict) -> dict | None:
    e = effects.get(effect_id)
    if not e:
        return None
    st = e.get("skillTypes")
    types = sorted(k for k, v in st.items() if v) if isinstance(st, dict) else list(st or [])
    levels = e.get("levels") or []
    if isinstance(levels, dict):
        levels = [levels[k] for k in sorted(levels, key=lambda x: int(x))]
    return {
        "effect_id": effect_id,
        "skill_types": types,
        "cast_time": e.get("castTime"),
        "levels": {str(i): level_entry(lv) for i, lv in enumerate(levels, start=1) if isinstance(lv, dict)},
        "stat_sets": stat_sets_of(e),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pob2", required=True, type=Path)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    skills_dir = args.pob2 / "src" / "Data" / "Skills"
    effects: dict = {}
    per_file = {}
    for f in sorted(p for p in skills_dir.glob("*.lua") if p.name not in SKIP):
        parsed = parse_skills_lua(f)
        per_file[f.name] = len(parsed)
        effects.update(parsed)
    gems = load_gems_lua(args.pob2 / "src" / "Data" / "Gems.lua")

    records, unmatched = [], []
    for gem_id, g in sorted(gems.items()):
        tags = g.get("tags") or {}
        rec = {
            "gem_id": gem_id,
            "variant_id": g.get("variantId"),
            "name": g.get("name"),
            "base_type_name": g.get("baseTypeName"),
            "gem_type": g.get("gemType"),
            "tier": g.get("Tier"),
            "natural_max_level": g.get("naturalMaxLevel"),
            "requirements": {"str": g.get("reqStr", 0), "dex": g.get("reqDex", 0), "int": g.get("reqInt", 0)},
            "weapon_requirements": g.get("weaponRequirements"),
            "tags": sorted(k for k, v in tags.items() if v) if isinstance(tags, dict) else list(tags),
            "tag_string": g.get("tagString"),
            "additional_stat_sets": [g[k] for k in sorted(g) if k.startswith("additionalStatSet")],
            "granted_effect": granted_effect(g.get("grantedEffectId", ""), effects),
        }
        if rec["granted_effect"] is None:
            unmatched.append(rec["name"])
        records.append(rec)

    old = json.loads(OUT.read_text(encoding="utf-8")).get("skill_gems", []) if OUT.exists() else []
    old_names = {r["gem_id"] for r in old}
    new_names = {r["gem_id"] for r in records}
    print(f"gems: {len(records)} (shipped {len(old)}): +{len(new_names - old_names)} added, -{len(old_names - new_names)} removed; unmatched effects: {len(unmatched)}")
    print("  by type:", dict(Counter(r["gem_type"] for r in records)))
    added = sorted(r["name"] for r in records if r["gem_id"] not in old_names)
    if added:
        print("  added:", ", ".join(added[:25]), "..." if len(added) > 25 else "")
    if unmatched:
        print("  unmatched:", ", ".join(unmatched[:10]))
    if not args.write:
        print("(dry run)")
        return 0

    text = json.dumps({"schema_version": 1, "skill_gems": records}, ensure_ascii=False, indent=1)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    data = text.encode("utf-8")
    commit = git(args.pob2, "rev-parse", "HEAD")
    date = git(args.pob2, "log", "-1", "--format=%cI")
    meta = json.loads(META.read_text(encoding="utf-8")) if META.exists() else {}
    meta.update({
        "dataset": "skill_gems", "filename": OUT.name,
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_repo": "PathOfBuilding-PoE2", "source_ref": "dev", "source_commit": commit, "source_commit_date": date,
        "source_files": ["src/Data/Gems.lua"] + [f"src/Data/Skills/{n}" for n in per_file],
        "extractor": "scripts/refresh_gems_from_pob2.py",
        "record_count": len(records), "matched_effect_count": len(records) - len(unmatched),
        "unmatched_count": len(unmatched), "per_skills_file_counts": per_file,
        "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    })
    META.write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8", newline="\n")
    v = json.loads(VERSION.read_text(encoding="utf-8"))
    ds = v.setdefault("datasets", {}).setdefault("skill_gems", {})
    ds["record_count"] = len(records)
    ds["matched_effect_count"] = len(records) - len(unmatched)
    ds["source"] = f"PoB2 dev @ {commit[:9]} ({date[:10]}) - Gems.lua + Skills/*.lua joined view"
    ds["note"] = f"Refreshed {meta['extracted_at'][:10]} via scripts/refresh_gems_from_pob2.py"
    VERSION.write_text(json.dumps(v, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT.name} ({len(data)/1e6:.1f} MB), metadata.json, version.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
