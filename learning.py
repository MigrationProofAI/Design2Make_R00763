"""learning.py — the Minimal Viable Learning Loop (feature flag D2M_LEARNING).

Principle: CODE decides WHEN to remember; the LLM only supplies CONTENT (same rule as severity).

  Capture   a Reflector (ONE LLM call, strict JSON) fires deterministically on /reflect, on a
            "what did you learn" message, or on session close. Plus capture-on-correction: a user
            "no / wrong / should be" + an entity is logged as ground-truth hard_candidate.
  Store     lessons.jsonl (append-only; id, status active|retired, hit counters) + a FAISS 'lesson'
            index (separate from the material index, same embedding model).
  Recall    top-3 active lessons by similarity to (intent + object), filtered by applies_to,
            injected as a compact "LESSONS (past sessions)" block, hard-capped ~300 tokens.
  Measure   lesson_injected / mistake_avoided / mistake_repeated events -> a one-line "Learning"
            summary in the trace panel, and a before/after proof ledger.
  Promote   recurrence >= 2 or a correction -> promotion queue (human-gated, reversible diff).

knowledge.md stays HUMAN-curated; this module never writes it.
"""
import os
import re
import json
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    import faiss
    _FAISS = True
except Exception:                       # learning still works (recall degrades to substring) without faiss
    _FAISS = False

from openai import OpenAI

# ---- flag + config ---------------------------------------------------------
ENABLED = os.getenv("D2M_LEARNING", "0").lower() in ("1", "true", "yes", "on")
_HERE = Path(__file__).resolve().parent
LESSONS_FILE = _HERE / "lessons.jsonl"
PROMO_FILE = _HERE / "promotions.jsonl"
LEDGER_FILE = _HERE / "learning_ledger.jsonl"
_STORE = _HERE / "vector_store"
_INDEX_PATH = _STORE / "lessons.faiss"

_EMBED_MODEL = "text-embedding-3-small"
_REFLECT_MODEL = os.getenv("D2M_LEARNING_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
RECALL_TOKEN_CAP = int(os.getenv("D2M_LEARNING_TOKEN_CAP", "300"))
_DUP_SIM = 0.93                          # a new lesson this close to an existing one just bumps recurrence
_RECALL_FLOOR = float(os.getenv("D2M_RECALL_FLOOR", "0.38"))   # cosine floor: only inject genuinely relevant
# lessons. 0.45 starved the boardroom (its lessons score 0.43-0.46 vs board queries) while negatives sit at
# 0.20-0.30; intent-scoping (the pool filter) is what guards cross-intent leakage, so the floor can be lower.

_client = None                          # lazy -- importing this module must never need a key
_LESSON_TYPES = {"process", "method", "fact"}
_KINDS = {"soft", "hard_candidate"}


def _oai():
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _embed(texts: list[str]) -> np.ndarray:
    """OpenAI embeddings, L2-normalized so inner product == cosine (same as the material index)."""
    resp = _oai().embeddings.create(model=_EMBED_MODEL, input=texts)
    arr = np.array([d.embedding for d in resp.data], dtype="float32")
    if _FAISS:
        faiss.normalize_L2(arr)
    else:
        arr = arr / (np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9)
    return arr


# ---- store -----------------------------------------------------------------
_index = None          # faiss index over lesson texts (positionally aligned with the jsonl order)
_lessons: list | None = None


def _searchable(le: dict) -> str:
    a = le.get("applies_to") or {}
    return " ".join(str(x) for x in (le.get("context"), le.get("mistake_or_insight"),
                                     le.get("correction"), a.get("intent"), a.get("object_type"),
                                     a.get("task_type"), a.get("tool")) if x)


def _load():
    """Lazy-load lessons.jsonl + the FAISS lesson index (rebuilding the index if out of sync)."""
    global _index, _lessons
    if _lessons is not None:
        return
    _lessons = []
    if LESSONS_FILE.exists():
        for line in LESSONS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    _lessons.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if not _FAISS:
        return
    if _INDEX_PATH.exists():
        try:
            _index = faiss.read_index(str(_INDEX_PATH))
        except Exception:
            _index = None
    if _index is None or _index.ntotal != len(_lessons):   # (re)build from scratch
        _index = None
        if _lessons:
            vecs = _embed([_searchable(le) for le in _lessons])
            _index = faiss.IndexFlatIP(vecs.shape[1])
            _index.add(vecs)
            _persist_index()


def _persist_index():
    if _FAISS and _index is not None:
        _STORE.mkdir(exist_ok=True)
        faiss.write_index(_index, str(_INDEX_PATH))


def _rewrite():
    """Persist the whole lessons list (used after status/counter updates)."""
    LESSONS_FILE.write_text("".join(json.dumps(le, ensure_ascii=False) + "\n" for le in _lessons),
                            encoding="utf-8")


def _append(le: dict):
    """Append one lesson to jsonl + the FAISS index, keeping them positionally aligned."""
    global _index
    _lessons.append(le)
    with LESSONS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(le, ensure_ascii=False) + "\n")
    if _FAISS:
        v = _embed([_searchable(le)])
        if _index is None:
            _index = faiss.IndexFlatIP(v.shape[1])
        _index.add(v)
        _persist_index()


