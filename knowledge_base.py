"""Unstructured knowledge: load the markdown docs, chunk them, and search them semantically.

Two interchangeable backends behind one `KnowledgeIndex` class:
  chroma  - ChromaDB persistent vector store + sentence-transformers 'all-MiniLM-L6-v2' (default)
  simple  - tiny pure-Python TF-IDF cosine search (no downloads; used by tests / when chroma is absent)
Pick with NEXUS_INDEX=chroma|simple. If unset: chroma when importable, otherwise simple (with a warning).

Each doc starts with a small front-matter block:
    doc_id / product_line / asserts   (asserts = machine-readable claim, e.g. auto_debit_permitted=true)
The `asserts` value is what lets the Retriever detect a conflict with live structured data.
"""
import math
import os
import re
from collections import Counter
from pathlib import Path

from errors import InvalidArgument, NotFound
from logging_config import get_logger, trace

logger = get_logger("knowledge_base")

KB_DIRS = ["policy_docs", "product_docs", "faqs", "communication_templates"]
COLLECTION = "meridian_kb"
_SAFE_DOC_ID = re.compile(r"^[A-Z0-9_]{3,60}$")


def kb_root():
    return Path(os.getenv("NEXUS_KB_DIR", Path(__file__).parent / "knowledge"))


def chroma_path():
    return os.getenv("NEXUS_CHROMA_DIR", str(Path(__file__).parent / "chroma_store"))


# ------------------------------------------------------------------ loading + chunking
def _parse_file(path):
    raw = path.read_text(encoding="utf-8")
    meta, body = {}, raw
    if raw.startswith("---"):
        _, front, body = raw.split("---", 2)
        for line in front.strip().splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, body.strip()


@trace(logger)
def load_chunks():
    """Every '## section' of every doc becomes one chunk carrying its doc's metadata."""
    chunks = []
    for kind in KB_DIRS:
        for path in sorted((kb_root() / kind).glob("*.md")):
            meta, body = _parse_file(path)
            doc_id = meta.get("doc_id", path.stem)
            sections = [s.strip() for s in re.split(r"(?m)^## ", body) if s.strip()]
            for n, section in enumerate(sections):
                heading = section.splitlines()[0]
                chunks.append({
                    "chunk_id": f"{doc_id}#{n}", "doc_id": doc_id, "kind": kind,
                    "product_line": meta.get("product_line", "all"),
                    "asserts": meta.get("asserts", ""), "section": heading, "text": section,
                })
    return chunks


@trace(logger)
def find_document(doc_id):
    """Return (kind, full text) for one document. Used by the knowledgebase:// resource."""
    if not _SAFE_DOC_ID.match(doc_id or ""):
        raise InvalidArgument("document_id must look like FAQ_AUTO_DEBIT", code="INVALID_DOCUMENT_ID")
    for kind in KB_DIRS:
        for path in (kb_root() / kind).glob("*.md"):
            meta, body = _parse_file(path)
            if meta.get("doc_id", path.stem) == doc_id:
                return kind, body
    raise NotFound(f"Document {doc_id} not found", code="DOCUMENT_NOT_FOUND")


def parse_asserts(chunk):
    """'auto_debit_permitted=true' -> [('auto_debit_permitted', True)]"""
    out = []
    for item in filter(None, (chunk.get("asserts") or "").split(",")):
        key, _, value = item.strip().partition("=")
        out.append((key, value.strip().lower() == "true"))
    return out


# ------------------------------------------------------------------ simple backend
_STOP = set("a an the and or of to in on for is are can i my me it be do does at by with from this that as we you your "
            "have has will if not no yes".split())


def _tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP]


class _SimpleBackend:
    def __init__(self, chunks):
        self.chunks = chunks
        docs = [Counter(_tokens(c["text"])) for c in chunks]
        df = Counter(t for d in docs for t in d)
        self.idf = {t: math.log((1 + len(docs)) / (1 + n)) + 1 for t, n in df.items()}
        self.vecs = [self._vec(d) for d in docs]

    def _vec(self, counts):
        v = {t: n * self.idf.get(t, 1.0) for t, n in counts.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def search(self, query, lines, k):
        q = self._vec(Counter(_tokens(query)))
        scored = []
        for chunk, vec in zip(self.chunks, self.vecs):
            if lines and chunk["product_line"] not in lines:
                continue
            scored.append((sum(w * vec.get(t, 0.0) for t, w in q.items()), chunk))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [dict(c, score=round(s, 3)) for s, c in scored[:k]]


# ------------------------------------------------------------------ chroma backend
def _chroma_collection(create_embedding=True):
    import chromadb
    from chromadb.utils import embedding_functions

    client = chromadb.PersistentClient(path=chroma_path())
    embed = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    return client, client.get_or_create_collection(COLLECTION, embedding_function=embed,
                                                    metadata={"hnsw:space": "cosine"})


class _ChromaBackend:
    def __init__(self, chunks, rebuild=False):
        self.client, self.collection = _chroma_collection()
        if rebuild or self.collection.count() != len(chunks):
            self.client.delete_collection(COLLECTION)
            self.client, self.collection = _chroma_collection()
            self.collection.add(
                ids=[c["chunk_id"] for c in chunks],
                documents=[c["text"] for c in chunks],
                metadatas=[{k: c[k] for k in ("doc_id", "kind", "product_line", "asserts", "section")} for c in chunks],
            )
        self.by_id = {c["chunk_id"]: c for c in chunks}

    def search(self, query, lines, k):
        where = {"product_line": {"$in": sorted(lines | {"all"})}} if lines else None
        res = self.collection.query(query_texts=[query], n_results=k, where=where)
        return [dict(self.by_id[cid], score=round(1 - dist, 3))
                for cid, dist in zip(res["ids"][0], res["distances"][0])]


# ------------------------------------------------------------------ public class
def _chroma_available():
    try:
        import chromadb  # noqa: F401
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


class KnowledgeIndex:
    def __init__(self, backend=None, rebuild=False):
        chosen = backend or os.getenv("NEXUS_INDEX")
        if not chosen:
            chosen = "chroma" if _chroma_available() else "simple"
            if chosen == "simple":
                logger.warning("chromadb/sentence-transformers not installed - using the simple fallback index")
        self.backend_name = chosen
        chunks = load_chunks()
        self.chunk_count = len(chunks)
        self._backend = _ChromaBackend(chunks, rebuild) if chosen == "chroma" else _SimpleBackend(chunks)

    @trace(logger)
    def search(self, query, product_lines=None, k=5):
        """Semantic search. `product_lines` (e.g. {'insurance','banking'}) filters by metadata;
        chunks tagged 'all' are always eligible."""
        lines = (set(product_lines) | {"all"}) if product_lines else None
        min_score = float(os.getenv("NEXUS_MIN_SCORE", "0.05"))  # drop irrelevant chunks
        return [h for h in self._backend.search(query, lines, k) if h["score"] >= min_score]
