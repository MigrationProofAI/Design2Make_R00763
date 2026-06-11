"""MCP server: semantic (vector) search over SAP material descriptions.

FAISS index + OpenAI embeddings, persisted to ../vector_store/. This is the
'find by MEANING' layer that substring/token search can't do:
  "gaming CPU" or "AMD Ryzen X9"  ->  "AMD Ryzen 7 9800X3D"

Build the index once with index_materials, then semantic_search. The engine is
generic — point it at lessons/logs later to back the memory layer (same code).
Scale path: swap FAISS -> Qdrant/pgvector behind these same two tools.
"""
import os
import sys
import json

import numpy as np
import faiss
from dotenv import load_dotenv
from openai import OpenAI
from mcp.server.fastmcp import FastMCP

# Reuse the SAP connection from the sibling sap.py (works standalone or imported).
sys.path.insert(0, os.path.dirname(__file__))
from sap import _sap_get, PRODUCT_SRV  # noqa: E402

load_dotenv()
client = OpenAI()
mcp = FastMCP("vector")

EMBED_MODEL = "text-embedding-3-small"
STORE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vector_store")
INDEX_PATH = os.path.join(STORE_DIR, "materials.faiss")
MAP_PATH = os.path.join(STORE_DIR, "materials.json")

_index = None          # faiss index (lazy-loaded)
_meta: list | None = None  # parallel list of {Product, Description}


def _embed(texts: list[str]) -> np.ndarray:
    """Embed texts with OpenAI and L2-normalize (so inner product == cosine)."""
    vecs = []
    for i in range(0, len(texts), 256):          # batch to stay within limits
        resp = client.embeddings.create(model=EMBED_MODEL, input=texts[i:i + 256])
        vecs.extend(d.embedding for d in resp.data)
    arr = np.array(vecs, dtype="float32")
    faiss.normalize_L2(arr)
    return arr


def _load() -> bool:
    """Lazy-load a persisted index + mapping from disk."""
    global _index, _meta
    if _index is None and os.path.exists(INDEX_PATH) and os.path.exists(MAP_PATH):
        _index = faiss.read_index(INDEX_PATH)
        with open(MAP_PATH, encoding="utf-8") as f:
            _meta = json.load(f)
    return _index is not None


@mcp.tool()
def index_materials(top: int = 3000, language: str = "EN") -> str:
    """Build/refresh the semantic index from SAP material descriptions.

    Pulls up to `top` descriptions in `language`, embeds them, and persists a FAISS
    index to vector_store/. Run this once (or after material data changes) before
    semantic_search.

    Args:
        top: max descriptions to index (default 3000).
        language: description language key (default "EN").
    """
    global _index, _meta
    raw = _sap_get(f"{PRODUCT_SRV}/A_ProductDescription",
                   {"$filter": f"Language eq '{language}'", "$top": top,
                    "$select": "Product,ProductDescription"})
    try:
        rows = json.loads(raw)["d"]["results"]
    except (json.JSONDecodeError, KeyError):
        return f"Could not load descriptions: {raw[:200]}"
    rows = [r for r in rows if r.get("ProductDescription", "").strip()]
    if not rows:
        return "No descriptions returned to index."

    vecs = _embed([r["ProductDescription"] for r in rows])
    index = faiss.IndexFlatIP(vecs.shape[1])     # cosine via normalized inner product
    index.add(vecs)

    _meta = [{"Product": r["Product"], "Description": r["ProductDescription"]} for r in rows]
    os.makedirs(STORE_DIR, exist_ok=True)
    faiss.write_index(index, INDEX_PATH)
    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(_meta, f, ensure_ascii=False)
    _index = index
    return f"Indexed {len(_meta)} materials ({language}). Stored at {INDEX_PATH}."


_DUP_THRESHOLD = float(os.getenv("DEDUP_THRESHOLD", "0.92"))   # cosine >= this => "the same material"


def _num(s):
    """A material number as int if it's all digits, else None (for numeric range compares)."""
    s = str(s).strip()
    return int(s) if s.isdigit() else None


def _in_range(product, lo, hi) -> bool:
    """Is `product` within the SAP-style material range [lo, hi]? Numeric compare when both the
    product and the given bound are numeric, else lexicographic. An empty/None bound is open."""
    lo = None if lo in (None, "") else lo
    hi = None if hi in (None, "") else hi
    if lo is None and hi is None:
        return True
    pn = _num(product)
    if lo is not None:
        ln = _num(lo)
        ge = (pn >= ln) if (pn is not None and ln is not None) else (str(product).strip() >= str(lo).strip())
        if not ge:
            return False
    if hi is not None:
        hn = _num(hi)
        le = (pn <= hn) if (pn is not None and hn is not None) else (str(product).strip() <= str(hi).strip())
        if not le:
            return False
    return True


