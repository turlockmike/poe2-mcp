"""Refresh data/game/skill_gems/skill_gems_v2.json from a PathOfBuilding-PoE2 checkout.

Usage:
    python scripts/refresh_skill_gems_from_pob2.py --pob2 C:/path/to/PathOfBuilding-PoE2 [--write]

Without --write it only reports what would change. This script is the (previously
unversioned) "scripts/extract_skill_gems_rich.py" referenced by metadata_v2.json; it is
committed so anyone can rerun the refresh against a newer PoB2.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.parsers.lua_skill_data import extract_canonical_subset, parse_skills_lua  # noqa: E402

OUT = ROOT / "data" / "game" / "skill_gems" / "skill_gems_v2.json"
META = ROOT / "data" / "game" / "skill_gems" / "metadata_v2.json"
VERSION = ROOT / "data" / "game" / "version.json"
SKIP = {"SkillAssets.lua"}  # icon/asset table, not skill data


def git(pob2: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(pob2), *args], text=True).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pob2", required=True, type=Path, help="PathOfBuilding-PoE2 checkout")
    ap.add_argument("--write", action="store_true", help="write the dataset + metadata")
    args = ap.parse_args()

    skills_dir = args.pob2 / "src" / "Data" / "Skills"
    files = sorted(p for p in skills_dir.glob("*.lua") if p.name not in SKIP)
    merged: dict = {}
    for f in files:
        parsed = parse_skills_lua(f)
        merged.update(parsed)
        print(f"  {f.name:<18} {len(parsed):5d} skills")
    skills = {k: extract_canonical_subset(v) for k, v in sorted(merged.items())}

    old = json.loads(OUT.read_text(encoding="utf-8")).get("skills", {}) if OUT.exists() else {}
    added = sorted(set(skills) - set(old))
    removed = sorted(set(old) - set(skills))
    changed = sorted(k for k in set(skills) & set(old) if skills[k] != old[k])
    print(f"\nparsed {len(skills)} skills  (shipped {len(old)}): +{len(added)} added, -{len(removed)} removed, {len(changed)} changed")
    if added:
        print("  added:", ", ".join(added[:20]), "..." if len(added) > 20 else "")
    if removed:
        print("  removed:", ", ".join(removed[:20]), "..." if len(removed) > 20 else "")

    if not args.write:
        print("\n(dry run; pass --write to update the dataset)")
        return 0

    payload = {"schema_version": 2, "skills": skills}
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    data = text.encode("utf-8")

    commit = git(args.pob2, "rev-parse", "HEAD")
    date = git(args.pob2, "log", "-1", "--format=%cI")
    meta = json.loads(META.read_text(encoding="utf-8")) if META.exists() else {}
    meta.update({
        "dataset": "skill_gems_v2",
        "filename": OUT.name,
        "schema_version": 2,
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_repo": "PathOfBuilding-PoE2",
        "source_commit": commit,
        "source_commit_date": date,
        "source_files": [f"src/Data/Skills/{f.name}" for f in files],
        "extractor": "scripts/refresh_skill_gems_from_pob2.py",
        "record_count": len(skills),
        "coverage": {
            "with_qualityStats": sum(1 for s in skills.values() if s.get("qualityStats")),
            "with_levels": sum(1 for s in skills.values() if s.get("levels")),
            "with_statSets": sum(1 for s in skills.values() if s.get("statSets")),
        },
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    })
    META.write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8", newline="\n")

    if VERSION.exists():
        v = json.loads(VERSION.read_text(encoding="utf-8"))
        ds = v.setdefault("datasets", {}).setdefault("skill_gems_v2", {})
        ds["record_count"] = len(skills)
        ds["coverage"] = meta["coverage"]
        ds["note"] = f"Refreshed {meta['extracted_at'][:10]} from PoB2 dev @ {commit[:9]} ({date[:10]}) via scripts/refresh_skill_gems_from_pob2.py"
        v["data_revision"] = int(v.get("data_revision", 0)) + 1
        v["released_as"] = f"data-v{v.get('patch_version', '0.5')}.0-r{v['data_revision']}"
        VERSION.write_text(json.dumps(v, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"\nwrote {OUT.name} ({len(data)/1e6:.1f} MB), {META.name}, version.json r{v.get('data_revision')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
