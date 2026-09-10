"""Lazy loaders for the PoB2-derived item datasets shipped under data/game/.

    item_bases()  -> {name: {type, tags, implicit, weapon, armour, req, ...}}   (Bases/*.lua)
    uniques()     -> [{name, base, category, implicits, mods, variants, ...}]    (Uniques/**/*.lua)
    item_mods()   -> {mod_id: {type, affix, text, level, group, spawn_weights, ...}} (ModItem*.lua)

All three are produced by scripts/refresh_*_from_pob2.py and are optional: a missing file
yields an empty container so callers degrade gracefully.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from .game_data import GAME_DATA_DIR

ITEM_BASES_JSON = GAME_DATA_DIR / "base_items" / "item_bases_pob2.json"
UNIQUES_JSON = GAME_DATA_DIR / "uniques" / "uniques.json"
ITEM_MODS_JSON = GAME_DATA_DIR / "mods" / "item_mods_pob2.json"


def _load(path: Path, key: str, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(key, default)
    except (OSError, ValueError):
        return default


@lru_cache(maxsize=1)
def item_bases() -> Dict[str, Dict[str, Any]]:
    return _load(ITEM_BASES_JSON, "item_bases", {})


@lru_cache(maxsize=1)
def uniques() -> List[Dict[str, Any]]:
    return _load(UNIQUES_JSON, "uniques", [])


@lru_cache(maxsize=1)
def item_mods() -> Dict[str, Dict[str, Any]]:
    return _load(ITEM_MODS_JSON, "mods", {})


def find_base(name: str) -> Dict[str, Any] | None:
    """Exact (case-insensitive) then substring match on base name."""
    q = name.strip().lower()
    bases = item_bases()
    for n, b in bases.items():
        if n.lower() == q:
            return b
    for n, b in bases.items():
        if q in n.lower():
            return b
    return None


def find_uniques(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    q = query.strip().lower()
    exact = [u for u in uniques() if u["name"].lower() == q]
    if exact:
        return exact[:limit]
    return [u for u in uniques() if q in u["name"].lower() or q in u.get("base", "").lower()][:limit]


def search_items(query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Unified name search across bases and uniques: [{kind, name, base/type, summary}]."""
    q = query.strip().lower()
    out: List[Dict[str, Any]] = []
    for u in uniques():
        if q in u["name"].lower():
            out.append({"kind": "unique", "name": u["name"], "base": u.get("base"), "category": u.get("category"),
                        "summary": "; ".join(u.get("mods", [])[:3])})
    for n, b in item_bases().items():
        if q in n.lower():
            summ = b.get("implicit") or (", ".join(f"{k} {v}" for k, v in (b.get("weapon") or b.get("armour") or {}).items())[:80])
            out.append({"kind": "base", "name": n, "type": b.get("type"), "summary": summ})
    exact_first = sorted(out, key=lambda r: (r["name"].lower() != q, r["kind"] != "unique", r["name"]))
    return exact_first[:limit]


def format_base(b: Dict[str, Any]) -> str:
    lines = [f"# {b['name']}", "", f"**Type:** {b.get('type')}"]
    if b.get("implicit"):
        lines.append(f"**Implicit:** {b['implicit']}")
    for key, label in (("weapon", "Weapon"), ("armour", "Defences"), ("flask", "Flask"), ("charm", "Charm"), ("jewel", "Jewel")):
        if b.get(key):
            lines.append(f"**{label}:** " + ", ".join(f"{k} {v}" for k, v in b[key].items()))
    if b.get("req"):
        lines.append("**Requirements:** " + ", ".join(f"{k} {v}" for k, v in b["req"].items()))
    if b.get("socket_limit") is not None:
        lines.append(f"**Socket limit:** {b['socket_limit']}")
    if b.get("tags"):
        lines.append(f"**Tags:** {', '.join(b['tags'])}")
    lines.append("\n*Source: data/game/base_items/item_bases_pob2.json (PoB2 src/Data/Bases)*")
    return "\n".join(lines)


def format_unique(u: Dict[str, Any]) -> str:
    lines = [f"# {u['name']}", f"*{u.get('base', '')}*", ""]
    for key in ("source", "league", "requires_level", "limited_to", "radius"):
        if u.get(key):
            lines.append(f"**{key.replace('_', ' ').title()}:** {u[key]}")
    if u.get("variants"):
        lines.append(f"**Variants:** {', '.join(u['variants'])}  (mods tagged `{{variant:n}}` apply to that variant only)")
    if u.get("grants_skill"):
        lines.append("**Grants Skill:** " + "; ".join(u["grants_skill"]))
    if u.get("implicits"):
        lines.append("\n**Implicits**")
        lines += [f"- {m}" for m in u["implicits"]]
    if u.get("mods"):
        lines.append("\n**Modifiers**")
        lines += [f"- {m}" for m in u["mods"]]
    lines.append("\n*Source: data/game/uniques/uniques.json (PoB2 src/Data/Uniques)*")
    return "\n".join(lines)