def _validate(raw: dict, *, source: str, default_intent: str = "") -> dict | None:
    """Coerce a Reflector item into a stored lesson; reject if it has no substance. Code (not the
    model) stamps id/status/counters/type-guards -- the model only supplies the content fields."""
    if not isinstance(raw, dict):
        return None
    text = " ".join(str(raw.get(k, "")).strip() for k in ("mistake_or_insight", "correction"))
    if len(text.strip()) < 8:
        return None
    a = raw.get("applies_to") or {}
    lt = str(raw.get("lesson_type", "")).lower()
    kind = str(raw.get("kind", "")).lower()
    try:
        conf = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    return {
        "id": "L-" + uuid.uuid4().hex[:8],
        "ts": _now(),
        "status": "active",
        "context": str(raw.get("context", ""))[:300],
        "mistake_or_insight": str(raw.get("mistake_or_insight", ""))[:300],
        "correction": str(raw.get("correction", ""))[:300],
        "applies_to": {"intent": str(a.get("intent", default_intent) or default_intent)[:30],
                       "tool": str(a.get("tool", ""))[:40],
                       "object_type": str(a.get("object_type", ""))[:30],
                       "task_type": str(a.get("task_type", ""))[:40]},
        "evidence": [str(e)[:40] for e in (raw.get("evidence") or [])][:8],
        "lesson_type": lt if lt in _LESSON_TYPES else "fact",
        "kind": kind if kind in _KINDS else "soft",
        "confidence": max(0.0, min(1.0, conf)),
        "hits": 0,
        "recurrence": 1,
        "source": source,
    }


def _dedup_or_add(le: dict) -> tuple[dict, str]:
    """If a near-identical active lesson exists, bump its recurrence (and maybe queue promotion);
    else add the new one. Returns (lesson, 'new'|'recurred')."""
    if _FAISS and _index is not None and _index.ntotal:
        qv = _embed([_searchable(le)])
        scores, idx = _index.search(qv, min(3, _index.ntotal))
        for s, i in zip(scores[0], idx[0]):
            if i >= 0 and s >= _DUP_SIM and _lessons[i].get("status") == "active":
                _lessons[i]["recurrence"] = _lessons[i].get("recurrence", 1) + 1
                _lessons[i]["ts"] = _now()
                _rewrite()
                _maybe_promote(_lessons[i], reason="recurrence")
                return _lessons[i], "recurred"
    _append(le)
    if le["kind"] == "hard_candidate":
        _maybe_promote(le, reason="correction")
    return le, "new"


