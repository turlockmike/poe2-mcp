"""
Structural parser for PoB2's Lua skill-data tables (Issue #119).

PoB2 ships rich per-skill data in src/Data/Skills/*.lua — qualityStats,
constantStats, statSets with per-level damage arrays, baseEffectiveness
etc. The v1 extractor at scripts/extract_skill_gems.py only pulls Gems.lua
top-level metadata. This module is the structural extractor that captures
the deep numeric data.

Strategy: embed Lua 5.4 via the `lupa` package, stub the SkillType enum so
table-key expressions like [SkillType.Spell] resolve to plain strings,
execute the .lua file as a script that populates a local `skills` table,
then walk the resulting Lua object and convert it to plain Python primitives.

Why lupa over hand-rolled parsing:
  * PoB's Lua mixes table constructors with conditional expressions and
    references to global lookup tables (SkillType.*). A naive literal
    parser breaks on the first such reference.
  * lupa runs Lua's own parser + evaluator, so any syntactic feature PoB
    uses is supported by definition.
  * We don't need a Python-side AST; we just want the data. lupa hands us
    the resulting Lua table, which we walk in 30 lines.

Lives in its own light-weight module (no SQLAlchemy / mcp_server imports)
so unit tests run without paying the main package import cost.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Lua prelude prepended to every Skills/*.lua file before execution.
# Stubs out the module-level references PoB's data files assume exist:
#  * `SkillType.X` -> returns the string "X" so the metamethod plays nice.
#  * `skills` table is the capture target — assignments like
#    `skills["IceNovaPlayer"] = { ... }` populate it.
LUA_PRELUDE = r"""
-- PoB defines `mod(name, modType, value, flags, keywords, ...tags)` in
-- ModParser.lua to build a modifier object. Inside the data files it's
-- called inline for "baseMods" / "incrementalMods" lists. We don't need
-- the real ModParser semantics — just preserve the call as a tagged dict
-- so downstream code can see what was declared.
function mod(name, modType, value, flag1, flag2, ...)
    local tags = {...}
    return { __mod = true, name = name, type = modType, value = value, flag1 = flag1, flag2 = flag2, tags = tags }
end

function flag(name, ...)
    return { __flag = true, name = name, tags = {...} }
end

function skill(name, ...)
    return { __skill = true, name = name, tags = {...} }
end