def _search(query: str, k: int, lo=None, hi=None) -> list[dict]:
    """Cosine search, optionally scoped to material range [lo, hi]. The index is exact, so we
    rank ALL then range-filter then take k -- a sparse range still yields its true top-k."""
    if not _load() or not _meta:
        return []
    qv = _embed([query])
    scores, idx = _index.search(qv, len(_meta))
    out = []
    for score, i in zip(scores[0], idx[0]):
        if i < 0:
            continue
        m = _meta[i]
        if not _in_range(m["Product"], lo, hi):
            continue
        out.append({"Product": m["Product"], "Description": m["Description"], "score": round(float(score), 3)})
        if len(out) >= k:
            break
    return out


@mcp.tool()
def semantic_search(query: str, k: int = 5, from_material: str = "", to_material: str = "") -> str:
    """Search materials by MEANING (not substring), e.g. "gaming processor" or "AMD Ryzen X9"
    finds "AMD Ryzen 7 9800X3D". Complements search_materials (exact/substring). Call
    index_materials first.

    Args:
        query: natural-language description of what you're looking for.
        k: number of results (default 5).
        from_material, to_material: optional SAP-style material-number range to search WITHIN
            (e.g. from_material="10000000", to_material="19999999"). Empty = open on that side.
    """
    if not _load():
        return "No index yet — call index_materials first."
    res = _search(query, k, from_material, to_material)
    return json.dumps({"query": query,
                       "scope": {"from_material": from_material or None, "to_material": to_material or None},
                       "results": res}, indent=2)


@mcp.tool()
def find_duplicates(description: str, from_material: str = "", to_material: str = "",
                    k: int = 5, threshold: float = 0.0) -> str:
    """BEFORE creating a material, ask: does one with this MEANING already exist? Returns the
    closest existing materials with cosine scores (scoped to [from_material, to_material]) and a
    verdict `is_duplicate` (best score >= threshold). This is how we catch duplicates at the very
    start instead of after the fact.

    Args:
        description: the new material's description / name.
        from_material, to_material: SAP-style range to search within (empty = all).
        k: how many candidates to return (default 5).
        threshold: duplicate cutoff; 0 -> server default (DEDUP_THRESHOLD, currently
            %.2f).
    """ % _DUP_THRESHOLD
    thr = threshold if (threshold and threshold > 0) else _DUP_THRESHOLD
    res = _search(description, max(k, 1), from_material, to_material)
    best = res[0] if res else None
    is_dup = bool(best and best["score"] >= thr)
    return json.dumps({"description": description, "threshold": round(thr, 3),
                       "scope": {"from_material": from_material or None, "to_material": to_material or None},
                       "is_duplicate": is_dup, "match": (best if is_dup else None),
                       "candidates": res}, indent=2)


def _add_material(product: str, description: str):
    """Append one created material to the index NOW (write-back). Returns (ok, message)."""
    global _index, _meta
    description = (description or "").strip()
    if not product or not description:
        return False, "add_material: need both product and description."
    _load()
    if _meta is None:
        _meta = []
    if any(str(m.get("Product")) == str(product) for m in _meta):
        return True, f"{product} already in the semantic index."
    vec = _embed([description])                       # (1, d), L2-normalized
    if _index is None:
        _index = faiss.IndexFlatIP(vec.shape[1])
    _index.add(vec)
    _meta.append({"Product": str(product), "Description": description})
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        faiss.write_index(_index, INDEX_PATH)
        with open(MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(_meta, f, ensure_ascii=False)
    except Exception as e:                            # in-memory is enough for this session
        return True, f"added {product} to the in-memory index (persist failed: {e})."
    return True, f"added {product} to the semantic index ({len(_meta)} total)."


@mcp.tool()
def add_material(product: str, description: str) -> str:
    """Write a freshly-created material's description into the semantic index immediately, so the
    NEXT find_duplicates / semantic_search can already see it -- no full re-index needed. Genesis
    calls this after each create, so a repeat run recognises what the last run made."""
    return _add_material(product, description)[1]


if __name__ == "__main__":
    mcp.run(transport="stdio")