# ---- capture: Reflector + corrections --------------------------------------
_REFLECT_PROMPT = (
    "You distill durable LESSONS from one SAP-agent session so future runs avoid the same mistakes.\n"
    "You ONLY supply content; code decides what to keep. Be specific and reusable, NOT a play-by-play.\n"
    "Return STRICT JSON: {\"lessons\":[{\n"
    '  "context": "<when this applies>",\n'
    '  "mistake_or_insight": "<the mistake made OR the insight>",\n'
    '  "correction": "<the durable rule to follow next time>",\n'
    '  "applies_to": {"intent":"search|create_change|make|genesis|planning|boardroom|validate",'
    '"tool":"<tool or \\"\\">","object_type":"<material|BOM|PIR|routing|... or \\"\\">","task_type":"<short label>"},\n'
    '  "evidence": ["<trace tool names / step hints>"],\n'
    '  "lesson_type": "process|method|fact",\n'
    '  "kind": "soft|hard_candidate",\n'
    '  "confidence": 0.0\n'
    "}]}\n"
    "lesson_type: process = workflow/sequencing (e.g. 'create the material, read it back, THEN write its "
    "BOM'); method = how to apply world knowledge to a task; fact = a system-specific truth (e.g. "
    "'bought parts inherit CoO from the supplier'). Prefer 1-3 high-value lessons; [] if nothing durable.\n\n"
    "SESSION INTENT: {intent}\nTRACE (most recent turns):\n{trace}\n"
)


def _trace_digest(steps: list[dict], limit: int = 60) -> str:
    out = []
    for s in (steps or [])[-limit:]:
        k = s.get("kind")
        if k == "tool_call":
            out.append(f"call {s.get('tool')}({', '.join(f'{x}={str(y)[:30]}' for x, y in (s.get('args') or {}).items())})")
        elif k == "tool_result":
            out.append(f"  -> {str(s.get('result', ''))[:160]}")
        elif k == "text":
            t = (s.get("text") or "").strip()
            if t and t[0] not in "💭✦":
                out.append(f"note: {t[:160]}")
    return "\n".join(out)[:6000] or "(no tool actions)"


def reflect(intent: str, steps: list[dict], trigger: str = "manual") -> list[dict]:
    """Fire the Reflector over a session's trace -> validated, stored lessons. ONE LLM call."""
    if not ENABLED:
        return []
    _load()
    prompt = _REFLECT_PROMPT.replace("{intent}", intent or "session").replace("{trace}", _trace_digest(steps))
    try:
        resp = _oai().chat.completions.create(
            model=_REFLECT_MODEL, temperature=0.2, max_tokens=700,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}])
        items = json.loads(resp.choices[0].message.content).get("lessons", [])
    except Exception as e:
        logging.warning(f"[learning] reflect failed: {e}")
        return []
    kept = []
    for raw in (items or [])[:5]:
        le = _validate(raw, source="reflector", default_intent=intent)
        if le:
            stored, how = _dedup_or_add(le)
            kept.append({**stored, "_how": how})
    if kept:
        logging.info(f"[learning] reflect({trigger}): {len(kept)} lesson(s) "
                     f"[{', '.join(k['_how'] for k in kept)}]")
    return kept


def reflect_session(session_id: str, logdir: str = "logs") -> list[dict]:
    """Retroactively run the Reflector over a SAVED session trace (logs/session_<id>.jsonl) -- so we
    can learn from sessions that ran before capture was enabled. Builds a whole-session digest (one
    block per turn, not just the last 60 steps) so breadth isn't lost."""
    if not ENABLED:
        return []
    path = Path(logdir) / f"session_{session_id}.jsonl"
    if not path.exists():
        logging.warning(f"[learning] reflect_session: no trace at {path}")
        return []
    turns, cur = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "input" in r:
            cur = {"intent": r.get("intent", ""), "text": (r.get("input", {}) or {}).get("text", ""),
                   "tools": [], "notes": []}
            turns.append(cur)
        elif cur is not None:
            k = r.get("kind")
            if k == "tool_call":
                cur["tools"].append(r.get("tool"))
            elif k == "tool_result":
                res = str(r.get("result", ""))
                if any(w in res.lower() for w in ("error", "fail", "blocked", "not found", "cannot",
                                                  "missing", "duplicate", "invalid")):
                    cur["notes"].append(f"{r.get('tool', '')}: {res[:120]}")
    lines = []
    for i, t in enumerate(turns):
        lines.append(f"TURN {i} [{t['intent']}] user: {t['text'][:120]}")
        if t["tools"]:
            lines.append("  tools: " + ", ".join(str(x) for x in t["tools"][:10]))
        for n in t["notes"][:3]:
            lines.append("  ! " + n)
    digest = "\n".join(lines)[:7000] or "(empty session)"
    _load()
    dominant = max((t["intent"] for t in turns),
                   key=lambda x: sum(1 for t in turns if t["intent"] == x), default="session") if turns else "session"
    prompt = _REFLECT_PROMPT.replace("{intent}", dominant).replace("{trace}", digest)
    try:
        resp = _oai().chat.completions.create(
            model=_REFLECT_MODEL, temperature=0.2, max_tokens=900,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}])
        items = json.loads(resp.choices[0].message.content).get("lessons", [])
    except Exception as e:
        logging.warning(f"[learning] reflect_session failed: {e}")
        return []
    kept = []
    for raw in (items or [])[:6]:
        le = _validate(raw, source="reflector", default_intent=dominant)
        if le:
            stored, how = _dedup_or_add(le)
            kept.append({**stored, "_how": how})
    logging.info(f"[learning] reflect_session({session_id}): {len(kept)} lesson(s)")
    return kept


