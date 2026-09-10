"""Crafting knowledge store: search + structured access over data/knowledge/.

Design notes (how an agent wants to use this):
  * search() returns a short ranked list of {doc_id, title, tier, kind, snippet, ...}. Never a wall
    of text. The agent picks a doc_id and calls get() for the full thing.
  * Every hit carries a trust tier so the caller can tell canonical data from a creator's claim.
  * Transcripts are indexed but excluded by default; they are noisy and long.
  * Search is hybrid: SQLite FTS5 BM25 always; if `fastembed` is installed, a semantic pass is
    fused in (reciprocal rank fusion). Both indexes live in a per-user cache and are rebuilt when
    docs.jsonl changes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Load the native extensions at import time, on the main thread, before the MCP server starts
# its event loop and helper threads. On Windows, first-importing numpy/onnxruntime after those
# threads exist deadlocks on the DLL loader lock (observed as a hang in create_module).
try:  # pragma: no cover - environment dependent
    import numpy as _np  # noqa: F401
except Exception:  # numpy missing: semantic search stays off, FTS still works
    _np = None
try:  # pragma: no cover
    import onnxruntime as _ort  # noqa: F401
except Exception:
    _ort = None

ROOT = Path(__file__).resolve().parent.parent.parent
KNOWLEDGE_DIR = Path(os.environ.get("POE2_KNOWLEDGE_DIR", ROOT / "data" / "knowledge"))
DEFAULT_TIERS = ("canonical", "verified", "analysis", "creator", "dispute")   # excludes unverified/opinion
TIER_ORDER = {"canonical": 0, "verified": 1, "analysis": 2, "creator": 3, "dispute": 4, "opinion": 5, "unverified": 6}
TIER_HELP = {
    "canonical": "tier-1 game data (poe2db / GGG / poe.ninja allowlists)",
    "verified": "atomic fact confirmed against a tier-1 source or in-game observation",
    "analysis": "derived analysis / costed method authored from verified inputs",
    "creator": "a content creator's claim, distilled from a video transcript",
    "dispute": "sources disagree; the doc records each side",
    "opinion": "creator or community opinion",
    "unverified": "a claim awaiting tier-1 confirmation; treat as a lead, not a fact",
}
CHUNK_CHARS = 1400
EMBED_MODEL = "BAAI/bge-small-en-v1.5"


def _cache_dir() -> Path:
    base = os.environ.get("POE2_MCP_CACHE") or (
        Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "poe2-mcp")
    p = Path(base) / "knowledge"
    p.mkdir(parents=True, exist_ok=True)
    return p


def chunk_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _chunks_of(text: str, limit: int = CHUNK_CHARS) -> Iterable[str]:
    """Split on paragraph boundaries into pieces of at most `limit` chars."""
    if len(text) <= limit:
        yield text
        return
    buf = ""
    for para in re.split(r"\n\s*\n", text):
        if len(buf) + len(para) + 2 > limit and buf:
            yield buf
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
        while len(buf) > limit:              # a single huge paragraph
            yield buf[:limit]
            buf = buf[limit:]
    if buf:
        yield buf


@dataclass
class Hit:
    doc_id: str
    chunk_id: int
    title: str
    kind: str
    tier: str
    heading: str
    snippet: str
    score: float
    source: str
    channel: Optional[str] = None
    patch: Optional[str] = None
    slots: Optional[List[str]] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], "")}


class KnowledgeStore:
    def __init__(self, data_dir: Path = KNOWLEDGE_DIR):
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._db: Optional[sqlite3.Connection] = None
        self._docs: Optional[Dict[str, dict]] = None
        self._json_cache: Dict[str, Any] = {}
        self._emb = None          # (ids: list[str], vectors: np.ndarray) once loaded
        self._embedder = None
        self._embed_state = "unknown"   # unknown | ready | unavailable

    # ------------------------------------------------------------------ loading
    @property
    def available(self) -> bool:
        return (self.data_dir / "docs.jsonl").exists()

    def _load_json(self, name: str, default):
        if name not in self._json_cache:
            p = self.data_dir / name
            try:
                self._json_cache[name] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._json_cache[name] = default
        return self._json_cache[name]

    def docs(self) -> Dict[str, dict]:
        if self._docs is None:
            out = {}
            p = self.data_dir / "docs.jsonl"
            if p.exists():
                with p.open(encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            d = json.loads(line)
                            out[d["id"]] = d
            self._docs = out
        return self._docs

    def _docs_fingerprint(self) -> str:
        p = self.data_dir / "docs.jsonl"
        st = p.stat()
        return hashlib.sha1(f"{st.st_size}:{int(st.st_mtime)}:v2".encode()).hexdigest()[:16]

    def _connect(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db
        with self._lock:
            if self._db is not None:
                return self._db
            fp = self._docs_fingerprint()
            db_path = _cache_dir() / f"index-{fp}.sqlite"
            fresh = not db_path.exists()
            con = sqlite3.connect(str(db_path), check_same_thread=False)
            if fresh:
                logger.info("Building knowledge FTS index at %s", db_path)
                self._build_fts(con)
                for old in _cache_dir().glob("index-*.sqlite"):
                    if old != db_path:
                        try:
                            old.unlink()
                        except OSError:
                            pass
            self._db = con
            return con

    def iter_chunks(self):
        """Yield (doc_id, heading, text, is_transcript) exactly as indexed."""
        for d in self.docs().values():
            for sec in d.get("sections", []):
                for piece in _chunks_of(sec["text"]):
                    yield d["id"], sec.get("heading", ""), piece, 0
            if d.get("transcript"):
                for piece in _chunks_of(d["transcript"], 2000):
                    yield d["id"], "transcript", piece, 1

    def _build_fts(self, con: sqlite3.Connection) -> None:
        con.executescript("""
            CREATE TABLE chunks(id INTEGER PRIMARY KEY, doc_id TEXT, heading TEXT, text TEXT, is_transcript INTEGER);
            CREATE VIRTUAL TABLE chunks_fts USING fts5(title, heading, text, doc_id UNINDEXED, content='', tokenize='porter unicode61');
            CREATE INDEX chunks_doc ON chunks(doc_id);
        """)
        titles = {k: v["title"] for k, v in self.docs().items()}
        rows = [(doc_id, heading, text, is_tr, titles[doc_id]) for doc_id, heading, text, is_tr in self.iter_chunks()]
        cur = con.cursor()
        for i, (doc_id, heading, text, is_tr, title) in enumerate(rows, start=1):
            cur.execute("INSERT INTO chunks(id, doc_id, heading, text, is_transcript) VALUES (?,?,?,?,?)", (i, doc_id, heading, text, is_tr))
            cur.execute("INSERT INTO chunks_fts(rowid, title, heading, text, doc_id) VALUES (?,?,?,?,?)", (i, title, heading, text, doc_id))
        con.commit()
        logger.info("Indexed %d chunks from %d docs", len(rows), len(self.docs()))

    # ------------------------------------------------------------------ embeddings (optional)
    # Corpus vectors are computed at BUILD time (scripts/build_knowledge.py --embed) and shipped as
    # data/knowledge/embeddings.npz keyed by chunk text hash. The server never embeds the corpus;
    # at query time it only embeds the query, and only if `fastembed` is importable.
    def _ensure_embeddings(self) -> bool:
        if self._embed_state == "unavailable":
            return False
        if self._embed_state == "ready":
            return True
        path = self.data_dir / "embeddings.npz"
        if not path.exists():
            self._embed_state = "unavailable"
            return False
        try:
            import numpy as np
            from fastembed import TextEmbedding
            z = np.load(path, allow_pickle=False)
            hashes = z["hashes"]                 # sha1 of chunk text, one per vector
            vecs = z["vecs"].astype(np.float32)
            con = self._connect()
            rows = con.execute("SELECT id, text FROM chunks WHERE is_transcript=0").fetchall()
            by_hash = {h: i for i, h in enumerate(hashes.tolist())}
            ids, keep = [], []
            for cid, text in rows:
                i = by_hash.get(chunk_hash(text))
                if i is not None:
                    ids.append(cid); keep.append(i)
            if len(ids) < 0.9 * len(rows):
                logger.warning("embeddings.npz is stale (%d/%d chunks matched); rebuild with --embed", len(ids), len(rows))
            if not ids:
                self._embed_state = "unavailable"
                return False
            self._emb = (np.array(ids, dtype=np.int64), vecs[keep])
            self._embedder = TextEmbedding(model_name=EMBED_MODEL)   # downloads a ~30 MB model once
            self._embed_state = "ready"
            return True
        except Exception as e:
            logger.info("Semantic search unavailable: %s", e)
            self._embed_state = "unavailable"
            return False

    def _semantic(self, query: str, k: int) -> List[tuple[int, float]]:
        if not self._ensure_embeddings():
            return []
        import numpy as np
        q = np.array(list(self._embedder.embed([query])), dtype=np.float32)[0]
        q /= np.linalg.norm(q) + 1e-9
        ids, vecs = self._emb
        sims = vecs @ q
        top = np.argsort(-sims)[:k]
        return [(int(ids[i]), float(sims[i])) for i in top]

    # ------------------------------------------------------------------ search
    @staticmethod
    def _fts_query(q: str) -> str:
        toks = re.findall(r"[A-Za-z0-9+%']+", q)
        toks = [t.replace("'", "''") for t in toks if len(t) > 1]
        return " OR ".join(f'"{t}"' for t in toks) if toks else '""'

    def search(self, query: str, limit: int = 8, kinds: Optional[List[str]] = None, tiers: Optional[List[str]] = None,
               slot: Optional[str] = None, channel: Optional[str] = None, patch: Optional[str] = None,
               include_transcripts: bool = False, semantic: bool = True) -> List[Hit]:
        if not self.available:
            return []
        con = self._connect()
        docs = self.docs()
        tiers = list(tiers) if tiers else list(DEFAULT_TIERS)
        k = max(limit * 6, 40)

        # BM25 candidates
        bm = con.execute(
            "SELECT rowid, bm25(chunks_fts, 4.0, 2.0, 1.0) AS s FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY s LIMIT ?",
            (self._fts_query(query), k)).fetchall()
        ranks: Dict[int, float] = {}
        for r, (cid, _) in enumerate(bm):
            ranks[cid] = ranks.get(cid, 0) + 1.0 / (60 + r)
        if semantic:
            for r, (cid, _) in enumerate(self._semantic(query, k)):
                ranks[cid] = ranks.get(cid, 0) + 1.0 / (60 + r)
        if not ranks:
            return []

        ids = list(ranks)
        rows = con.execute(f"SELECT id, doc_id, heading, text, is_transcript FROM chunks WHERE id IN ({','.join('?'*len(ids))})", ids).fetchall()
        hits: List[Hit] = []
        seen_docs: Dict[str, int] = {}
        for cid, doc_id, heading, text, is_tr in sorted(rows, key=lambda r: -ranks[r[0]]):
            d = docs.get(doc_id)
            if not d or d["tier"] not in tiers:
                continue
            if is_tr and not include_transcripts:
                continue
            if kinds and d["kind"] not in kinds:
                continue
            if slot and slot.lower() not in [s.lower() for s in d.get("slots", [])]:
                continue
            if channel and (d.get("channel") or "").lower() != channel.lower():
                continue
            if patch and d.get("patch") != patch:
                continue
            if seen_docs.get(doc_id, 0) >= 1:      # one chunk per doc: the agent fetches the rest with get()
                continue
            seen_docs[doc_id] = seen_docs.get(doc_id, 0) + 1
            hits.append(Hit(doc_id=doc_id, chunk_id=cid, title=d["title"], kind=d["kind"], tier=d["tier"], heading=heading,
                            snippet=self._snippet(text, query), score=round(ranks[cid], 5), source=d.get("source", ""),
                            channel=d.get("channel"), patch=d.get("patch"), slots=d.get("slots") or None))
            if len(hits) >= limit:
                break
        return hits

    @staticmethod
    def _snippet(text: str, query: str, width: int = 420) -> str:
        toks = [t.lower() for t in re.findall(r"[A-Za-z0-9+%]+", query) if len(t) > 2]
        low = text.lower()
        pos = min((low.find(t) for t in toks if low.find(t) >= 0), default=0)
        start = max(0, pos - width // 3)
        snip = text[start:start + width].strip()
        return ("…" if start > 0 else "") + re.sub(r"\s+", " ", snip) + ("…" if start + width < len(text) else "")

    # ------------------------------------------------------------------ documents
    def get(self, doc_id: str, section: Optional[str] = None, max_chars: int = 12000) -> Optional[dict]:
        d = self.docs().get(doc_id)
        if d is None:
            # forgiving lookup: video id or suffix match
            for k, v in self.docs().items():
                if k.endswith("/" + doc_id) or v.get("video_id") == doc_id:
                    d = v
                    break
        if d is None:
            return None
        meta = {k: v for k, v in d.items() if k not in ("sections", "transcript")}
        if section and section.lower() == "transcript":
            text = d.get("transcript") or ""
        elif section:
            secs = [s for s in d["sections"] if section.lower() in (s.get("heading") or "").lower()]
            text = "\n\n".join(f"## {s['heading']}\n{s['text']}" if s["heading"] else s["text"] for s in secs)
        else:
            text = "\n\n".join(f"## {s['heading']}\n{s['text']}" if s["heading"] else s["text"] for s in d["sections"])
        truncated = len(text) > max_chars
        meta["sections_available"] = [s["heading"] for s in d["sections"] if s["heading"]] + (["transcript"] if d.get("transcript") else [])
        meta["text"] = text[:max_chars] + ("\n\n[truncated; request a section or raise max_chars]" if truncated else "")
        return meta

    def list_docs(self, kinds: Optional[List[str]] = None, tiers: Optional[List[str]] = None, slot: Optional[str] = None,
                  patch: Optional[str] = None, channel: Optional[str] = None, limit: int = 50) -> List[dict]:
        out = []
        for d in self.docs().values():
            if kinds and d["kind"] not in kinds:
                continue
            if tiers and d["tier"] not in tiers:
                continue
            if slot and slot.lower() not in [s.lower() for s in d.get("slots", [])]:
                continue
            if patch and d.get("patch") != patch:
                continue
            if channel and (d.get("channel") or "").lower() != channel.lower():
                continue
            out.append({"doc_id": d["id"], "title": d["title"], "kind": d["kind"], "tier": d["tier"], "patch": d.get("patch"),
                        "slots": d.get("slots") or None, "channel": d.get("channel"), "has_tests": d.get("has_tests"),
                        "summary": (d.get("summary") or "")[:240] or None})
        out.sort(key=lambda x: (TIER_ORDER.get(x["tier"], 9), x["title"]))
        return out[:limit]

    # ------------------------------------------------------------------ structured data
    def principles(self, channel: Optional[str] = None, theme: Optional[str] = None, query: Optional[str] = None) -> List[dict]:
        items = self._load_json("principles.json", [])
        q = (query or "").lower()
        out = []
        for p in items:
            if channel and p["channel"].lower() != channel.lower():
                continue
            if theme and theme.lower() not in (p.get("theme") or "").lower():
                continue
            if q and q not in (p["claim"] + " " + p["detail"]).lower():
                continue
            out.append(p)
        return out

    def mod_priorities(self, slot: Optional[str] = None) -> dict:
        m = self._load_json("mod_priorities.json", {"rows": [], "notes": []})
        if not slot:
            return m
        s = slot.lower()
        rows = [r for r in m["rows"] if s in (r.get("slot") or "").lower()]
        notes = [n for n in m["notes"] if n.get("slot") and s in n["slot"].lower()]
        return {"rows": rows, "notes": notes, "source": m.get("source")}

    def terms(self, query: Optional[str] = None, family: Optional[str] = None) -> List[dict]:
        t = self._load_json("terms.json", {})
        q = (query or "").lower()
        out = []
        for fam, entries in t.items():
            if family and fam != family:
                continue
            for e in entries:
                if not q or q in e["name"].lower() or q in e["effect"].lower():
                    out.append(e)
        return out

    def contamination_check(self, text: str) -> List[dict]:
        c = self._load_json("contamination.json", {})
        low = text.lower()
        hits = []
        for term, info in c.items():
            key = term.split(" = ")[0].split(" / ")[0].strip().lower()
            if len(key) < 3:
                continue
            if re.search(r"(?<![a-z])" + re.escape(key) + r"(?![a-z])", low):
                hits.append({"term": term, "category": info.get("category"), "note": info.get("note")})
        return hits

    def overview(self) -> dict:
        man = self._load_json("manifest.json", {})
        return {
            "available": self.available, "docs": man.get("docs"), "by_kind": man.get("by_kind"), "by_tier": man.get("by_tier"),
            "principles": man.get("principles"), "built_at": man.get("built_at"),
            "tiers": TIER_HELP, "default_search_tiers": list(DEFAULT_TIERS),
            "semantic_search": self._embed_state if self._embed_state != "unknown" else "not yet probed (needs optional `fastembed`)",
            "patch_note": "Mechanics generally hold across 0.5.x; every price/cost in a doc is a snapshot from the doc's date and is NOT current.",
        }


_store: Optional[KnowledgeStore] = None


def get_store() -> KnowledgeStore:
    global _store
    if _store is None:
        _store = KnowledgeStore()
    return _store
