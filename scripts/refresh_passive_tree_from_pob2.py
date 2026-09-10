"""Rebuild data/psg_passive_nodes.json (the node graph the passive-tree resolver uses)
from a PathOfBuilding-PoE2 checkout's src/TreeData/<ver>/tree.lua.

PoB2's tree carries every node with its group/orbit placement, connections, name, stats,
and keystone/notable/ascendancy flags. Positions are computed the same way PoB does:
    x = group.x + orbitRadii[orbit] * sin(angle),  y = group.y - orbitRadii[orbit] * cos(angle)
with angle = orbitAnglesByOrbit[orbit][orbitIndex] (falls back to 2*pi*idx/skillsPerOrbit).

Usage:
    python scripts/refresh_passive_tree_from_pob2.py --pob2 C:/path/to/PathOfBuilding-PoE2 [--tree 0_5] [--write]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "psg_passive_nodes.json"
META = ROOT / "data" / "psg_passive_nodes.metadata.json"


def load_tree(path: Path) -> dict:
    import lupa
    from src.parsers.lua_skill_data import _lua_to_python

    L = lupa.LuaRuntime(unpack_returned_tuples=True)
    return _lua_to_python(L.execute(path.read_text(encoding="utf-8")))


def as_list(v):
    """lupa tables with 1-based integer keys come back as dicts or lists; normalise."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v[k] for k in sorted(v, key=lambda x: int(x))]
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pob2", required=True, type=Path)
    ap.add_argument("--tree", default="0_5", help="TreeData subfolder (default 0_5)")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    tree_path = args.pob2 / "src" / "TreeData" / args.tree / "tree.lua"
    t = load_tree(tree_path)
    consts = t["constants"]
    radii = as_list(consts["orbitRadii"])
    per_orbit = as_list(consts["skillsPerOrbit"])
    angles = [as_list(a) for a in as_list(consts.get("orbitAnglesByOrbit", []))]
    groups = {int(k): v for k, v in t["groups"].items()}
    nodes_in = {int(k): v for k, v in t["nodes"].items()}

    out = {}
    for nid, n in sorted(nodes_in.items()):
        g = groups.get(int(n.get("group", 0)))
        orbit = int(n.get("orbit", 0))
        idx = int(n.get("orbitIndex", 0))
        if g is not None:
            if orbit < len(angles) and angles[orbit] and idx < len(angles[orbit]):
                ang = angles[orbit][idx]
            else:
                ang = 2 * math.pi * idx / max(1, per_orbit[orbit] if orbit < len(per_orbit) else 1)
            r = radii[orbit] if orbit < len(radii) else 0
            x, y = g["x"] + r * math.sin(ang), g["y"] - r * math.cos(ang)
        else:
            x = y = 0.0
        conns = sorted({int(c["id"]) for c in as_list(n.get("connections")) if isinstance(c, dict) and "id" in c})
        rec = {
            "psg_id": nid,
            "x": round(x, 3), "y": round(y, 3),
            "radius": radii[orbit] if orbit < len(radii) else 0,
            "position": idx, "orbit": orbit,
            "connections": conns,
            "group_id": int(n.get("group", 0)),
            "source": "pob2_tree",
            "string_id": n.get("stringId"),
            "name": n.get("name", ""),
            "stats": [s for s in as_list(n.get("stats")) if isinstance(s, str)],
            "icon": n.get("icon", ""),
            "is_notable": bool(n.get("isNotable")),
            "is_keystone": bool(n.get("isKeystone")),
            "is_jewel_socket": bool(n.get("isJewelSocket")),
            "is_attribute": bool(n.get("isAttribute")),
            "is_ascendancy_start": bool(n.get("isAscendancyStart")),
            "ascendancy": n.get("ascendancyName"),
            "class_start": n.get("classStartIndex"),
        }
        if n.get("flavourText"):
            rec["flavour_text"] = as_list(n["flavourText"]) if not isinstance(n["flavourText"], str) else [n["flavourText"]]
        if n.get("recipe"):
            rec["recipe"] = as_list(n["recipe"])
        out[str(nid)] = rec

    # make adjacency symmetric (PoB lists each edge once)
    for nid, rec in out.items():
        for c in rec["connections"]:
            other = out.get(str(c))
            if other is not None and int(nid) not in other["connections"]:
                other["connections"].append(int(nid))
    for rec in out.values():
        rec["connections"].sort()

    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    print(f"nodes: {len(out)} (shipped {len(old)}): +{len(set(out) - set(old))} added, -{len(set(old) - set(out))} removed")
    print(f"  keystones {sum(r['is_keystone'] for r in out.values())}, notables {sum(r['is_notable'] for r in out.values())}, "
          f"jewel sockets {sum(r['is_jewel_socket'] for r in out.values())}, ascendancy nodes {sum(1 for r in out.values() if r['ascendancy'])}")
    if not args.write:
        print("(dry run)")
        return 0

    text = json.dumps(out, ensure_ascii=False, indent=1)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    commit = subprocess.check_output(["git", "-C", str(args.pob2), "rev-parse", "HEAD"], text=True).strip()
    date = subprocess.check_output(["git", "-C", str(args.pob2), "log", "-1", "--format=%cI"], text=True).strip()
    META.write_text(json.dumps({
        "dataset": "psg_passive_nodes", "filename": OUT.name,
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_repo": "PathOfBuilding-PoE2", "source_ref": "dev", "source_commit": commit, "source_commit_date": date,
        "source_files": [f"src/TreeData/{args.tree}/tree.lua"],
        "extractor": "scripts/refresh_passive_tree_from_pob2.py",
        "record_count": len(out), "bytes": len(text.encode("utf-8")),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "notes": "Node ids are the game's passive skill ids (same ids poe.ninja/PoB export). Positions computed from group+orbit like PoB.",
    }, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT.name} ({len(text)/1e6:.1f} MB) + {META.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