_CORRECTION_RE = re.compile(r"\b(no|nope|wrong|incorrect|not right|should be|that's not|isn't|"
                            r"shouldn't|don't|do not|actually it's|it is not|that is wrong)\b", re.I)
_ENTITY_RE = re.compile(r"\b([A-Z]{2,}\d{2,}|\d{4,}|[A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)?)\b")


def capture_correction(intent: str, user_text: str, prev_answer: str) -> dict | None:
    """A user correction is LABELLED GROUND TRUTH -> a hard_candidate with before/after."""
    if not ENABLED or not user_text:
        return None
    if not _CORRECTION_RE.search(user_text) or not _ENTITY_RE.search(user_text):
        return None
    _load()
    le = _validate({
        "context": f"{intent}: user corrected the agent",
        "mistake_or_insight": f"agent said/did: {(prev_answer or '')[:160]}",
        "correction": user_text.strip()[:280],
        "applies_to": {"intent": intent, "object_type": "", "task_type": "user-correction", "tool": ""},
        "evidence": ["user-correction"],
        "lesson_type": "fact",
        "kind": "hard_candidate",
        "confidence": 0.9,
    }, source="correction", default_intent=intent)
    if not le:
        return None
    stored, _ = _dedup_or_add(le)
    logging.info(f"[learning] correction captured -> {stored['id']}")
    return stored


# ---- recall (soft lane) ----------------------------------------------------
def recall(intent: str, query: str, k: int = 3) -> list[dict]:
    """Top-k ACTIVE lessons by similarity to (intent + query), filtered by applies_to.intent."""
    if not ENABLED:
        return []
    _load()
    active = [le for le in _lessons if le.get("status") == "active"]
    if not active:
        return []
    # intent-scoped: a lesson surfaces only on its own intent, or if it carries no intent tag (universal).
    # No "or active" fallback -- otherwise an unrelated intent sees every lesson and recall becomes always-on.
    pool = [le for le in active if le["applies_to"].get("intent") in (intent, None, "")]
    if not pool:
        return []
    q = (intent + " " + (query or "")).strip()
    if _FAISS and _index is not None and _index.ntotal:
        qv = _embed([q])
        scores, idx = _index.search(qv, min(_index.ntotal, k * 6))
        ranked, seen = [], set()
        for s, i in zip(scores[0], idx[0]):
            if i < 0 or i in seen:
                continue
            seen.add(i)
            le = _lessons[i]
            if float(s) >= _RECALL_FLOOR and le.get("status") == "active" and le in pool:  # relevance gate
                ranked.append(le)
            if len(ranked) >= k:
                break
        hits = ranked
    else:                                # no faiss: substring fallback
        ql = q.lower()
        hits = sorted(pool, key=lambda le: -sum(w in _searchable(le).lower() for w in ql.split()))[:k]
    for le in hits:                      # hit counter (recall is a vote of relevance)
        le["hits"] = le.get("hits", 0) + 1
    if hits:
        _rewrite()
    return hits


def format_block(lessons: list[dict]) -> str:
    """The compact, token-capped 'LESSONS (past sessions)' injection block."""
    if not lessons:
        return ""
    lines, used = [], 0
    for le in lessons:
        line = f"- ({le['lesson_type']}) {le['correction'] or le['mistake_or_insight']}"
        t = len(line) // 4                # ~4 chars/token
        if used + t > RECALL_TOKEN_CAP:
            break
        lines.append(line)
        used += t
    if not lines:
        return ""
    return ("\n\nLESSONS (past sessions) — apply these unless the user says otherwise:\n"
            + "\n".join(lines) + "\n")