-- Catch-all global metatable: any global identifier PoB references that
-- we haven't pre-stubbed (SkillType, KeywordFlag, ModFlag, etc.) resolves
-- to a metatabled placeholder whose __index returns the bare key as a
-- string and whose __call returns a tagged dict mirroring the call args.
-- This lets us run any PoB data file without enumerating every helper.
-- PoB uses the LuaJIT `bit` library for flag masks (bit.bor(ModFlag.X, ...)).
-- Flags resolve to bare-name strings under our stubs, so keep the names:
-- bor("A", "B") -> "A|B". Downstream consumers only need the labels.
bit = {
    bor = function(...)
        local parts = {}
        for _, v in ipairs({...}) do parts[#parts + 1] = tostring(v) end
        return table.concat(parts, "|")
    end,
    band = function(a, b) return tostring(a) .. "&" .. tostring(b) end,
    bnot = function(a) return "~" .. tostring(a) end,
}
local stub_mt = {
    __index = function(_, k) return k end,
    __call = function(self, ...) return { __stub_call = true, args = {...} } end,
}
local global_mt = {
    __index = function(_, name)
        return setmetatable({ __stub_name = name }, stub_mt)
    end,
}
setmetatable(_G, global_mt)

-- Capture target.
skills = {}
"""

LUA_EPILOGUE = r"""
return skills
"""


def _lua_to_python(obj: Any) -> Any:
    """Recursively convert a lupa Lua table (or scalar) to plain Python.

    Lua tables that look like arrays (consecutive integer keys starting at
    1) become Python lists. Mixed-key tables become dicts with the integer
    keys preserved as strings (since JSON requires string keys).
    """
    # Primitives pass through unchanged.
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj

    # Lua tables have keys() / values() methods via lupa.
    if hasattr(obj, "items") and callable(obj.items):
        pairs = list(obj.items())
    elif hasattr(obj, "keys") and callable(obj.keys):
        pairs = [(k, obj[k]) for k in obj.keys()]
    else:
        # Unknown — return as-is and let JSON encoder explode if it does.
        return obj

    if not pairs:
        return {}

    # Detect array-shape: all keys are positive ints AND form a contiguous
    # 1..N sequence (Lua's array convention).
    keys = [k for k, _ in pairs]
    is_array = all(isinstance(k, int) and k > 0 for k in keys) and sorted(keys) == list(
        range(1, len(keys) + 1)
    )

    if is_array:
        # Sort by index then strip the keys.
        return [_lua_to_python(v) for _, v in sorted(pairs, key=lambda p: p[0])]

    # Dict shape — coerce int keys to strings for JSON compatibility.
    result: Dict[str, Any] = {}
    for k, v in pairs:
        if isinstance(k, bool):
            # Lua doesn't actually allow bool keys in tables, but cover edge.
            key = str(k).lower()
        elif isinstance(k, int):
            key = str(k)
        else:
            key = str(k)
        result[key] = _lua_to_python(v)
    return result


def parse_skills_lua(path: Path) -> Dict[str, Dict[str, Any]]:
    """Parse a single PoB2 Skills/*.lua file into a Python dict.

    Args:
        path: Path to e.g. src/Data/Skills/act_int.lua

    Returns:
        Dict keyed by skill_id (the string inside `skills["X"] = {...}`)
        with values as fully-converted dicts.

    Raises:
        FileNotFoundError if path doesn't exist.
        lupa.LuaError on Lua syntax/runtime errors (which would indicate
        the file uses a feature the prelude doesn't stub).
    """
    if not path.exists():
        raise FileNotFoundError(f"Lua skills file not found: {path}")

    import lupa

    L = lupa.LuaRuntime(unpack_returned_tuples=True)

    body = path.read_text(encoding="utf-8")
    # PoB's Skills/*.lua files start with:
    #     local skills, mod, flag, skill = ...
    # which captures chunk varargs supplied by PoB's loader. When we run
    # the file as a top-level script, `...` is empty and those locals are
    # nil, shadowing the globals our prelude defines. Strip that line so
    # the global stubs (`skills`, `mod`, `flag`) carry through.
    import re

    body = re.sub(
        r"^\s*local\s+skills\s*,\s*mod\s*,\s*flag\s*,\s*skill\s*=\s*\.\.\.\s*$",
        "-- (line stripped by parser: file expects chunk varargs)",
        body,
        count=1,
        flags=re.MULTILINE,
    )

    # Since mid-2026 PoB2's data files are chunk *factories*:
    #     return function(skills, mod, flag, skill) ... end
    # Evaluate the chunk to get the factory and call it with our stub `skills`
    # table and helpers. Older files (top-level `skills[...] = {...}`
    # statements) still take the script path.
    factory_re = r"^\s*return\s+function\s*\(\s*skills\s*,\s*mod\s*,\s*flag\s*,\s*skill\s*\)"
    if re.search(factory_re, body, re.MULTILINE):
        body = re.sub(r"^\s*---@cast.*$", "", body, flags=re.MULTILINE)  # LuaLS annotations
        factory = L.execute(LUA_PRELUDE + "\n" + body)
        g = L.globals()
        factory(g.skills, g.mod, g.flag, g.skill)
        lua_table = g.skills
    else:
        full_script = LUA_PRELUDE + "\n" + body + "\n" + LUA_EPILOGUE
        lua_table = L.execute(full_script)
    py = _lua_to_python(lua_table)
    if not isinstance(py, dict):
        raise RuntimeError(f"Expected skills table to be a dict, got {type(py).__name__}")
    return py


def parse_all_skill_files(skills_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Parse every *.lua file in a PoB2 src/Data/Skills/ directory.

    Returns a merged dict { skill_id -> data } across all files. Later
    files override earlier ones on key collision (PoB doesn't do this in
    practice, but the contract is well-defined).
    """
    if not skills_dir.is_dir():
        raise NotADirectoryError(f"Not a directory: {skills_dir}")
    merged: Dict[str, Dict[str, Any]] = {}
    for lua_file in sorted(skills_dir.glob("*.lua")):
        logger.info(f"Parsing {lua_file.name}")
        try:
            parsed = parse_skills_lua(lua_file)
        except Exception as e:
            logger.error(f"Failed to parse {lua_file}: {e}")
            continue
        merged.update(parsed)
    return merged


def extract_canonical_subset(skill: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a full skill record to the rich-numeric subset Issue #119 asks for.

    Drops verbose fields (description, color, etc.) and keeps the data
    that the v1 extractor doesn't already carry:
      * castTime
      * skillTypes (set of type-name strings)
      * qualityStats
      * levels (per-level cost / critChance / levelRequirement)
      * statSets[*].{label, baseEffectiveness, incrementalEffectiveness,
        damageIncrementalEffectiveness, baseFlags, constantStats, stats,
        levels}
    """
    out: Dict[str, Any] = {}
    for field in ("name", "baseTypeName", "castTime"):
        if field in skill:
            out[field] = skill[field]

    # skillTypes: the original is {SkillType.Spell = true, ...}; after
    # the prelude stub it's {"Spell" = true, ...}. Flatten to a list.
    st = skill.get("skillTypes")
    if isinstance(st, dict):
        out["skillTypes"] = sorted(k for k, v in st.items() if v)
    elif isinstance(st, list):
        out["skillTypes"] = list(st)

    for field in ("qualityStats", "levels", "statSets"):
        if field in skill:
            out[field] = skill[field]

    return out
