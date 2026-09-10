"""Knowledge store: search, get, structured lookups, contamination lint."""
import json
from pathlib import Path

import pytest

from src.knowledge.store import KnowledgeStore, _chunks_of, chunk_hash

DATA = Path(__file__).resolve().parent.parent / "data" / "knowledge"
pytestmark = pytest.mark.skipif(not (DATA / "docs.jsonl").exists(), reason="knowledge data not built")


@pytest.fixture(scope="module")
def store(tmp_path_factory, monkeypatch_module=None):
    import os
    os.environ["POE2_MCP_CACHE"] = str(tmp_path_factory.mktemp("cache"))
    return KnowledgeStore(DATA)


def test_manifest_matches_docs(store):
    man = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    assert man["docs"] == len(store.docs())
    assert man["docs"] > 2000
    assert man["principles"] >= 90


def test_every_doc_has_required_fields(store):
    for d in store.docs().values():
        assert d["id"] and d["title"] and d["kind"] and d["tier"], d["id"]
        assert d["tier"] in {"canonical", "verified", "analysis", "creator", "dispute", "opinion", "unverified"}
        assert isinstance(d["sections"], list) and d["sections"], d["id"]


def test_search_returns_tiered_hits(store):
    hits = store.search("fracturing orb random mod 4 mod minimum", limit=5, semantic=False)
    assert hits, "no hits"
    assert all(h.tier in {"canonical", "verified", "analysis", "creator", "dispute"} for h in hits)
    assert any("fractur" in (h.snippet + h.title).lower() for h in hits)


def test_search_excludes_unverified_by_default(store):
    default = store.search("Blood Mage", limit=20, semantic=False)
    assert all(h.tier != "unverified" for h in default)
    widened = store.search("Blood Mage", limit=20, tiers=["unverified"], semantic=False)
    assert widened and all(h.tier == "unverified" for h in widened)


def test_search_filters(store):
    hits = store.search("profit recipe", limit=10, slot="helmet", semantic=False)
    assert hits and all("helmet" in (h.slots or []) for h in hits)
    hits = store.search("crafting", limit=10, channel="Keyson", semantic=False)
    assert hits and all(h.channel == "Keyson" for h in hits)


def test_get_video_sections(store):
    d = store.get("video/mA6cjs1frao")
    assert d and d["kind"] == "video_guide" and "Economics" in d["sections_available"]
    econ = store.get("video/mA6cjs1frao", section="Economics")
    assert "divines" in econ["text"].lower()
    tr = store.get("mA6cjs1frao", section="transcript")   # forgiving id
    assert tr and len(tr["text"]) > 1000


def test_structured_lookups(store):
    assert store.mod_priorities("boots")["rows"]
    assert any("Omen of Light" == t["name"] for t in store.terms("light"))
    ps = store.principles(channel="Keyson")
    assert ps and all(p["cites"] for p in ps)


def test_contamination_lint(store):
    hits = store.contamination_check("Just scour it and metacraft prefixes cannot be changed, Chieftain style.")
    terms = {h["term"].lower() for h in hits}
    assert "scour" in terms and "prefixes cannot be changed" in terms and "chieftain" in terms
    assert not store.contamination_check("Use Omen of Whittling then Fracturing Orb.")


def test_chunker_is_stable():
    text = "para one\n\n" + ("x" * 900) + "\n\n" + ("y" * 900)
    parts = list(_chunks_of(text, 1000))
    assert len(parts) == 2 and all(len(p) <= 1000 for p in parts)
    assert chunk_hash("abc") == chunk_hash("abc") and chunk_hash("abc") != chunk_hash("abd")