# ---- measure ---------------------------------------------------------------
def log_event(kind: str, detail: dict):
    if not ENABLED:
        return
    try:
        with (_HERE / "logs").joinpath("learning_events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _now(), "kind": kind, **detail}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def learning_line(injected: list[dict], repeated: int, avoided: int) -> str:
    """The single human line for the trace panel."""
    n = len(injected)
    parts = [f"{n} lesson{'' if n == 1 else 's'} injected"]
    if avoided:
        parts.append(f"{avoided} mistake{'' if avoided == 1 else 's'} avoided")
    parts.append(f"{repeated} past mistake{'' if repeated == 1 else 's'} repeated")
    return " · ".join(parts)


# ---- promotion (hard lane) -------------------------------------------------
def _maybe_promote(le: dict, reason: str):
    """recurrence>=2 OR a correction -> promotion queue (deduped by lesson id). Human-gated."""
    if reason == "recurrence" and le.get("recurrence", 1) < 2:
        return
    existing = list_promotions()
    if any(p.get("lesson_id") == le["id"] and p.get("status") == "queued" for p in existing):
        return
    target = {"process": "before_tool_callback guard", "method": "persona instruction-append (versioned diff)",
              "fact": "KG edge / rules.md entry"}.get(le["lesson_type"], "rules.md entry")
    rec = {"id": "P-" + uuid.uuid4().hex[:8], "ts": _now(), "status": "queued", "reason": reason,
           "lesson_id": le["id"], "lesson_type": le["lesson_type"], "proposed_artifact": target,
           "summary": le["correction"] or le["mistake_or_insight"]}
    with PROMO_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    logging.info(f"[learning] queued promotion {rec['id']} ({le['lesson_type']} -> {target})")


def list_promotions() -> list[dict]:
    if not PROMO_FILE.exists():
        return []
    out = []
    for line in PROMO_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def apply_promotion(promo_id: str, artifact_ref: str) -> dict:
    """HUMAN-GATED last mile: mark a queued promotion APPLIED (a deterministic check/guard now exists
    at `artifact_ref`) and RETIRE the underlying soft lesson, so it stops surfacing in fuzzy recall --
    it's now a hard check that can't be forgotten or missed by a low similarity score. Rewrites the
    queue + lessons file. Returns the updated promotion ({} if not found/already applied)."""
    promos = list_promotions()
    target = next((p for p in promos if p.get("id") == promo_id and p.get("status") == "queued"), None)
    if not target:
        return {}
    target["status"] = "applied"
    target["artifact_ref"] = artifact_ref
    target["applied_ts"] = _now()
    with PROMO_FILE.open("w", encoding="utf-8") as f:
        for p in promos:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    _load()                                   # retire the soft lesson -> recall (status=='active') drops it
    for le in _lessons:
        if le.get("id") == target.get("lesson_id"):
            le["status"] = "retired"
            le["retired_reason"] = f"promoted to deterministic check: {artifact_ref}"
    _rewrite()
    logging.info(f"[learning] promotion {promo_id} APPLIED -> {artifact_ref}; "
                 f"lesson {target.get('lesson_id')} retired (now a hard check)")
    return target


# ---- ledger (proof) --------------------------------------------------------
_SCENARIO = "create-material flow attempts BOM-item write"


def record_ledger(scenario: str, phase: str, outcome: dict):
    """A before/after proof pair: the SAME scenario, pre- vs post-learning."""
    try:
        with LEDGER_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _now(), "scenario": scenario, "phase": phase, **outcome},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def seed_ledger_before():
    """Idempotently record the BEFORE half of the proof pair (the known pre-loop mistake on file)."""
    if not ENABLED:
        return
    if LEDGER_FILE.exists() and '"phase": "before"' in LEDGER_FILE.read_text(encoding="utf-8"):
        return
    record_ledger(_SCENARIO, "before",
                  {"outcome": "mistake occurred: a create-material flow wrote/attempted BOM items "
                              "(no guard)", "source": "historical session on file"})
