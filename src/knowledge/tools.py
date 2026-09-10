"""MCP tool definitions + handlers for the crafting knowledge store.

Kept out of mcp_server.py so the giant file only needs a registration hook. Every handler
returns Markdown that is compact by construction: ranked hits with ids, not documents.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from mcp import types

from .store import DEFAULT_TIERS, TIER_HELP, get_store

KINDS = ["video_guide", "recipe", "fact", "canonical", "mechanic", "crafting_spec", "crafting_doc", "guide",
         "video_digest", "dispute", "opinion", "economy", "patch_note", "doc"]
TIERS = list(TIER_HELP)


def tool_definitions() -> List[types.Tool]:
    return [
        types.Tool(
            name="knowledge_search",
            description=(
                "Search the PoE2 crafting knowledge base (curated KB + 82 creator video guides + slot priorities). "
                "Returns ranked snippets with a trust tier (canonical > verified > analysis > creator > dispute > opinion > unverified) "
                "and a doc_id to pass to knowledge_get. Use for: how a currency/omen/mechanic works, how to craft a slot profitably, "
                "what sells, EV/cost reasoning, market behaviour. Prices in results are dated snapshots, never current."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language question or keywords"},
                    "limit": {"type": "integer", "default": 8, "minimum": 1, "maximum": 25},
                    "kinds": {"type": "array", "items": {"type": "string", "enum": KINDS}, "description": "Restrict to document kinds"},
                    "tiers": {"type": "array", "items": {"type": "string", "enum": TIERS},
                              "description": f"Trust tiers to include. Default {list(DEFAULT_TIERS)} (excludes opinion/unverified)."},
                    "slot": {"type": "string", "description": "Item slot filter, e.g. ring, amulet, boots, helmet, body, wand, staff, quarterstaff, focus, jewel, tablet"},
                    "channel": {"type": "string", "description": "Creator filter: ASaVeQ, Keyson, LilBotQ, XTheFarmerX"},
                    "patch": {"type": "string", "enum": ["0.4", "0.5"]},
                    "include_transcripts": {"type": "boolean", "default": False, "description": "Also search raw video transcripts (noisy)"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="knowledge_get",
            description="Fetch a knowledge document by doc_id (from knowledge_search / knowledge_list). Optionally one section by heading substring, or 'transcript' for a video's full transcript.",
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string"},
                    "section": {"type": "string", "description": "Heading substring, e.g. 'Method', 'Economics', 'transcript'"},
                    "max_chars": {"type": "integer", "default": 12000},
                },
                "required": ["doc_id"],
            },
        ),
        types.Tool(
            name="knowledge_list",
            description="List knowledge documents by kind/tier/slot/patch/channel without searching. Good for 'what recipes exist for rings' or 'list Keyson's videos'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "kinds": {"type": "array", "items": {"type": "string", "enum": KINDS}},
                    "tiers": {"type": "array", "items": {"type": "string", "enum": TIERS}},
                    "slot": {"type": "string"}, "patch": {"type": "string", "enum": ["0.4", "0.5"]}, "channel": {"type": "string"},
                    "limit": {"type": "integer", "default": 40},
                },
            },
        ),
        types.Tool(
            name="crafting_principles",
            description="Cross-video crafting principles (92) distilled per creator and theme: market behaviour & pricing, EV & bankroll, batch discipline, league timing, sourcing bases. Each carries the creator's evidence and the video ids it came from.",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "ASaVeQ | Keyson | LilBotQ | XTheFarmerX"},
                    "theme": {"type": "string", "description": "Substring of a theme, e.g. 'pricing', 'EV', 'batch', 'timing', 'bases'"},
                    "query": {"type": "string", "description": "Substring filter on the principle text"},
                },
            },
        ),
        types.Tool(
            name="mod_priorities",
            description="Which mods and bases make a rare SELL, slot by slot (LilBotQ's 0.5 reference reconciled with Keyson): where value lives (prefix vs suffix), top mods, base to hunt, dead mods. Pass a slot or omit for the whole table.",
            inputSchema={"type": "object", "properties": {"slot": {"type": "string", "description": "e.g. boots, helmet, body, gloves, belt, ring, amulet, martial, caster"}}},
        ),
        types.Tool(
            name="crafting_term",
            description="Look up a crafting term in the canonical allowlists (omens, essences, catalysts, currencies, waystones). Returns effect text from tier-1 sources. Use to verify a name exists in PoE2 before recommending it.",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string"}, "family": {"type": "string", "enum": ["omens", "essences", "catalysts", "currencies", "waystones", "charges", "tablets"]}},
                "required": ["query"],
            },
        ),
        types.Tool(
            name="check_poe1_contamination",
            description="Lint a draft answer for PoE1-only terms (Orb of Scouring, Chieftain, Harvest, metacrafting, PoE1 essence names, PoE1 base names...). Returns each offending term with the PoE2 correction. Run this on any PoE2 crafting answer before sending it.",
            inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        ),
        types.Tool(
            name="knowledge_overview",
            description="What the crafting knowledge base contains, what the trust tiers mean, and how to use the knowledge_* tools well. Call once per session before relying on the KB.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


def _md_hits(hits, query: str) -> str:
    if not hits:
        return f"No knowledge hits for '{query}'. Try broader wording, `tiers` including 'unverified', or `include_transcripts`."
    lines = [f"# Knowledge hits for '{query}'  ({len(hits)})", ""]
    for i, h in enumerate(hits, 1):
        tags = [h.tier, h.kind] + ([h.channel] if h.channel else []) + ([f"patch {h.patch}"] if h.patch else [])
        lines.append(f"**{i}. {h.title}**  `{h.doc_id}`  _{' · '.join(tags)}_")
        if h.heading and h.heading != "transcript":
            lines.append(f"   section: {h.heading}")
        lines.append(f"   {h.snippet}")
        lines.append("")
    lines.append("Tier key: canonical=tier-1 data · verified=confirmed fact · analysis=costed method · creator=video claim · dispute=sources disagree.")
    lines.append("Fetch a full doc with knowledge_get(doc_id[, section]). Prices in these docs are dated snapshots.")
    return "\n".join(lines)


async def handle(name: str, args: Dict[str, Any]) -> List[types.TextContent]:
    s = get_store()
    if not s.available:
        return [types.TextContent(type="text", text="Knowledge base not present (data/knowledge/docs.jsonl missing). Run scripts/build_knowledge.py.")]

    if name == "knowledge_search":
        # First call: load numpy/onnxruntime + open the FTS index on the MAIN thread. Importing
        # native extensions for the first time inside a worker thread deadlocks on Windows
        # (loader lock vs. the asyncio/anyio threads). After that, searches run off-loop.
        if not _warm.get("done"):
            s._connect()
            s._ensure_embeddings()
            _warm["done"] = True
        hits = await asyncio.to_thread(
            s.search, args["query"], limit=int(args.get("limit", 8)), kinds=args.get("kinds"), tiers=args.get("tiers"),
            slot=args.get("slot"), channel=args.get("channel"), patch=args.get("patch"),
            include_transcripts=bool(args.get("include_transcripts", False)))
        return [types.TextContent(type="text", text=_md_hits(hits, args["query"]))]

    if name == "knowledge_get":
        d = s.get(args["doc_id"], section=args.get("section"), max_chars=int(args.get("max_chars", 12000)))
        if not d:
            return [types.TextContent(type="text", text=f"No document '{args['doc_id']}'.")]
        head = [f"# {d['title']}", f"`{d['id']}` · tier **{d['tier']}** · {d['kind']}" + (f" · {d['channel']}" if d.get("channel") else "") + (f" · patch {d['patch']}" if d.get("patch") else "")]
        if d.get("source"):
            head.append(f"source: {d['source']}")
        if d.get("sections_available"):
            head.append("sections: " + ", ".join(d["sections_available"]))
        return [types.TextContent(type="text", text="\n".join(head) + "\n\n" + d["text"])]

    if name == "knowledge_list":
        rows = s.list_docs(kinds=args.get("kinds"), tiers=args.get("tiers"), slot=args.get("slot"), patch=args.get("patch"),
                           channel=args.get("channel"), limit=int(args.get("limit", 40)))
        if not rows:
            return [types.TextContent(type="text", text="No documents match.")]
        lines = [f"# {len(rows)} documents", ""]
        for r in rows:
            extra = " · ".join(x for x in [r["tier"], r["kind"], r.get("channel"), f"patch {r['patch']}" if r.get("patch") else None, "tests" if r.get("has_tests") else None] if x)
            lines.append(f"- **{r['title']}**  `{r['doc_id']}`  _{extra}_" + (f"\n  {r['summary']}" if r.get("summary") else ""))
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "crafting_principles":
        items = s.principles(channel=args.get("channel"), theme=args.get("theme"), query=args.get("query"))
        if not items:
            return [types.TextContent(type="text", text="No principles match.")]
        lines = [f"# Crafting principles ({len(items)})", ""]
        cur = None
        for p in items:
            key = f"{p['channel']} — {p['theme']}"
            if key != cur:
                lines.append(f"\n## {key}")
                cur = key
            cites = ", ".join(f"video/{c}" for c in p["cites"][:4])
            lines.append(f"- **{p['claim']}** {p['detail']}" + (f"  _(sources: {cites})_" if cites else ""))
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "mod_priorities":
        m = s.mod_priorities(args.get("slot"))
        lines = [f"# Modifier priorities by slot" + (f" — {args['slot']}" if args.get("slot") else ""), f"_source: {m.get('source', 'LilBotQ, 0.5')}_", ""]
        for r in m["rows"]:
            lines.append(f"**{r.get('slot')}** — value lives in: {r.get('value-lives-in')}\n  top mods: {r.get('top-mod-s')}\n  base to hunt: {r.get('base-to-hunt')}")
        if m.get("notes"):
            lines.append("\n## Notes")
            for n in m["notes"]:
                if n.get("slot"):
                    lines.append(f"- **{n['slot']}**: {n['text']}")
                else:
                    lines.append(f"- {n['text']}")
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "crafting_term":
        hits = s.terms(args["query"], family=args.get("family"))
        if not hits:
            return [types.TextContent(type="text", text=f"'{args['query']}' is not in the canonical allowlists. If you were about to recommend it, verify it exists in PoE2 (check_poe1_contamination / poe2db) first.")]
        lines = [f"# Canonical terms matching '{args['query']}'", ""]
        for t in hits:
            lines.append(f"- **{t['name']}** ({t['family']}): {t['effect']}" + (f"  ⚠ {t['note']}" if t.get("note") else ""))
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "check_poe1_contamination":
        hits = s.contamination_check(args["text"])
        if not hits:
            return [types.TextContent(type="text", text="No PoE1-only terms detected.")]
        lines = [f"# {len(hits)} PoE1 contamination hit(s)", ""]
        for h in hits:
            lines.append(f"- **{h['term']}** [{h.get('category')}]: {h.get('note')}")
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "knowledge_overview":
        o = s.overview()
        lines = ["# PoE2 crafting knowledge base", "",
                 f"{o.get('docs')} documents · {o.get('principles')} principles · built {o.get('built_at')}",
                 f"by kind: {json.dumps(o.get('by_kind'))}", f"by tier: {json.dumps(o.get('by_tier'))}", "",
                 "## Trust tiers"] + [f"- **{k}**: {v}" for k, v in o["tiers"].items()] + [
                 "", f"Default search tiers: {o['default_search_tiers']} (pass `tiers` to include opinion/unverified).",
                 f"Semantic search: {o['semantic_search']}", "",
                 "## How to use",
                 "1. knowledge_search for the question; read the tier on each hit.",
                 "2. knowledge_get(doc_id, section) for the one or two docs that matter (recipes have Method / Economics sections; videos have TL;DR / Key numbers / Method / Economics / Notes / transcript).",
                 "3. mod_priorities(slot) before recommending what to craft; crafting_principles for market/EV rules.",
                 "4. crafting_term to confirm an omen/essence/currency name exists; check_poe1_contamination on your draft answer.",
                 "5. Treat every divine/exalt figure as a dated snapshot; use live prices (poe.ninja) for current numbers.",
                 "", o["patch_note"]]
        return [types.TextContent(type="text", text="\n".join(lines))]

    return [types.TextContent(type="text", text=f"Unknown knowledge tool {name}")]


TOOL_NAMES = {t.name for t in tool_definitions()}
_warm: Dict[str, bool] = {}
