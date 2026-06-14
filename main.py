import asyncio
import base64
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from google.adk.agents import LlmAgent, SequentialAgent, ParallelAgent
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService, DatabaseSessionService
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams, SseConnectionParams  # from here
from mcp import StdioServerParameters                                              # from here
from google.genai import types
from openai import OpenAI
from starlette.websockets import WebSocketDisconnect
from google.adk.models.lite_llm import LiteLlm
from google.adk.agents.run_config import RunConfig
import litellm
# gpt-5 models (e.g. gpt-5-nano on the Proc/Fin assessors) reject temperature != 1; without this
# litellm raises UnsupportedParamsError mid-ParallelAgent. Drop unsupported params instead of crashing.
litellm.drop_params = True
from create_pipeline import build_create_pipeline, READONLY_SAP_TOOLS   # at top
from validate_pipeline import build_validation_pipeline
from rule_engine_glue import run_rule_engine
from knowledge import remember, knowledge_block   # shared (human-curated) knowledge base
import contextvars
import learning                                    # the Minimal Viable Learning Loop (flag D2M_LEARNING)

# Windows-only: the Proactor event loop raises a benign ConnectionResetError
# (WinError 10054) inside _call_connection_lost when a socket/pipe is torn down abruptly
# (browser refresh, MCP subprocess shutdown). Wrap that one method to swallow exactly that
# at the source -- more reliable than a loop exception handler (which depends on install
# timing and on which loop surfaces the error).
if sys.platform == "win32":
    from asyncio.proactor_events import _ProactorBasePipeTransport
    _orig_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost

    def _quiet_call_connection_lost(self, exc):
        try:
            _orig_call_connection_lost(self, exc)
        except ConnectionResetError:
            pass

    _ProactorBasePipeTransport._call_connection_lost = _quiet_call_connection_lost

load_dotenv()
# learning.py read its flag at import (before .env loaded); re-read now so D2M_LEARNING in .env works too.
learning.ENABLED = os.getenv("D2M_LEARNING", "0").lower() in ("1", "true", "yes", "on")

# One knob for the chat model used by every agent + the router classifier. Defaults to the
# cheap gpt-4o-mini; override with OPENAI_MODEL in .env (e.g. gpt-5-nano) -- no code edits.
LLM_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# Writers (agents that actually MUTATE SAP) -- and the vision Genesis agent. Kept on a SEPARATE
# knob so they can be upgraded independently, but defaulted to gpt-4o-mini so the whole app runs
# on cheap models only (gpt-4o-mini / gpt-5-nano). gpt-4o-mini IS vision-capable, so Genesis still
# sees images. Set OPENAI_MODEL_WRITE=gpt-4o to put writers back on the stronger model.
LLM_MODEL_WRITE = os.getenv("OPENAI_MODEL_WRITE", "gpt-4o-mini")

APP_NAME = "ADK_MCP_example"
_HERE = Path(__file__).resolve().parent      # anchor asset dirs to this file, not the CWD
STATIC_DIR = _HERE / "static"
LOG_DIR = Path("logs")          # per-session JSONL trace logs land here

# In-memory ring buffer of recent backend log records, exposed at /api/logs so the
# console stream (esp. the [ctx] context-budget lines) can be inspected without scraping
# the terminal. Capped, so it can never grow unbounded. Console behaviour is preserved:
# an explicit stderr handler at WARNING (replacing logging.lastResort, which a custom
# handler would otherwise disable), while the ring captures INFO+ for diagnosis.
import collections as _collections
_LOG_RING = _collections.deque(maxlen=3000)


class _RingLogHandler(logging.Handler):
    def emit(self, record):
        try:
            _LOG_RING.append({"t": record.created, "lvl": record.levelname,
                              "name": record.name, "msg": record.getMessage()})
        except Exception:
            pass


_root_logger = logging.getLogger()
if not any(isinstance(h, _RingLogHandler) for h in _root_logger.handlers):
    _root_logger.setLevel(logging.INFO)
    _console_handler = logging.StreamHandler(sys.stderr)
    _console_handler.setLevel(logging.WARNING)        # console stays as quiet as before
    _console_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    _root_logger.addHandler(_console_handler)
    _root_logger.addHandler(_RingLogHandler(level=logging.INFO))

# DURABLE sessions: persisted to SQLite so a session survives an app restart and can be
# reconnected / reloaded by id (vs InMemory, which dies with the process). The genesis run is
# the reason -- a mid-run stop is recoverable because re-running genesis is idempotent
# (_exists skips already-created objects) and SAP is the system of record. The DB needs an
# ASYNC driver (aiosqlite) -> sqlite+aiosqlite URL. Override with SESSION_DB_URL (e.g. Postgres).
SESSION_DB_URL = os.getenv("SESSION_DB_URL", "sqlite+aiosqlite:///./adk_sessions.db")
session_service = DatabaseSessionService(db_url=SESSION_DB_URL)
artifacts_service = InMemoryArtifactService()

# Used only for Whisper speech-to-text (a plain model call, NOT an agent/tool).
# Reads OPENAI_API_KEY from the environment, same key the agent's GPT-4o uses.
openai_client = OpenAI()

# --- One connection per MCP server ---
# Spawn paths are anchored to THIS file's directory (not the CWD), so `uv run main.py` works from
# anywhere. Each local server is a sibling script under mcp_server/.
def _srv(name: str) -> list[str]:
    return [str(_HERE / "mcp_server" / name)]


cocktail_server_params = StdioConnectionParams(
    server_params=StdioServerParameters(command=sys.executable, args=_srv("cocktail.py")),
    timeout=30,
)

sap_server_params = StdioConnectionParams(
    server_params=StdioServerParameters(command=sys.executable, args=_srv("sap.py")),
    timeout=30,
)

serper_server_params = StdioConnectionParams(
    server_params=StdioServerParameters(command=sys.executable, args=_srv("serper.py")),
    timeout=30,   # web search (Serper -> Google) routinely exceeds the 5s default; the timeouts
                  # were causing the agent to RETRY (12 materials -> 65 google_search calls), which
                  # then blew the turn's call budget and half-rendered the price cards.
)

vector_server_params = StdioConnectionParams(
    server_params=StdioServerParameters(command=sys.executable, args=_srv("vector.py")),
    timeout=60,   # building the embedding index can take a little longer
)

graph_server_params = StdioConnectionParams(
    server_params=StdioServerParameters(command=sys.executable, args=_srv("graph.py")),
    timeout=30,
)

# The "make" objects (PIR / cost / BOM / routing) live on their own MCP server
# (different OData services than API_PRODUCT_SRV). All its write tools are confirm-gated.
make_server_params = StdioConnectionParams(
    server_params=StdioServerParameters(command=sys.executable, args=_srv("make.py")),
    timeout=30,
)

# Assurance: the DETERMINISTIC findings engine behind the boardroom -- runs policies.json + the
# data rules over the REAL created objects (material + components + PIRs/CoO) and returns structured
# findings. Read-only; the board JUDGES its findings (facts by code, judgment by the board).
assurance_server_params = StdioConnectionParams(
    server_params=StdioServerParameters(command=sys.executable, args=_srv("assurance.py")),
    timeout=120,
)

# Genesis: one tool (run_genesis) creates a whole assembly's master data (parent + components
# + PIR/cost + BOM + routing). A full run does many SAP writes, so allow a long timeout.
genesis_server_params = StdioConnectionParams(
    server_params=StdioServerParameters(command=sys.executable, args=_srv("genesis.py")),
    timeout=300,
)

# Planning (MRP + demand) runs as a SEPARATE process (MCPCTypeNWRFC, :8001) that carries the SAP
# NW RFC SDK + SAP .env. The ADK app connects as a REMOTE MCP client over SSE and imports NO SAP
# SDK -- dependency isolation made physical. Start that server first (python sap_planning_mcp.py).
# Lazily loaded (only when a 'planning' turn is routed), so a down server never affects genesis/make.
PLANNING_MCP_URL = os.getenv("PLANNING_MCP_URL", "http://127.0.0.1:8001/sse")
planning_server_params = SseConnectionParams(url=PLANNING_MCP_URL)

# Production version (MKAL) -- another REMOTE MCP (yours, :8002, ZD2M_PROD_VERS_MAINTAIN over RFC).
# The FINAL genesis step AFTER routing: binds the BOM + routing so MRP can select them -- without a
# production version, MRP fails MD408 "No BOM selected". App imports no SAP SDK -- only the URL.
PRODVER_MCP_URL = os.getenv("PRODVER_MCP_URL", "http://127.0.0.1:8002/sse")
prodver_server_params = SseConnectionParams(url=PRODVER_MCP_URL)

import tiktoken                                          # token-accurate context budgeting
try:
    _ENC = tiktoken.encoding_for_model("gpt-4o-mini")    # o200k_base for the gpt-4o family
except Exception:                                        # offline / unknown model -> a safe default
    _ENC = tiktoken.get_encoding("cl100k_base")


def _ntok(s) -> int:
    """Token count of a string (or anything stringifiable). The measurement that ends the guessing."""
    if not s:
        return 0
    try:
        return len(_ENC.encode(s if isinstance(s, str) else str(s)))
    except Exception:
        return len(str(s)) // 4


def _strip_card_from_fr(fr) -> int:
    """Drop the @@DATA@@ UI-card block from a function_response the MODEL will replay. The card JSON
    powers the Data panel (extracted separately in run_with_trace from the streamed events, so the UI
    is untouched) and the model never needs it -- only the readable text before @@DATA@@. Removes
    ~13k chars per genesis result (+ every make-tool card) from the context window. Mutates in place;
    returns the number of chars removed (so the trimmer can log/verify the saving)."""
    sentinel = "@@DATA@@"
    resp = getattr(fr, "response", None)
    removed = 0
    if isinstance(resp, str):
        if sentinel in resp:
            new = resp.split(sentinel, 1)[0].rstrip()
            removed = len(resp) - len(new)
            fr.response = new
        return removed
    if isinstance(resp, dict):
        content = resp.get("content")
        if isinstance(content, list):
            for part in content:
                t = part.get("text") if isinstance(part, dict) else None
                if isinstance(t, str) and sentinel in t:
                    nt = t.split(sentinel, 1)[0].rstrip()
                    removed += len(t) - len(nt)
                    part["text"] = nt
        for k in ("result", "text"):                      # other shapes: a flat string field
            v = resp.get(k)
            if isinstance(v, str) and sentinel in v:
                nv = v.split(sentinel, 1)[0].rstrip()
                removed += len(v) - len(nv)
                resp[k] = nv
    return removed


def _trim_context(callback_context, llm_request):
    """before_model_callback -- keep the request under the 128k TOKEN window (tiktoken-measured, not
    chars). Escalating passes so a long session OR a single fat turn can't ContextWindowExceededError:
      0. MEASURE: when near budget, log the token composition (top contributors) -- bloat is data, not a guess.
      1. strip inline images from history (base64 / vision tokens are huge).
      2. over budget -> stub out tool results in OLDER invocations.
      3. still over -> drop the OLDEST whole invocations, cutting only at a user-typed boundary so no
         function_response is orphaned (the OpenAI 'tool must follow tool_calls' 400).
      4. last resort -> the LIVE turn alone is too big (e.g. the boardroom's MRP+assurance JSON):
         stub its largest tool results, biggest first. May degrade that answer, but never crashes.
    The DURABLE fix is to keep fat results out of context in the first place (compact tool returns +
    structured persona verdicts); this callback is the universal backstop."""
    contents = llm_request.contents or []
    if not contents:
        return None
    TOKEN_BUDGET = 100_000           # messages-only ceiling for a 128k model (room: functions+system+output)
    STUB = {"elided": "[tool result elided to fit the context window -- shown in full in the Activity panel]"}

    def _is_user_text(c):                                # a message the user actually TYPED (not a tool result)
        return getattr(c, "role", "") == "user" and any(
            getattr(p, "text", None) and getattr(p, "function_response", None) is None
            for p in (c.parts or []))

    def _frs(c):                                         # the function_responses carried by a content
        return [p.function_response for p in (c.parts or [])
                if getattr(p, "function_response", None) is not None]

    def _csize(c):                                       # tokens for one content (a remaining image ~= 1500)
        n = _ntok("".join(getattr(p, "text", "") or "" for p in (c.parts or [])))
        for p in (c.parts or []):
            fr = getattr(p, "function_response", None)
            if fr is not None:
                n += _ntok(getattr(fr, "response", ""))
            if getattr(p, "inline_data", None) is not None:
                n += 1500
        return n

    current_start = max((i for i, c in enumerate(contents) if _is_user_text(c)), default=0)

    # (1) strip inline images from history
    for idx in range(current_start):
        parts = contents[idx].parts or []
        kept = [p for p in parts if getattr(p, "inline_data", None) is None]
        if not kept and parts:
            kept = [types.Part(text="[image omitted from history]")]
        contents[idx].parts = kept

    # (1b) drop @@DATA@@ UI-card blocks from EVERY function_response -- pure waste in the model
    #      context (the card powers the Data panel, extracted separately). Runs every call, regardless
    #      of budget: ~13k chars/genesis + every make-tool card, gone before we even measure.
    _card_removed = 0
    for c in contents:
        for fr in _frs(c):
            _card_removed += _strip_card_from_fr(fr)
    if _card_removed:
        logging.info(f"[ctx] stripped @@DATA@@ UI-card blocks from model context (-{_card_removed} chars)")

    total = sum(_csize(c) for c in contents)

    # (0) MEASURE near the ceiling -- all at INFO. Trimming a long session is the DESIGNED behaviour
    #     (compaction enabling endless work), NOT an error -- so it must not scream WARNING every turn.
    #     The only real WARNING is pass (4): a single LIVE turn that can't be made to fit even after
    #     dropping all history. "monitoring" => under budget; "trimming (routine)" => fitting the window.
    if total > 0.8 * TOKEN_BUDGET:
        top = sorted(((_csize(c), i, getattr(c, "role", "?")) for i, c in enumerate(contents)),
                     reverse=True)[:6]
        state = "trimming (routine)" if total > TOKEN_BUDGET else "monitoring"
        logging.info(f"[ctx] {total} tok / {len(contents)} msgs (budget {TOKEN_BUDGET}) -- {state}; top: "
                     + " ".join(f"#{i}:{role}={n}t" for n, i, role in top))
    if total <= TOKEN_BUDGET:
        return None

    # (2) stub tool results in OLDER invocations (history detail is rarely needed to answer)
    for idx in range(current_start):
        for fr in _frs(contents[idx]):
            if _ntok(getattr(fr, "response", "")) > 300:
                fr.response = dict(STUB)
    total = sum(_csize(c) for c in contents)

    # (3) drop oldest whole invocations -- cut ONLY at a user-typed boundary (no orphaned responses)
    if total > TOKEN_BUDGET:
        user_idx = [i for i, c in enumerate(contents) if _is_user_text(c)]
        cut = next((ui for ui in user_idx if sum(_csize(c) for c in contents[ui:]) <= TOKEN_BUDGET), None)
        if cut and cut > 0:
            contents = contents[cut:]
            llm_request.contents = contents
            logging.info(f"[ctx] dropped {cut} old content(s) to fit the window")
            total = sum(_csize(c) for c in contents)

    # (4) last resort: the LIVE turn alone is over budget -> stub its biggest tool results, largest first
    if total > TOKEN_BUDGET:
        ranked = sorted(((_ntok(getattr(fr, "response", "")), fr) for c in contents for fr in _frs(c)),
                        key=lambda t: t[0], reverse=True)
        for before, fr in ranked:
            if total <= TOKEN_BUDGET:
                break
            fr.response = dict(STUB)
            total -= max(0, before - _ntok(STUB))
        logging.warning(f"[ctx] live turn over budget; stubbed largest tool results -> ~{total} tok")
    return None


from google.adk.plugins.base_plugin import BasePlugin    # noqa: E402


class _TokenTrimPlugin(BasePlugin):
    """Runner-level context trimmer. A per-agent before_model_callback is fragile -- the rule-engine
    pipeline (Derive/CodeCheck/PolicyCheck/Critic) never got it, so the 'validate' path bypassed
    trimming and blew the window at 295k tokens. A PLUGIN fires for EVERY agent in the runner's tree,
    so no path can slip through. (Per-agent callbacks remain as harmless belt-and-braces.)"""

    def __init__(self):
        super().__init__(name="token_trim")

    async def before_model_callback(self, *, callback_context, llm_request):
        _trim_context(callback_context, llm_request)
        if learning.ENABLED and _inject_lessons(llm_request):
            logging.info(f"[learning] LESSONS injected -> agent "      # 🎓 per-agent confirmation; on a
                         f"'{getattr(callback_context, 'agent_name', '?')}'")  # board turn, look for each assessor
        return None


_TRIM_PLUGIN = _TokenTrimPlugin()


# ====================== LEARNING LOOP wiring (flag D2M_LEARNING) ======================
# Code decides WHEN to remember; the LLM only supplies content. Engine in learning.py.
_RUN_LESSONS: dict[str, list] = {}        # session_id -> lessons recalled this turn (for the line)
_SESSION_STEPS: dict[str, list] = {}      # session_id -> accumulated trace steps (for the Reflector)
_LAST_ANSWER: dict[str, str] = {}         # session_id -> previous agent answer (capture-on-correction)
_PREV_INTENT: dict[str, str] = {}         # session_id -> previous turn's intent
_LESSON_BLOCK = contextvars.ContextVar("d2m_lesson_block", default="")   # injected into THIS turn
_LAST_LESSON_BLOCK = [""]   # module-level mirror: ContextVars are copied into asyncio tasks at CREATE
                            # time, but the boardroom's ParallelAgent assessors can miss it -> the
                            # callback falls back to this so EVERY agent (incl. assessors) gets lessons.
_AVOIDED_COUNT: dict = {}                                                # token -> guard blocks (crosses tasks)
# A ContextVar propagates DOWN into the model callback (so _LESSON_BLOCK / _TURN_TOKEN are readable
# there) but NOT back UP to this task. So injection CONFIRMATION crosses back via a module-level set
# keyed by a per-turn token -- the request-level check that gates the 'injected' counter.
_TURN_TOKEN = contextvars.ContextVar("d2m_turn", default="")             # per-turn id (read in the callback)
_INJECTED_TOKENS: set = set()                                            # tokens whose block actually landed
_TURN_SEQ = [0]                                                          # monotonic turn counter
_LESSON_MARKER = "LESSONS (past sessions)"                               # unique substring of the block

_BOM_WRITE_TOOLS = {"create_bom", "add_bom_component", "remove_bom_component"}
# Fire the Reflector on /reflect, "what did/have you|we learn(ed)", "what we/you learned",
# "what could be/we improve", or "did we update ... knowledge/learning". Kept specific to reflection.
_REFLECT_RE = re.compile(
    r"^\s*/reflect\b"
    r"|what (did|have|do)\s+(you|we)\s+learn"
    r"|(things|what)\s+(we|you)\b.{0,25}\blearned\b"
    r"|what (could|can|should) (be|we|you) improve"
    r"|(updated?|capture[d]?|store[d]?)\b.{0,25}(knowledge|learning|lesson)", re.I)


def _si_text(si) -> str:
    """Flatten a system_instruction (str | Content | parts) to text for assertion."""
    if si is None:
        return ""
    if isinstance(si, str):
        return si
    try:
        return " ".join(getattr(p, "text", "") or "" for p in (getattr(si, "parts", None) or []))
    except Exception:
        return str(si)


def _inject_lessons(llm_request):
    """Soft lane: put THIS turn's LESSONS block in front of the model, then ASSERT at the request
    level that it actually landed. The 'injected' counter is gated on this check (see the WS loop),
    so the Learning line can never over-report. Falls back to a leading user content if the
    system-instruction path isn't writable in this ADK build -- so lessons still reach the model."""
    block = _LESSON_BLOCK.get() or _LAST_LESSON_BLOCK[0]   # ContextVar for serial agents; the mirror
    if not block:                                          # reaches the boardroom's PARALLEL assessors
        return False
    cfg = getattr(llm_request, "config", None)
    if cfg is not None:                          # preferred: append to the system instruction
        si = getattr(cfg, "system_instruction", None)
        try:
            if si is None or isinstance(si, str):
                cfg.system_instruction = (si or "") + block
            else:
                si.parts.append(types.Part(text=block))
        except Exception:
            try:
                cfg.system_instruction = _si_text(si) + block
            except Exception:
                pass
    present = bool(cfg) and _LESSON_MARKER in _si_text(getattr(cfg, "system_instruction", None))
    if not present:                              # fallback: a leading user content the model WILL see
        try:
            (llm_request.contents or []).insert(0, types.Content(role="user", parts=[types.Part(text=block)]))
            present = any(_LESSON_MARKER in (getattr(p, "text", "") or "")
                          for c in (llm_request.contents or [])[:1] for p in (c.parts or []))
        except Exception:
            present = False
    if present:                                  # request-level confirmation -> crosses tasks via the set
        tok = _TURN_TOKEN.get()
        if tok:
            _INJECTED_TOKENS.add(tok)
    return present


def _bom_guard(tool, args, tool_context):
    """HARD lane (first promotion): a create-material flow may NOT write BOM items -> block + redirect.
    Returns a dict to short-circuit the call (ADK before_tool_callback contract)."""
    if not learning.ENABLED:
        return None
    name = getattr(tool, "name", "") or ""
    a = args or {}
    via_generic = name in ("change_make_object", "query_sap", "create_change_object") and any(
        s in str(v).lower() for v in a.values() for s in ("billofmaterial", "materialbom", "/bom"))
    if name in _BOM_WRITE_TOOLS or via_generic:
        learning.log_event("mistake_avoided", {"tool": name, "guard": "bom_in_create_material"})
        tok = _TURN_TOKEN.get()              # confirm to the WS loop across tasks (like injection)
        if tok:
            _AVOIDED_COUNT[tok] = _AVOIDED_COUNT.get(tok, 0) + 1
        return {"blocked": True,
                "message": ("Blocked by a learned guard: a create-material flow must NOT write BOM "
                            "items. Finish creating the material first; BOM changes are a SEPARATE "
                            "request ('make' / 'fix the BOM' once the material exists).")}
    return None


def _count_repeated_mistakes(steps: list[dict]) -> int:
    """A known mistake that actually slipped past (a BOM write that ran, not one the guard blocked)."""
    n = 0
    for s in steps or []:
        if s.get("kind") == "tool_result" and s.get("tool") in _BOM_WRITE_TOOLS \
           and "blocked by a learned guard" not in str(s.get("result", "")).lower():
            n += 1
    return n


def _with_bom_guard(pipeline):
    """Attach the BOM guard (before_tool_callback) to every sub-agent of the create-material flow."""
    if learning.ENABLED:
        for a in (getattr(pipeline, "sub_agents", None) or []):
            a.before_tool_callback = _bom_guard
    return pipeline


def create_agent():
    """Creates an ADK Agent with tools from MCP Server."""
    agent_instruction = """You are a helpful and reliable AI assistant with access to SAP S/4HANA and a cocktail database via MCP tools.

GENERAL
 - Use your tools to answer; combine the results into a clear Markdown answer (tables work well for material lists).
 - If a tool returns a "warnings" field, mention the caveat (e.g. results were a lower bound) to the user.
 - If you cannot find what was asked, say so plainly.

SEARCHING SAP MATERIALS / PRODUCTS  (the key skill)
 - For ANY request to find/search/filter materials, call `search_materials`. Never hand-craft OData with `query_sap` for searches.
 - Map the user's words to PARAMETERS (the tool owns SAP's data model). The MOST IMPORTANT choice is material NUMBER vs DESCRIPTION:
     * A code-like token (letters+digits/hyphens, e.g. "TG10", "TG-10", "MZ-TG-Y120", "21") is a MATERIAL NUMBER  -> use product=...
     * Natural words (e.g. "pump", "bicycle", "AMD Ryzen") are DESCRIPTION text                                  -> use description=...
   Examples:
     "find TG10"                    -> search_materials(product="TG10")
     "material MZ-TG-Y120"          -> search_materials(product="MZ-TG-Y120")
     "anything with 'pump' in it"   -> search_materials(description="pump")
     "Ryzen made in plant 1710"     -> search_materials(description="Ryzen", plant="1710")
     "trading goods sold in the US" -> search_materials(product_type="HAWA", country="US")
 - product and description are SUBSTRING matches. Material numbers are often typed with separators/padding that differ from the stored key (user types "TG-10" but it is stored "TG10"). If a search returns 0 results, DO NOT give up — retry automatically:
     1. Remove separators/spaces and retry (e.g. "TG-10" -> "TG10").
     2. Still 0? Retry with a shorter leading prefix (e.g. "TG") and let the user pick from the list.
     3. If a token could be either kind, try product= first, then description=.
   Briefly tell the user what you tried.
 - If unsure which parameter or code to use (material-type, plant code, etc.), call `describe_search_fields` FIRST, then `search_materials`.
 - Prefer fewer, selective parameters. If a result is huge or flagged as a lower bound, ask the user to narrow it.

MULTIMODAL INPUT (image or audio)
 - If the user sends an IMAGE (e.g. a photo of a product, a box, a label, a spec sheet), read it and EXTRACT the searchable attributes — product name/description text, brand, any visible plant/material codes — then call `search_materials` with those as parameters.
 - If the user's message is a TRANSCRIPT of spoken audio, treat it exactly like a typed query: extract intent -> parameters -> `search_materials`.
 - Always tell the user which attributes you extracted before/with the results, so they can correct you.

 - `query_sap`, `list_materials`, `get_material` remain available for direct reads by key or for non-product OData services.
"""
    root_agent = LlmAgent(
        model=LiteLlm(model=f"openai/{LLM_MODEL}"),
        name="ai_assistant",
        instruction=agent_instruction,
        # Low temperature -> more deterministic tool routing ("listens" reliably).
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
        before_model_callback=_trim_context,
        tools=[McpToolset(connection_params=cocktail_server_params),
               McpToolset(connection_params=sap_server_params),
               McpToolset(connection_params=serper_server_params)]
    )
    return root_agent


# ReAct-style trace narration: the multi-tool agents emit a one-line Thought BEFORE a tool and
# a one-line Reflection AFTER a result. These land as text steps the Activity panel styles
# distinctly; run_with_trace keeps them OUT of the final chat answer. (Genesis is excluded --
# its flow is a single deterministic call, nothing to narrate.)
_NARRATION = """

TRACE NARRATION (for the Activity panel only -- NOT part of your final answer):
- BEFORE each tool call, emit ONE short line beginning with "💭 " stating WHY you are calling it.
- AFTER each tool returns, emit ONE short line beginning with "✦ " stating what the result means
  or your next step.
One sentence each. Never put these lines in your final user-facing answer.
"""


def build_search_agent():
    """SEARCH / LOOKUP specialist: exact + semantic material search, the knowledge-graph
    ontology, and web lookups. Read-only -- no create/change tools."""
    instruction = """You are the SEARCH & LOOKUP specialist. Pick the right tool:
 - Exact / code material search -> search_materials (get_material for a known ID).
 - "Show the BOM for X" -> get_bom(material). "Show the PIR for X" -> read_pir(material) (a
   MATERIAL is NOT a PIR number -- never call get_info_record with a material). "Show the routing
   for X" -> get_routing(material). These DIRECT readers render cards and default the plant if the
   user didn't give one -- do NOT poke at BOM/PIR/routing via explore_entity or list_fields for a
   plain "show me" request, and don't demand a plant/alternative the user didn't mention.
 - Semantic / fuzzy "find something like ..." -> semantic_search (if it says no index,
   call index_materials first). "Are there duplicates of ..." -> find_duplicates.
 - Type-level KNOWLEDGE ("what does a FERT need?", "does ROH need a PIR?", which views/
   objects a material type requires) -> requirements_for / neighbors / list_concepts
   (the knowledge graph = the ONTOLOGY, not instance data).
 - Plant / code lookups -> list_plants, describe_search_fields, list_allowed_values.
 - "What field is X?" / a view's fields, its KEY or its nav names -> find_field(term, service=...)
   or list_fields(entity, service=...) (live $metadata). For NON-product objects pass service=
   one of API_INFORECORD_PROCESS_SRV / API_PURGPRCGCONDITIONRECORD_SRV /
   API_BILL_OF_MATERIAL_SRV / API_PRODUCTION_ROUTING. list_fields returns the exact KEY and
   NAVIGATION properties too.
 - To read NESTED segments ("all segments/entities of this PIR/routing/material"): use
   explore_entity(entity, filter=..., service=...) -- it discovers the navs and expands the
   WHOLE object in ONE call. When the user gives BUSINESS keys (e.g. material + supplier), put
   them in `filter` (e.g. "Material eq '11066' and Supplier eq '17300001'") -- those are NOT
   the object's own number (a PIR's own id looks like 53000xxxxx). Never hand-build $expand or
   guess nav names.
 - Web facts (specs, weights, dimensions) -> google_search; cite the source.
Combine results into a clear Markdown answer. You do NOT create or change materials
(you don't even have those tools) -- those requests go to a different specialist.
""" + _NARRATION
    return LlmAgent(
        model=LiteLlm(model=f"openai/{LLM_MODEL}"),
        name="Search",
        instruction=instruction,
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
        before_model_callback=_trim_context,
        # read-only SAP tools: Search can never write (tool_filter drops create/update/change)
        tools=[McpToolset(connection_params=sap_server_params, tool_filter=READONLY_SAP_TOOLS),
               McpToolset(connection_params=vector_server_params),
               McpToolset(connection_params=graph_server_params),
               # read-only make views so "show the BOM / PIR / routing for X" renders a card here too
               McpToolset(connection_params=make_server_params,
                          tool_filter=["get_bom", "read_pir", "get_routing", "get_info_record"]),
               McpToolset(connection_params=serper_server_params)],
    )


def build_make_agent():
    """MAKE & PLAN specialist: builds the supply-chain objects AROUND an existing
    material/assembly -- PIR (bought), BOM + routing + cost (made). Read-only SAP for
    lookups + the confirm-gated make tools. Does NOT edit the material master itself."""
    instruction = """You are the MAKE & PLAN specialist. You build the supply-chain objects
AROUND an existing material/assembly in SAP. You do NOT create or edit the material master
itself (a different specialist does that). Your make tools are ALL preview-then-confirm gated:
  - create_info_record   -> Purchase Info Record (the BOUGHT path: supplier, price, lead time)
  - create_cost_condition-> purchasing price condition (PPR0), optional quantity scales
  - create_bom           -> Bill of Material (the MADE structure: parent + components)
  - add_bom_component / remove_bom_component -> add or remove ONE component on an EXISTING
    BOM, by component number (they find the BOM + the item's key for you -- use these for BOM
    edits rather than change_make_object). Pass alternative= for a specific BOM variant.
  - create_routing       -> Routing (the MADE operations: work centers, setup/run times)
  - change_make_object   -> change/extend any other keyed make row (PATCH/add/delete)
  - create_production_version -> the PRODUCTION VERSION (MKAL): binds an existing material's BOM so
    MRP can SELECT it -- fixes MD408 "No BOM selected". Use THIS for "create the production version",
    NEVER hand-build it via change_make_object (A_ProductPlant is not on the BOM service -> 404). Pass
    ONLY: material, plant, version (e.g. "0001"), text (e.g. "<material description> version 1"),
    bom_usage="1", bom_alt="01", testrun=false. Do NOT pass routing fields
    (routing_type/routing_group/routing_counter) -- we do NOT maintain the routing/task-list
    assignment in the version at this stage (it errors on PLNAL; only the BOM bind is needed, and the
    Planning-data section stays BLANK).

DECIDE bought vs made from the material TYPE (call get_material if unsure):
  - BOUGHT (ROH / HAWA / externally procured): create a PIR (material + supplier); optionally a
    cost condition.
  - MADE (FERT / HALB / in-house): create a BOM (parent + components) and a Routing; then cost.

SAFETY GATE (always): call the tool with confirm=false FIRST, show the user the exact preview,
and only call again with confirm=true AFTER they explicitly authorise ("go ahead", "create it").

GROUNDING (the failure modes we have actually hit -- avoid them):
  - BOM + ROUTING need the material EXTENDED to the plant. If a create fails with "does not exist
    in plant" or a consistency error (CZCL/002), tell the user the material isn't set up for that
    plant -- don't retry blindly.
  - ROUTING needs the material PRODUCTION-configured (work-scheduling view). For the work center,
    pass the human CODE/NAME the user gives (e.g. "PACK01" or "packaging") as `work_center` -- the
    tool resolves it to the internal id (or call find_work_center to look it up / list the options).
    Do NOT ask the user for an internal id. Operation numbers have NO leading zeros; routing is
    created released.
  - BOM live-create is currently BLOCKED by a backend issue (BOM/171). You can still PREVIEW the BOM
    (confirm=false) and explain the live create is pending a backend (Z OData) wrapper.
  - MRP / planning is handled by the separate PLANNING specialist now (a remote server). If the
    user asks to run MRP / plan / create demand, that routes there -- you don't do it here.
  - Resolve coded values with list_allowed_values; resolve exact field names with find_field.
  - Before change_make_object, call list_fields(entity, service=...) to get the child entity's
    EXACT key -- don't guess it. e.g. A_PurgInfoRecdOrgPlantData's key is PurchasingInfoRecord +
    PurchasingInfoRecordCategory + PurchasingOrganization + Plant (Plant is '' at EKORG level),
    and a PIR's net price lives in its to_PurInfoRecdPrcgCndnValidity child, not on the org row.

Report what you did, including any created object numbers, in clear Markdown. If a write fails, show
the SAP error verbatim so we can fix the tool. If a corrected code / field / work-center then
succeeds (or the user corrects you), call remember(...) so the lesson persists.

A tool result may end with a line beginning "@@DATA@@" followed by JSON -- that block powers the
UI's structured card and is NOT for the user. NEVER repeat, quote, or mention it; reply only from
the readable text ABOVE it.
""" + knowledge_block() + _NARRATION
    return LlmAgent(
        model=LiteLlm(model=f"openai/{LLM_MODEL_WRITE}"),    # writes SAP -> stronger model
        name="Make",
        instruction=instruction,
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
        before_model_callback=_trim_context,
        tools=[McpToolset(connection_params=sap_server_params, tool_filter=READONLY_SAP_TOOLS),
               McpToolset(connection_params=make_server_params),
               McpToolset(connection_params=prodver_server_params, tool_filter=["create_production_version"]),
               remember],
    )


def build_genesis_agent():
    """GENESIS specialist (vision): turns an IMAGE of a disassembled product + structured
    details into a FULL SAP master-data set, by building a genesis spec and calling
    run_genesis (which deterministically creates parent + components + PIR/cost + BOM + routing)."""
    instruction = """You are the GENESIS specialist. You turn a PRODUCT/ASSEMBLY shown in an
IMAGE (a disassembled product with LABELED parts) -- plus any structured details the user
provides (types, vendors, prices; e.g. a pasted CSV) -- into a FULL set of SAP master data,
by building one genesis spec and calling run_genesis. You do NOT call the individual create
tools yourself; run_genesis does the whole chain deterministically.

STEPS:
1. READ the image: identify the PARENT assembly (the finished product) and EACH labeled
   COMPONENT part. List what you see.
2. BUILD the spec (a JSON object):
   {
     "parent": {"description": "<assembly name>", "type": "FERT"},
     "components": [
       {"name": "<part>", "description": "<part>", "type": "HAWA", "role": "bought",
        "vendor": "<from the user's details, else 17300001>", "price": <number or omit>,
        "quantity": 1}
     ],
     "routing": [{"operation":"10","text":"Final Assembly","work_center":"ASSEMBLY"},
                 {"operation":"20","text":"Packaging","work_center":"PACK01"}]
   }
   - Use the user's structured details (CSV) for each component's type / vendor / price when
     given. Otherwise default a component to type "HAWA", role "bought".
   - A sub-assembly the user marks as made -> type "HALB", role "made".
   - SINGLE STANDALONE COMPONENT (e.g. "create DDR RAM with its material + PIR + cost", no parent
     assembly): OMIT the "parent" key entirely and put just that part in "components". run_genesis
     then creates the material + PIR + cost and correctly SKIPS BOM/routing/production-version (a
     bought part has nothing to manufacture). NEVER invent a parent for a single part, and NEVER
     build the material by hand -- always go through run_genesis.
3. PREVIEW: call run_genesis(spec, confirm=false) and SHOW the returned plan to the user.
4. Only AFTER the user explicitly authorises ("go ahead", "create them all"), call
   run_genesis(spec, confirm=true). Then report the created object numbers (parent, BOM, routing).
   The result ends with a 🛡 DISCIPLINE DOSSIER (S0-S8) -- ALWAYS surface its headline: the
   verdict, the overall confidence, and any "S7 ESCALATE" lines. If the verdict is ESCALATE,
   tell the user WHAT needs a human (the escalated stages) before treating genesis as done.
5. PRODUCTION VERSION -- run_genesis creates it AUTOMATICALLY as the final stage of an ASSEMBLY
   (binds BOM alt 01 / usage 1 as version 0001 -> MRP-ready, clears MD408). It is NOT created for a
   components-only run. You have NO separate production-version tool and must NEVER try to make one
   yourself -- and NEVER pass a material NAME ("DDR RAM") where a material NUMBER is expected. If the
   dossier escalates "production_version" (e.g. :8002 was down), tell the user it can be retried via
   the 'make' specialist.

NEVER call run_genesis with confirm=true before the user approves the preview. NEVER hand-build a
material via build_material_payload/create tools -- run_genesis owns the whole write chain. If the
image is unclear or details are missing, say what you assumed.

run_genesis's result may end with a line beginning "@@DATA@@" followed by JSON -- that block powers
the UI's structured card and is NOT for the user. NEVER repeat it, quote it, or mention it; reply
only from the readable report ABOVE it. When the preview shows a component as REUSE/dup, tell the
user it will reuse that existing material (a duplicate was found) instead of creating a new one.
""" + knowledge_block() + _NARRATION
    return LlmAgent(
        model=LiteLlm(model=f"openai/{LLM_MODEL_WRITE}"),    # vision + writes -> stronger model
        name="Genesis",
        instruction=instruction,
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
        before_model_callback=_trim_context,
        # NO create_production_version tool here -- run_genesis does the prodver internally (only for a
        # real FERT parent). Exposing it let the agent hand-call it with a material NAME -> SAP failure.
        tools=[McpToolset(connection_params=genesis_server_params),
               McpToolset(connection_params=sap_server_params, tool_filter=READONLY_SAP_TOOLS),
               remember],
    )


def build_planning_agent():
    """PLANNING (MRP) specialist: once a finished good's master data exists (genesis/make),
    plan it via the REMOTE planning MCP server (MRP + demand over RFC, a separate process at
    :8001). This agent imports no SAP SDK -- only the remote tools."""
    instruction = """You are the PLANNING (MRP) specialist. Once a finished good's master data
exists (material + BOM + routing), you PLAN it with the remote planning tools:
  - create_demand(material, plant, quantity?, customer?) -> creates a sales order so MRP has
    demand to plan. Returns {"sales_order": "<n>"}.
  - run_mrp(material, plant) -> runs single-material MRP, returns
    {"return": {type,message}, "planned_orders": [...], "purchase_reqs": [...]}.
  - read_mrp_results(material, plant) -> READ-ONLY (no re-plan): the FULL picture for display --
    planned_orders + the BOM components' DEPENDENT REQUIREMENTS (RESB) + purchase_reqs. run_mrp
    returns ONLY the material's own planned orders/purchase reqs; the component demands it explodes
    into live in RESB, which this reads. It is read-only, so NO confirmation is needed.

FLOW for "plan / run MRP for material M (plant default 1710)":
  1. A made finished good needs DEMAND before MRP yields planned orders. If the scenario has no
     demand yet, create_demand(M, plant) to seed it.
  2. run_mrp(M, plant).
  3. To DISPLAY everything ("show them all"), call read_mrp_results(M, plant) and present a
     MULTI-LEVEL view: the FERT PLANNED ORDER(S) (PLNUM, qty GSMNG, start/finish PSTTR/PEDTR), then
     under it the component DEPENDENT REQUIREMENTS (COMPONENT, REQ_QTY, REQ_DATE), and any PURCHASE
     REQUISITIONS (BANFN/BNFPO, qty MENGE, date LFDAT). In-house materials yield planned orders;
     externally-procured ones yield purchase reqs; the BOM explosion shows as dependent requirements.

DESTRUCTIVE -- HUMAN GATE: create_demand and run_mrp CREATE orders and RE-PLAN (NOT idempotent).
Before calling either, STATE exactly what you will do (material, plant, demand qty) and get an
explicit go-ahead; only call after the user confirms. Pass the material as the user typed it
(e.g. 11070) -- the server zero-pads numeric materials ITSELF; do NOT pad it on your side.

The plan is the trigger for the boardroom / re-genesis loop -- surface anything notable (long
lead times, a purchase req that flags a supply risk, missing coverage) for that review.
""" + knowledge_block() + _NARRATION
    return LlmAgent(
        model=LiteLlm(model=f"openai/{LLM_MODEL_WRITE}"),    # consequential writes -> stronger model
        name="Planning",
        instruction=instruction,
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
        before_model_callback=_trim_context,
        tools=[McpToolset(connection_params=planning_server_params,
                          tool_filter=["run_mrp", "create_demand", "read_mrp_results"]),
               McpToolset(connection_params=sap_server_params, tool_filter=READONLY_SAP_TOOLS),
               remember],
    )


# ===== Role assessors -- standalone, reusable agents (the design's "Lego blocks") =====
# Each role is built by build_assessor_agent, so the SAME Engineering/Procurement/Compliance/Finance
# agent can serve the boardroom (parallel debate) now and the re-genesis loop later. They JUDGE the
# Conductor's grounded dossier and emit a COMPACT STRUCTURED verdict (facts by code, judgment by the
# board) -- no per-assessor tool calls, so the four run lean and the Chair consumes structure, not prose.
_ASSESSORS = {  # lens -> (model env var, cheap default model, output_key, focus)
    "Engineering": ("OPENAI_MODEL_ENGG", "gpt-4o-mini", "engg",
        "your task is BUILDABILITY -- leave CoO/trade to Compliance and price to Finance. FIRST, scan "
        "EVERY component's QUANTITY against world-knowledge -- this is mandatory, do it before anything "
        "else: a normally-SINGULAR part (keyboard, motherboard/mainboard, optical/CD drive, "
        "screen/display, chassis) at qty>1 is an ERROR -> ESCALATE it by name (e.g. 'Keyboard x2 on a "
        "laptop -- should be 1'); normally-MANY parts (RAM, cooling fans, screws, cables, connectors) at "
        "qty>1 are FINE -- do NOT flag those. THEN apply your engineering knowledge to the rest of the "
        "composition: a part that does not belong, a missing essential part, or a made FERT/HALB lacking "
        "a routing / work-scheduling view. A world-knowledge call has no policy to cite -- cite your "
        "engineering reasoning."),
    "Procurement": ("OPENAI_MODEL_PROC", "gpt-5-nano", "proc",
        "the PURCHASE REQUISITIONS -- supplier risk, single-sourcing, lead times, vendor concentration "
        "(e.g. every bought part on vendor 17300001)."),
    "Compliance": ("OPENAI_MODEL_COMP", "gpt-4o-mini", "comp",
        "trade & regulatory -- COUNTRY OF ORIGIN for EVERY component, export/trade compliance, "
        "restricted/hazardous parts, missing master data, auditability."),
    "Finance": ("OPENAI_MODEL_FIN", "gpt-5-nano", "fin",
        "COST & cash -- rolled cost vs price, purchase prices/conditions, working capital in the "
        "planned orders + purchase reqs, margin and overrun risk."),
}


def _board_grounding_tools():
    """Read-only grounding the assessors CALL -- the KG (graph) + the policy/rules readers."""
    return [McpToolset(connection_params=graph_server_params),
            McpToolset(connection_params=assurance_server_params, tool_filter=["read_policy", "read_rules"])]


def build_assessor_agent(lens: str):
    """A standalone role ASSESSOR (Engineering / Procurement / Compliance / Finance) -- a first-class,
    reusable agent. The boardroom runs the four in parallel; the re-genesis loop can invoke one alone.
    Each runs on its OWN (cheap) model and JUDGES the Conductor's already-grounded dossier (facts by
    code, judgment by the board), emitting a COMPACT STRUCTURED verdict the Chair consumes without
    wading through four prose monologues. NO tools: the grounding is the dossier's deterministic
    findings + component facts -- one grounding pass (the Conductor), not five (= leaner context)."""
    env, default_model, key, focus = _ASSESSORS[lens]
    return LlmAgent(
        model=LiteLlm(model=f"openai/{os.getenv(env, default_model)}"),
        name=lens, output_key=key,
        generate_content_config=types.GenerateContentConfig(temperature=0.3),
        before_model_callback=_trim_context,
        instruction=f"""You are the {lens} ASSESSOR in an assurance review. You are NOT in a meeting:
never 'convene'/'schedule', never invent participants or dates. You JUDGE data.

THE GROUNDED DOSSIER (facts + DETERMINISTIC findings; each finding already carries the policy id it is
'against', and each component its description + quantity):
{{dossier}}

From the {lens} lens, {focus}
Judge the dossier's findings in your lens, AND apply your own {lens} world-knowledge to the component
list -- the descriptions + quantities are right there. The grounding IS the dossier: never assert a
fact it does not contain.
STAY IN YOUR LANE: report ONLY issues that belong to the {lens} lens. Do NOT echo a finding another
lens owns -- country-of-origin / trade compliance is COMPLIANCE's alone; if you are Procurement or
Finance, SKIP every CoO line unless it carries a real sourcing or cost angle. The board must be four
DIFFERENT views, not four copies of the same list.

OUTPUT -- one line PER issue, in EXACTLY this pipe format and NOTHING else (no preamble, no prose):
  DISPOSITION | component | severity | issue | evidence
where:
  DISPOSITION = ACCEPT | ALTERNATE | ESCALATE   (ACCEPT=ok; ALTERNATE=a concrete fix; ESCALATE=needs a human)
  component   = the matnr, or (assembly) for a whole-assembly issue
  severity    = info | warning | error   (use the finding's severity; for a world-knowledge call you judge it)
  issue       = the one-line problem
  evidence    = the policy id you cite (e.g. P-CoO, P-SRC), or 'world-knowledge: <reason>' when no policy applies
Emit 1-5 lines. If your lens finds nothing wrong, emit EXACTLY one line:
  ACCEPT | (assembly) | info | no {lens} issues | dossier reviewed""",
    )


def build_boardroom_agent():
    """BOARDROOM (Track 5): a PARALLEL expert critique of the current MRP plan. Four critics --
    Engineering / Procurement / Compliance / Finance -- review the plan (planned orders, purchase
    reqs, dependent requirements, exceptions) already in the conversation IN PARALLEL, then a Chair
    synthesizes a board verdict and flags re-genesis candidates. The first real parallel-agent
    structure in the app; read-only (no tools, no writes) -- pure reasoning over the plan.

    Shape:  Sequential[ Parallel[Engg, Proc, Comp, Fin] -> Chair ].
    Each critic writes its critique to an output_key; the Chair reads {engg}/{proc}/{comp}/{fin}."""
    # The Conductor is the ONE boardroom role doing real multi-tool orchestration, and CHEAP models
    # LOOP on it -- gpt-4o-mini keeps calling read tools and never converges to the dossier (seen live:
    # list_fields x90, then get_material x50, never finishing). So it gets its OWN knob, defaulting to
    # gpt-4o; the assessors + Chair stay cheap. (Set OPENAI_MODEL_CONDUCTOR=gpt-4o-mini to force cheap --
    # it WILL loop. The durable cheap-able fix is a DETERMINISTIC Conductor: read_mrp_results +
    # assure_assembly are just two calls + formatting -- no LLM needed to fetch evidence.)
    conductor_model = LiteLlm(model=f"openai/{os.getenv('OPENAI_MODEL_CONDUCTOR', 'gpt-4o')}")

    # CONDUCTOR: fetches the EVIDENCE (read_mrp_results + assure_assembly) -> the dossier. The cheap
    # model role-played a "meeting" instead of calling tools, so: stronger model + no meeting language
    # + an explicit "you MUST call the tools" mandate.
    conductor = LlmAgent(
        model=conductor_model, name="Conductor", output_key="dossier",
        generate_content_config=types.GenerateContentConfig(temperature=0.0),
        before_model_callback=_trim_context,
        instruction="""You FETCH EVIDENCE for an assurance review. You are NOT in a meeting: never
'convene', 'schedule', or describe a meeting, and never invent participants or dates. Your output is
a data dossier built FROM TOOL CALLS.
1. Identify the finished-good material + plant (default 1710) from the conversation.
2. You MUST call read_mrp_results(material, plant) -- the plan + its components; collect the component
   material numbers.
3. You MUST call assure_assembly(material, plant, components=<those numbers>) -- it runs the
   DETERMINISTIC policy + rule checks over the real objects and returns the structured FINDINGS.
4. Output the DOSSIER (markdown):
   - **Plan:** <FG> @ <plant> -- planned order(s), purchase-req count, component count.
   - **Components (ALL, numbered):** each as `<description> (<matnr>) x<bom_quantity>` + procurement
     (E in-house / F bought) + country of origin. ALWAYS show the quantity from the facts -- it matters
     (a Keyboard x2 on a laptop is suspicious; two cooling fans is normal).
   - **DETERMINISTIC FINDINGS:** copy EVERY finding verbatim (object, fact, severity, against, verdict),
     errors first, with the summary counts.
   Copy findings exactly -- add, soften, omit nothing. If a tool fails, say so and mark PARTIAL.
Output ONLY the dossier. If you did not call the tools, you have failed the task.""",
        # NARROW tools on purpose: read_mrp_results + assure_assembly do the work. The broad
        # READONLY_SAP_TOOLS handed the cheap model the METADATA tool `list_fields`, which it looped
        # on forever on a clean session (90+ identical calls, never reaching the data tools).
        # get_material is the one safe extra read it might want.
        tools=[McpToolset(connection_params=planning_server_params, tool_filter=["read_mrp_results"]),
               McpToolset(connection_params=assurance_server_params, tool_filter=["assure_assembly"]),
               McpToolset(connection_params=sap_server_params, tool_filter=["get_material"])],
    )

    # The four role assessors are now STANDALONE, reusable agents -- see build_assessor_agent above.

    chair = LlmAgent(
        model=LiteLlm(model=f"openai/{LLM_MODEL}"), name="Chair", output_key="verdict",
        generate_content_config=types.GenerateContentConfig(temperature=0.2),
        before_model_callback=_trim_context,
        instruction="""You are the Chair. You ADJUDICATE findings. You are NOT in a meeting: never
invent participants/dates or describe a meeting.
The grounded dossier (DETERMINISTIC findings -- severity is a policy fact, not yours to change):
{dossier}

The assessors' STRUCTURED verdicts (each line: DISPOSITION | component | severity | issue | evidence):
- Engineering: {engg}
- Procurement: {proc}
- Compliance: {comp}
- Finance: {fin}

Parse those lines. Merge duplicates (same component+issue raised by more than one lens -> note both).
The strongest disposition wins (ESCALATE > ALTERNATE > ACCEPT).

BOARD VERDICT (markdown):
1. **Disposition** -- per ESCALATE/ALTERNATE line and per ERROR/REVIEW finding: ACCEPT (rationale) /
   ALTERNATE (fix) / ESCALATE (human). Apply SYSTEMIC gaps (e.g. country of origin) across ALL affected components.
2. **Top risks** -- the 3 most material, naming which lens(es) raised each + the evidence cited.
3. **Re-genesis candidates** -- components to swap/re-source (the hand-off to the re-genesis loop), and why.
Judge what to DO about the findings; do not override severities or invent meetings.""",
    )

    panel = ParallelAgent(name="Boardroom",
                          sub_agents=[build_assessor_agent(lens) for lens in _ASSESSORS])
    return SequentialAgent(name="BoardroomReview", sub_agents=[conductor, panel, chair])


def transcribe_audio(data_b64: str, mime: str) -> str:
    """Speech-to-text via OpenAI Whisper. A plain model call -- no agent, no tool.

    Runs BEFORE the agent: audio in -> text out. The agent only ever sees text.
    """
    audio_bytes = base64.b64decode(data_b64)
    ext = (mime.split("/")[-1].split(";")[0]) or "webm"
    buf = BytesIO(audio_bytes)
    buf.name = f"audio.{ext}"          # the API infers the format from the name
    result = openai_client.audio.transcriptions.create(model="whisper-1", file=buf)
    return result.text


async def build_user_content(raw: str) -> tuple[types.Content, str | None]:
    """Turn the WebSocket envelope into ADK Content for one user turn.

    Envelope (JSON): {"text": str, "image": {mime,data}?, "audio": {mime,data}?}
      - audio -> transcribed to text here (preprocessing model call, not a tool)
      - image -> attached inline so GPT-4o's OWN vision reads it (no extra model)
      - a plain non-JSON string is still accepted as text (backward compatible)

    Returns (content, status_note); status_note is echoed to the UI so the user
    can see what was heard / attached and correct us if needed.
    """
    try:
        env = json.loads(raw)
        if not isinstance(env, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return types.Content(role="user", parts=[types.Part(text=raw)]), None

    segments: list[str] = []
    notes: list[str] = []
    typed = (env.get("text") or "").strip()
    if typed:
        segments.append(typed)

    audio = env.get("audio")
    if audio and audio.get("data"):
        try:
            transcript = (await asyncio.to_thread(
                transcribe_audio, audio["data"],
                audio.get("mime", "audio/webm"))).strip()
            if transcript:
                segments.append(transcript)
                notes.append(f'heard: "{transcript}"')
        except Exception as e:  # surface to the UI rather than dropping the turn
            notes.append(f"transcription failed: {e}")

    prompt = "\n".join(segments).strip()
    parts: list[types.Part] = []
    image = env.get("image")
    if image and image.get("data"):
        if not prompt:
            prompt = ("Identify the product in this image and search SAP for "
                      "matching materials using search_materials.")
        parts.append(types.Part(text=prompt))
        try:
            parts.append(types.Part.from_bytes(
                data=base64.b64decode(image["data"]),
                mime_type=image.get("mime", "image/jpeg")))
            notes.append("image attached")
        except Exception as e:
            notes.append(f"image decode failed: {e}")
    else:
        parts.append(types.Part(text=prompt))

    return types.Content(role="user", parts=parts), (" · ".join(notes) or None)





async def process_message_with_runner(runner: Runner, session_id: str,
                                      content: types.Content):
    """Run one user turn (already built into ADK Content) through the agent."""
    events_async = runner.run_async(
        session_id=session_id, user_id=session_id, new_message=content
    )

    response_parts = []
    async for event in events_async:
        ec = event.content
        if ec and ec.role == "model" and ec.parts and ec.parts[0].text:
            print("[agent]:", ec.parts[0].text)
            response_parts.append(ec.parts[0].text)

    return response_parts


def _content_summary(content: types.Content) -> dict:
    """Compact description of the user's turn for the log header."""
    parts = content.parts or []
    text = " ".join(p.text for p in parts if getattr(p, "text", None))
    return {"text": text[:300],
            "image": any(getattr(p, "inline_data", None) for p in parts)}


def _trim_steps(steps: list[dict]) -> list[dict]:
    """Trim step payloads for transport to the UI Activity panel (keep them light)."""
    out = []
    for s in steps:
        t = dict(s)
        if "result" in t:
            t["result"] = " ".join(str(t["result"]).split())[:400]
        if "lines" in t:                             # orchestrator sub-steps (keep, lightly capped)
            t["lines"] = [" ".join(str(ln).split())[:200] for ln in t["lines"][:80]]
        if "text" in t:
            t["text"] = " ".join(str(t["text"]).split())[:400]
        if "args" in t:
            t["args"] = {k: str(v)[:120] for k, v in (t.get("args") or {}).items()}
        t.pop("card", None)                          # the card travels in "data", not the trace
        out.append(t)
    return out


def _detect_gate(steps: list[dict], final: str, intent: str):
    """A WRITE-GATE moment: a make/genesis/create agent produced a PREVIEW (confirm=false) and is
    waiting for approval. The UI then renders an EXPLICIT confirm gate -- the restated write with
    Approve/Reject, operable by click OR voice -- so no casual 'yes' silently writes to S/4. Detected
    from the 'nothing written ... confirm' marker the gated tools emit on a preview."""
    if intent not in ("genesis", "make", "create_change"):
        return None
    for s in steps:
        if s.get("kind") == "tool_result":
            res = str(s.get("result", "")).lower()
            if "nothing written" in res and "confirm" in res:
                return {"summary": (final or "").strip()[:2000], "intent": intent}
    return None


_DATA_SENTINEL = "@@DATA@@"     # tools may append <readable text>@@DATA@@<json> -- see genesis.py


def _unwrap(resp):
    """The raw text of an MCP tool response (unwraps {'content':[{'text':...}]})."""
    if isinstance(resp, str):
        return resp
    c = resp.get("content") if isinstance(resp, dict) else getattr(resp, "content", None)
    if isinstance(c, list) and c:
        first = c[0]
        t = first.get("text") if isinstance(first, dict) else getattr(first, "text", None)
        if isinstance(t, str):
            return t
    return None


def _parse_tool_payload(resp):
    """Extract a JSON object/list from an MCP tool response, for the Data panel. Handles two
    shapes: a whole-JSON response, OR a readable report with a structured block after @@DATA@@."""
    text = _unwrap(resp)
    if not isinstance(text, str):
        return None
    if _DATA_SENTINEL in text:                       # readable text + appended structured block
        text = text.split(_DATA_SENTINEL, 1)[1].strip()
    if text[:1] not in "{[":
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _tool_text(resp) -> str:
    """Human-readable text of an MCP tool response. Strips any @@DATA@@ structured block so the
    Activity panel / chat / gate see only the readable report, never the UI-only JSON."""
    text = _unwrap(resp)
    if not isinstance(text, str):
        return str(resp)
    if _DATA_SENTINEL in text:
        text = text.split(_DATA_SENTINEL, 1)[0].rstrip()
    return text


# A turn can produce SEVERAL structured cards (e.g. read a material AND preview its PIR). The Data
# panel shows ALL card-worthy results -- and ONLY them: raw exploration dumps (explore_entity,
# list_fields, find_field, read_policy, list_concepts, requirements_for...) are intentionally kept
# OUT of the cards (they live in the Activity panel) so they can't bury the material/PIR/BOM cards.
_CARD_KINDS = {"genesis", "pir", "cost", "bom", "routing"}     # tools that append @@DATA@@{kind:...}
_CARD_TOOLS = {"get_material", "build_material_payload", "create_material", "run_mrp",
               "semantic_search", "find_duplicates", "search_materials", "create_demand",
               "assure_assembly"}                              # tools whose whole result is a card


def _is_card(tool: str, payload) -> bool:
    """True if this tool result renders as a typed card (vs. raw exploration JSON)."""
    if isinstance(payload, dict) and payload.get("kind") in _CARD_KINDS:
        return True
    # A 0-match search is not a card: genesis fires search_materials once per component to dedup-
    # check, all 0-match -> they'd flood the Data panel with empty cards (and persist into the trace).
    # The GenesisCard rolls them up; a real user search with 0 hits is reported in the agent's text.
    if tool == "search_materials" and isinstance(payload, dict) and not (
            payload.get("materials") or payload.get("total_matches")):
        return False
    return tool in _CARD_TOOLS and isinstance(payload, (dict, list))


def _card_list(candidates: list) -> list:
    """Every card-worthy payload from a turn, in order, de-duped, capped."""
    out, seen = [], set()
    for name, obj in candidates:
        if not _is_card(name, obj):
            continue
        try:
            key = (name, json.dumps(obj, sort_keys=True, default=str)[:300])
        except Exception:
            key = (name, id(obj))
        if key in seen:
            continue
        seen.add(key)
        out.append({"tool": name, "payload": obj})
    return out[-10:]     # cap so a long multi-tool turn can't flood the pane


async def run_with_trace(runner: Runner, session_id: str, content: types.Content,
                         intent: str = ""):
    """Run ONE turn, capturing a full trace -- each agent's text, every tool call with
    its arguments, and every tool result (with duration, and orchestrator reports split
    into sub-steps). Appends a JSONL session log (logs/session_<id>.jsonl) and returns
    (final_answer, steps, primary_data). The final answer is the LAST agent's text."""
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    steps: list[dict] = []
    data_candidates: list = []          # (tool_name, parsed_json) for the Data panel
    pending: dict = {}                  # tool name -> call start time, for per-step timing
    final = ""
    async for event in runner.run_async(
        session_id=session_id, user_id=session_id, new_message=content,
        run_config=RunConfig(max_llm_calls=30),    # hard backstop: a cheap model can't run away forever
    ):
        who = getattr(event, "author", "") or "?"
        ec = getattr(event, "content", None)
        for p in ((getattr(ec, "parts", None) or []) if ec else []):
            if getattr(p, "text", None) and p.text.strip():
                steps.append({"author": who, "kind": "text", "text": p.text})
                if p.text.lstrip()[:1] not in ("💭", "✦"):   # narration lines aren't the answer
                    final = p.text                   # last non-narration text wins
            fc = getattr(p, "function_call", None)
            if fc:
                pending[fc.name] = datetime.now()
                steps.append({"author": who, "kind": "tool_call",
                              "tool": fc.name, "args": dict(fc.args or {})})
            fr = getattr(p, "function_response", None)
            if fr:
                start = pending.pop(fr.name, None)
                dur = round((datetime.now() - start).total_seconds(), 1) if start else None
                clean = _tool_text(fr.response)
                lines = [ln.rstrip() for ln in clean.split("\n") if ln.strip()]
                step = {"author": who, "kind": "tool_result", "tool": fr.name,
                        "result": clean, "dur": dur}
                if len(lines) > 1:                   # a multi-line report (e.g. genesis) ->
                    step["lines"] = lines            # show each line as a discrete sub-step
                steps.append(step)
                parsed = _parse_tool_payload(fr.response)
                if parsed is not None:
                    data_candidates.append((fr.name, parsed))
                    if _is_card(fr.name, parsed):
                        step["card"] = parsed          # persist so reload/replay can re-render it

    with (LOG_DIR / f"session_{session_id}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": stamp, "intent": intent, "input": _content_summary(content)},
                           ensure_ascii=False) + "\n")
        for s in steps:
            rec = dict(s)
            if rec.get("kind") == "tool_result":
                rec["result"] = str(rec.get("result", ""))[:1000]
                if "lines" in rec:
                    rec["lines"] = [str(ln)[:200] for ln in rec["lines"][:80]]
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    logging.info(f"[trace] {session_id}: {len(steps)} steps -> logs/session_{session_id}.jsonl")
    return final, steps, _card_list(data_candidates)

_ROUTER_INTENTS = ("search", "create_change", "make", "genesis", "planning", "boardroom", "validate")

# A bare confirmation/continuation ("go ahead", "yes", "confirm") carries NO intent of its
# own -- it belongs to whatever the PREVIOUS turn was doing (e.g. confirming a make preview).
# Routing it fresh sends it to the wrong specialist, breaking every preview->confirm flow.
_CONTINUATION_RE = re.compile(
    r"^\s*(y|yes|yep|yeah|ya|ok|okay|k|sure|fine|go|go ahead|go for it|do it|proceed|continue|"
    r"confirm|confirmed|approve|approved|accept|create it|change it|update it|make it|write it|"
    r"save it|commit|send it|that'?s? (right|correct|fine|good)|looks good|lgtm|do that)\b[\s.!]*",
    re.IGNORECASE)


# A bare 'ok/yes' that actually LAUNCHES A DIFFERENT OPERATION is NOT a continuation -- e.g. after a
# genesis, "ok please create the demand and run mrp" must route to planning, not stick on genesis.
# These signals each map to a different specialist, so their presence vetoes the sticky shortcut.
_NEW_TASK_RE = re.compile(
    r"\b(mrp|demand|replan|re-plan|planned order|purchase req|"             # -> planning
    r"convene|board\b|critique|debate|stress[- ]?test|"                     # -> boardroom
    r"country of origin|\bcoo\b|"                                           # -> create_change (a field)
    r"production version)\b",                                              # -> make (MKAL)
    re.IGNORECASE)


def _is_continuation(text: str) -> bool:
    """True if `text` is a BARE confirmation/continuation of the previous step ('go ahead',
    'proceed', 'confirmed', 'proceed and extend to sales org 1710') -- so a confirm of a
    genesis/make/create flow can't get re-routed mid-write. BUT a turn that opens with 'ok/yes'
    and then launches a DIFFERENT operation (run MRP, create demand, update CoO, convene the board)
    is NOT a continuation -- it must reach the right specialist."""
    t = (text or "").strip()
    return (bool(t) and len(t.split()) <= 25
            and _CONTINUATION_RE.match(t) is not None
            and _NEW_TASK_RE.search(t) is None)


def classify_intent(text: str, has_image: bool, last_intent: str | None = None) -> str:
    """Route one request to a specialist. A fast, cheap classification (OPENAI_MODEL).

    STATEFUL so a preview->confirm flow keeps its thread:
      1. a bare confirmation/continuation ('go ahead', 'yes', 'confirm') deterministically
         KEEPS the previous intent -- it has no intent words of its own;
      2. the previous intent is also given to the classifier as context for gray cases.
    """
    # 1) deterministic: a short confirmation right after a real turn stays on that intent.
    if last_intent in _ROUTER_INTENTS and _is_continuation(text):
        logging.info(f"[router] continuation -> staying on '{last_intent}'")
        return last_intent

    context = ""
    if last_intent in _ROUTER_INTENTS:
        context = (f"CONTEXT: the previous turn was routed to '{last_intent}'. Keep '{last_intent}' "
                   "ONLY if THIS message is a bare confirmation or a refinement of that SAME step "
                   "(e.g. 'proceed, and extend it to sales org 1710'). If the user moves to a "
                   "DIFFERENT OPERATION -- even on the same material -- route to THAT operation: after "
                   "a genesis, 'create demand / run MRP / plan it' -> planning; 'update or set a field "
                   "(country of origin, description)' -> create_change; 'fix the BOM/PIR/routing' -> "
                   "make; 'review/convene the board' -> boardroom. Building a thing and then PLANNING "
                   "it (or CHANGING a field on it) are DIFFERENT intents -- do not glue them together.\n")
    prompt = (
        "Classify the user's request into ONE routing intent:\n"
        "- search: PURELY find/look up/list materials, semantic 'find something like', the "
        "ontology ('what does a FERT need'), plant/code lookups, web fact lookups -- with "
        "NO write.\n"
        "- create_change: create / change / update / set / extend a MATERIAL MASTER itself "
        "(its OWN fields, views, descriptions, sales/plant extension, country-of-origin) -- EVEN IF "
        "it looks something up first (e.g. 'find its weight and SET it', 'create this material from "
        "the image'). NOT its BOM / PIR / routing / cost / components -- those are 'make'.\n"
        "- make: build the supply-chain objects AROUND an existing material -- a purchase info "
        "record (PIR) / sourcing, a BOM (incl. add/remove a component), a routing, a cost "
        "estimate/condition, or the PRODUCTION VERSION (MKAL -- binds the material's BOM "
        "alternative + usage so it becomes MRP-ready). The material already exists; this creates "
        "its make objects -- INCLUDING fixing/editing existing ones. Phrases: 'create/update the "
        "production version', 'bind the BOM', 'FIX the BOM', 'remove/swap a component', 'fix the PIR', "
        "'correct the routing', 'production version with bom alternative 01 / bom usage 1 for the FG'.\n"
        "- planning: run MRP / PLAN a finished good, create demand (a sales order to plan "
        "against), or VIEW the plan -- planned orders / purchase requisitions / MRP results -- "
        "for a material. Use for 'run MRP', 'plan it', 'create demand', 'replan', 'show/list the "
        "planned orders or purchase reqs'.\n"
        "- genesis: from an IMAGE or parts-list of a WHOLE PRODUCT/ASSEMBLY, create the ENTIRE "
        "master-data set at once -- the parent material AND its component materials AND their "
        "PIRs/costs AND the BOM AND the routing. Use when the user wants to build/genesis an "
        "entire assembly from a picture or bill of parts (esp. with an image attachment).\n"
        "- boardroom: convene a PARALLEL expert review/critique/debate of the CURRENT plan -- four "
        "critics (Engineering, Procurement, Compliance, Finance) challenge the existing MRP plan and "
        "a chair synthesizes risks + actions. Use for 'review/critique/challenge/debate/stress-test "
        "the plan', 'convene the board', 'what do the experts/board think'.\n"
        "- validate: validate/check a proposed material against the rules.\n"
        + context +
        "Tiebreakers: writing the material's OWN fields/views/CoO -> create_change. ANYTHING about its "
        "PIR / BOM / routing / cost / components -- create OR fix/edit/remove/swap -> make (NOT "
        "create_change, even though the word is 'fix' or 'change'). The PRODUCTION VERSION (MKAL / 'production version' / "
        "binding a bom alternative + usage to make a material MRP-ready) -> make, NOT planning -- "
        "planning only RUNS MRP, it never CREATES the version. MRP / planned orders / purchase reqs / "
        "demand / replan (RUN or VIEW the plan) -> planning. CRITIQUE/REVIEW/DEBATE an existing plan -> "
        "boardroom. Whole assembly from an image/parts-list -> genesis. Looking up materials -> search.\n"
        f"Request: {text!r}" + (" [has an image attachment]" if has_image else "") + "\n"
        "Reply with ONLY one word: search, create_change, make, planning, genesis, boardroom, or validate."
    )
    try:
        resp = openai_client.chat.completions.create(
            model=LLM_MODEL, temperature=0, max_tokens=4,
            messages=[{"role": "user", "content": prompt}])
        label = resp.choices[0].message.content.strip().lower().strip(".")
        return label if label in _ROUTER_INTENTS else (last_intent or "search")
    except Exception as e:  # never block the turn on the router
        logging.warning(f"[router] classify failed ({e}); keeping '{last_intent or 'search'}'")
        return last_intent or "search"


async def run_adk_agent_session(websocket: WebSocket, session_id: str):
    """ROUTER session: classify each turn's intent and dispatch to the right specialist
    (search / create_change / validate). Runners are built lazily + cached per intent,
    so each MCP server is launched only when first needed."""
    builders = {
        "search": build_search_agent,            # exact + semantic + ontology + web
        "create_change": lambda: _with_bom_guard(build_create_pipeline(before_model_callback=_trim_context)),
        "make": build_make_agent,                # PIR / cost / BOM / routing (around a material)
        "genesis": build_genesis_agent,          # whole assembly from an image (run_genesis)
        "planning": build_planning_agent,        # MRP + demand via the REMOTE planning server
        "boardroom": build_boardroom_agent,      # PARALLEL Engg/Proc/Comp/Fin critique -> Chair
        "validate": build_validation_pipeline,   # the rule engine
    }
    runners: dict = {}

    def get_runner(intent: str) -> Runner:
        if intent not in runners:
            runners[intent] = Runner(app_name=APP_NAME, agent=builders[intent](),
                                     plugins=[_TRIM_PLUGIN],     # trims EVERY agent (incl. rule engine)
                                     artifact_service=artifacts_service,
                                     session_service=session_service)
            logging.info(f"[router] built runner for '{intent}'")
        return runners[intent]

    logging.info(f"Router session started for {session_id}.")
    last_intent: str | None = None     # sticky: confirmations keep the previous specialist
    try:
        while True:
            raw = await websocket.receive_text()
            logging.info(f"Received from {session_id}: {raw[:120]}")
            try:                                          # CONFIRM-GATE audit: HOW was a write approved?
                _env = json.loads(raw)
                if isinstance(_env, dict) and _env.get("modality"):
                    logging.info(f"[gate] confirmation via {str(_env['modality']).upper()}: {str(_env.get('text',''))[:60]!r}")
            except (json.JSONDecodeError, TypeError):
                pass
            content, note = await build_user_content(raw)
            if note:
                await websocket.send_text(json.dumps({"type": "status", "text": note}))

            summary = _content_summary(content)
            # --- LEARNING capture trigger: /reflect [session_id] or "what did you learn" -> Reflector, skip the run ---
            if learning.ENABLED and summary["text"] and _REFLECT_RE.search(summary["text"]):
                _sid_m = re.search(r"/reflect\s+([A-Za-z0-9_-]{6,})", summary["text"])
                if _sid_m:                              # backfill a past recorded session by id
                    _sid = _sid_m.group(1)
                    kept = await asyncio.to_thread(learning.reflect_session, _sid)
                    head = (f"🧠 **Backfilled session `{_sid}`** — captured {len(kept)} lesson(s):\n" if kept
                            else f"🧠 No durable lessons in session `{_sid}` (or no trace on file).")
                else:                                   # reflect on the live, in-progress session
                    kept = await asyncio.to_thread(learning.reflect, last_intent or "session",
                                                   _SESSION_STEPS.get(session_id, []), "reflect")
                    head = (f"🧠 **Captured {len(kept)} lesson(s)** for future sessions:\n" if kept
                            else "🧠 Nothing durable to capture from this session yet.")
                body = head + ("\n".join("- (" + k["lesson_type"] + ") " + (k["correction"] or k["mistake_or_insight"])
                                         for k in kept) if kept else "")
                await websocket.send_text(json.dumps(
                    {"type": "turn", "intent": "reflect", "answer": body, "trace": [], "data": None}))
                continue
            intent = await asyncio.to_thread(
                classify_intent, summary["text"], summary["image"], last_intent)
            last_intent = intent
            logging.info(f"[router] {session_id}: intent='{intent}'")
            # --- LEARNING: capture a correction of the PREVIOUS answer; recall lessons for THIS turn ---
            _RUN_LESSONS[session_id] = []
            _turn_tok = ""
            if learning.ENABLED:
                await asyncio.to_thread(learning.capture_correction, _PREV_INTENT.get(session_id, ""),
                                        summary["text"], _LAST_ANSWER.get(session_id, ""))
                _lessons = await asyncio.to_thread(learning.recall, intent, summary["text"])
                _RUN_LESSONS[session_id] = _lessons
                _block = learning.format_block(_lessons)
                _LESSON_BLOCK.set(_block)
                _LAST_LESSON_BLOCK[0] = _block       # mirror for the boardroom's parallel assessors
                _TURN_SEQ[0] += 1
                _turn_tok = f"{session_id}:{_TURN_SEQ[0]}"   # the callback confirms injection against this
                _TURN_TOKEN.set(_turn_tok)
                _INJECTED_TOKENS.discard(_turn_tok)
            runner = get_runner(intent)

            try:
                if intent == "validate":  # rule engine assembles its own verdict + diagnostic
                    rendered = await run_rule_engine(runner, session_service, APP_NAME, session_id, content)
                    await websocket.send_text(json.dumps(
                        {"type": "turn", "intent": intent, "answer": rendered,
                         "trace": [], "data": None}))
                else:
                    final, steps, data = await run_with_trace(runner, session_id, content, intent)
                    msg = {"type": "turn", "intent": intent,
                           "answer": final or "(no answer)", "trace": _trim_steps(steps),
                           "data": data, "gate": _detect_gate(steps, final, intent)}
                    if learning.ENABLED:                  # measure: the one "Learning" line for the trace panel
                        _SESSION_STEPS.setdefault(session_id, []).extend(steps)
                        _LAST_ANSWER[session_id] = final or ""
                        _PREV_INTENT[session_id] = intent
                        recalled = _RUN_LESSONS.get(session_id, [])
                        injected_ok = _turn_tok in _INJECTED_TOKENS   # request-level: did the block actually land?
                        _INJECTED_TOKENS.discard(_turn_tok)           # cleanup (the set never grows)
                        inj = recalled if injected_ok else []   # COUNTER BEHIND THE CHECK -- no over-report
                        avoided = _AVOIDED_COUNT.pop(_turn_tok, 0)    # guard blocks (reliable; not trace-scraped)
                        repeated = _count_repeated_mistakes(steps) if intent == "create_change" else 0
                        msg["learning"] = {"line": learning.learning_line(inj, repeated, avoided),
                                           "injected": len(inj), "avoided": avoided, "repeated": repeated}
                        if inj:
                            learning.log_event("lesson_injected", {"intent": intent, "n": len(inj),
                                                                   "ids": [le["id"] for le in inj]})
                        elif recalled:
                            logging.warning(f"[learning] recalled {len(recalled)} lesson(s) but injection NOT "
                                            f"confirmed at request level (intent '{intent}')")
                        if repeated:
                            learning.log_event("mistake_repeated", {"intent": intent, "n": repeated})
                        if (avoided or repeated) and intent in ("create_change", "make"):
                            learning.seed_ledger_before()          # the BEFORE half (on file)
                            learning.record_ledger("create-material flow attempts BOM-item write",
                                                   "after" if avoided else "repeat",
                                                   {"intent": intent, "blocked_by_guard": avoided,
                                                    "slipped_through": repeated})
                    await websocket.send_text(json.dumps(msg))
            except Exception as run_err:
                # A failed run (e.g. the model's context window overflowed) must NOT kill the socket --
                # surface it, clear the client's "working" cue, and keep the session alive for the next turn.
                logging.exception(f"[run] {session_id} intent='{intent}' failed")
                low = str(run_err).lower()
                if "context" in low and ("length" in low or "window" in low or "token" in low):
                    msg = ("⚠ This conversation grew past the model's context window. I trim history, "
                           "but it still overflowed — please start a NEW session (🗂 session bar) to "
                           "continue cleanly; this one's history is too large.")
                elif "llm" in low and "call" in low and "limit" in low:
                    msg = ("⚠ An agent looped and was stopped at the 30-call safety cap — a cheap model "
                           "got stuck calling a tool repeatedly. Try again (often transient) or a fresh "
                           "session; if it persists, that agent's tools need narrowing.")
                else:
                    msg = f"⚠ That run hit an error and was stopped: {type(run_err).__name__}: {str(run_err)[:300]}"
                await websocket.send_text(json.dumps({"type": "error", "message": msg}))

    except WebSocketDisconnect:
        logging.info(f"Client {session_id} disconnected.")
    finally:
        # --- LEARNING: reflect on the whole session at close (deterministic capture hook) ---
        if learning.ENABLED and _SESSION_STEPS.get(session_id):
            try:
                await asyncio.to_thread(learning.reflect, last_intent or "session",
                                        _SESSION_STEPS.get(session_id, []), "close")
            except Exception as e:
                logging.warning(f"[learning] close reflect failed: {e}")
        for _d in (_SESSION_STEPS, _RUN_LESSONS, _LAST_ANSWER, _PREV_INTENT):
            _d.pop(session_id, None)
        # Close all per-intent runners CONCURRENTLY, each bounded by a short timeout, so
        # shutdown can't hang on a slow MCP stdio-subprocess teardown. A session may have
        # several servers up (sap/make/vector/graph/serper); closing them serially with no
        # bound is what made Ctrl+C take "ages". This caps it to a few seconds total.
        async def _close(r):
            try:
                await asyncio.wait_for(r.close(), timeout=3)
            except Exception as e:
                logging.warning(f"[shutdown] runner close timed out/failed: {e}")

        if runners:
            await asyncio.gather(*(_close(r) for r in runners.values()))
        logging.info(f"Router session for {session_id} closed ({len(runners)} runners).")


# FastAPI web app
@asynccontextmanager
async def lifespan(app: FastAPI):
    """On startup, quiet the Windows Proactor loop's benign ConnectionResetError
    (WinError 10054) -- it's logged at ERROR with a traceback whenever a socket/pipe is
    torn down abruptly (browser disconnect, MCP subprocess shutdown), but it's harmless
    (real disconnects are handled via WebSocketDisconnect). We can't use the Selector loop
    (it can't spawn the MCP stdio subprocesses on Windows), so we filter just this one
    error and defer everything else to asyncio's default handler."""
    loop = asyncio.get_running_loop()

    def _handler(loop, context):
        if isinstance(context.get("exception"), ConnectionResetError):
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)
    yield


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """Client websocket endpoint"""
    await websocket.accept()
    logging.info(f"Client #{session_id} connected and WebSocket accepted.")

    try:
        # Check for existing session and reuse it, or create a new one
        existing = await session_service.get_session(
            app_name=APP_NAME,
            user_id=session_id,
            session_id=session_id,
        )
        if existing is None:
            await session_service.create_session(
                app_name=APP_NAME,
                user_id=session_id,
                session_id=session_id,
                state={}
            )
            logging.info(f"ADK Session created for {session_id}.")
        else:
            logging.info(f"ADK Session reused for {session_id}.")

        await run_adk_agent_session(websocket, session_id)

    except WebSocketDisconnect:
        logging.info(f"WebSocket endpoint for {session_id} detected disconnect.")
    finally:
        logging.info(f"WebSocket endpoint for session {session_id} is concluding.")


# ---- session browser API (durable sessions) -----------------------------------------
# user_id == session_id throughout this app (one user per conversation), and
# list_sessions(user_id=None) returns ALL sessions for the app -> the browser lists everything.
def _event_text(ev) -> str:
    """Concatenated TEXT of an event's parts ('' for pure tool-call/result events)."""
    parts = getattr(getattr(ev, "content", None), "parts", None) or []
    return "".join(getattr(p, "text", "") or "" for p in parts).strip()


def _session_summary(full) -> tuple[str, int]:
    """(title, user_turn_count) for the list -- title = first user message, truncated."""
    if not full or not full.events:
        return ("(empty session)", 0)
    title, turns = "", 0
    for ev in full.events:
        role = getattr(getattr(ev, "content", None), "role", None)
        txt = _event_text(ev)
        if role == "user" and txt:
            turns += 1
            if not title:
                title = txt[:70]
    return (title or "(no text yet)", turns)


def _events_to_turns(events) -> list:
    """Flatten events to chat turns [{role, text, image?}] -- skips pure tool-call/result events.
    Sets an `image` marker on user turns that carried an inline image, so a reloaded session SHOWS
    that a picture was pasted (the bytes are in the stored event; we surface only a lightweight flag)."""
    out = []
    for ev in events or []:
        role = getattr(getattr(ev, "content", None), "role", None)
        parts = getattr(getattr(ev, "content", None), "parts", None) or []
        txt = _event_text(ev)
        has_img = role == "user" and any(getattr(p, "inline_data", None) for p in parts)
        if not txt and not has_img:
            continue
        turn = {"role": "user" if role == "user" else "agent", "text": txt}
        if has_img:
            turn["image"] = True            # marker only (a thumbnail would re-encode the bytes)
        out.append(turn)
    return out


@app.get("/api/sessions")
async def api_list_sessions():
    """List every persisted session (newest first) with a title + turn count for the browser."""
    resp = await session_service.list_sessions(app_name=APP_NAME, user_id=None)
    items = []
    for meta in resp.sessions:
        full = await session_service.get_session(
            app_name=APP_NAME, user_id=meta.user_id, session_id=meta.id)
        title, turns = _session_summary(full)
        items.append({"id": meta.id, "title": title, "turns": turns,
                      "updated": getattr(full, "last_update_time", None)
                                 or getattr(meta, "last_update_time", None)})
    items.sort(key=lambda x: x["updated"] or 0, reverse=True)
    return items


@app.get("/api/sessions/{sid}/events")
async def api_session_events(sid: str):
    """The conversation transcript for one session, so the UI can reload it into the chat."""
    full = await session_service.get_session(app_name=APP_NAME, user_id=sid, session_id=sid)
    return _events_to_turns(full.events if full else [])


@app.get("/api/sessions/{sid}/trace")
async def api_session_trace(sid: str):
    """Per-turn agent activity (tool calls, results, sub-steps, timing) replayed from the
    session's JSONL trace log, so reopening a session restores the Activity panel too."""
    path = LOG_DIR / f"session_{sid}.jsonl"
    if not path.exists():
        return []
    turns, cur = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "input" in rec:                          # a turn header record
            cur = {"intent": rec.get("intent", ""), "input": rec.get("input", {}), "steps": []}
            turns.append(cur)
        elif cur is not None:
            # Strip 0-match search_materials cards persisted before the _is_card fix, so reloading
            # an OLD session no longer floods the Data panel with empty genesis dedup-probe cards.
            if rec.get("tool") == "search_materials" and isinstance(rec.get("card"), dict) and not (
                    rec["card"].get("materials") or rec["card"].get("total_matches")):
                rec.pop("card", None)
            cur["steps"].append(rec)
    return turns


@app.get("/api/logs")
async def api_logs(limit: int = 200, level: str = "", contains: str = ""):
    """Recent backend log records (ring buffer). Filter with ?level=WARNING and/or
    ?contains=[ctx] ; ?limit caps how many of the most-recent matches are returned."""
    items = list(_LOG_RING)
    if level:
        lv = level.upper()
        items = [r for r in items if r["lvl"] == lv]
    if contains:
        c = contains.lower()
        items = [r for r in items if c in r["msg"].lower()]
    return {"count": len(items), "records": items[-max(1, min(limit, 2000)):]}


@app.get("/api/promotions")
async def api_promotions():
    """The learning loop's promotion queue -- lessons that recurred or were corrected, routed by type
    to a deterministic artifact (guard / assurance check / rule). Human-gated: 'queued' until a check
    is built and learning.apply_promotion() marks it 'applied' (which also retires the soft lesson)."""
    promos = learning.list_promotions()
    return {"queued": sum(1 for p in promos if p.get("status") == "queued"),
            "applied": sum(1 for p in promos if p.get("status") == "applied"),
            "promotions": sorted(promos, key=lambda p: p.get("status") != "queued")}


@app.post("/api/reflect_session/{sid}")
async def api_reflect_session(sid: str):
    """Retroactively run the Reflector over a saved session trace -- learn from a session that
    ran before D2M_LEARNING was on. No-op (with a reason) when the flag is off."""
    if not learning.ENABLED:
        return {"ok": False, "reason": "D2M_LEARNING is off", "lessons": []}
    if not (LOG_DIR / f"session_{sid}.jsonl").exists():
        return {"ok": False, "reason": f"no trace on file for {sid}", "lessons": []}
    kept = await asyncio.to_thread(learning.reflect_session, sid)
    return {"ok": True, "count": len(kept),
            "lessons": [{"type": k["lesson_type"], "intent": (k.get("applies_to") or {}).get("intent"),
                         "text": k["correction"] or k["mistake_or_insight"], "how": k.get("_how")}
                        for k in kept]}


@app.post("/api/explain")
async def api_explain(payload: dict):
    """AI EXPLAINER -- narrate the agent's actions for a BUSINESS USER. Takes a turn's trace (the
    steps the UI already holds) and returns a 2-4 sentence plain-language 'what I did + why'. One
    cheap gpt-4o-mini call, fired only on the 💡 button, so it costs nothing unless clicked."""
    steps = payload.get("steps") or []
    intent = payload.get("intent") or ""
    lines = []
    for s in steps:
        k = s.get("kind")
        if k == "tool_call":
            args = ", ".join(f"{kk}={str(vv)[:40]}" for kk, vv in (s.get("args") or {}).items())
            lines.append(f"- called {s.get('tool')}({args})")
        elif k == "tool_result":
            lines.append(f"    -> {str(s.get('result', ''))[:220]}")
        elif k == "text":
            t = (s.get("text") or "").strip()
            if t and t[0] not in "💭✦":
                lines.append(f"- noted: {t[:220]}")
    trace_txt = "\n".join(lines)[:6000] or "(no tool actions -- a direct answer)"
    prompt = (
        "You explain to a BUSINESS USER what an AI assistant just did for them in an SAP system. "
        "Given the routing intent and the assistant's actions below, write a SHORT narration -- 2 to 4 "
        "sentences, FIRST PERSON ('I ...'), plain language, NO jargon, NO markdown, NO bullets. Explain "
        "WHAT you did and WHY in business terms (e.g. 'I read the laptop's bill of materials, saw two "
        "keyboard lines, and removed the duplicate so it has exactly one'). Never invent steps not shown.\n\n"
        f"Routing intent: {intent}\nActions:\n{trace_txt}"
    )
    try:
        resp = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model=LLM_MODEL, temperature=0.3, max_tokens=180,
            messages=[{"role": "user", "content": prompt}])
        return {"explanation": resp.choices[0].message.content.strip()}
    except Exception as e:                       # never break the UI on the explainer
        logging.warning(f"[explain] failed: {e}")
        return {"explanation": f"(could not generate an explanation: {e})"}


_TOUR_LABELS = {"intro": "Welcome", "multimodal": "Multimodal in", "genesis": "Genesis",
                "planning": "Planning", "board": "The board", "correction": "Self-correction",
                "discipline": "Discipline", "panels": "Your workspace", "cta": "Get started"}


# Curated narration -- served INSTANTLY so the tour never waits on the LLM. The LLM refreshes
# this in the background (see _tour_cache) so a later tour can use fresher phrasing.
_TOUR_BAKED = [
    {"id": "intro", "text": "I'm Design2Make. I take one real product and carry it all the way into SAP S/4HANA -- master data, a production plan, an expert review, and the fixes -- as a single connected loop, not a pile of disconnected steps."},
    {"id": "multimodal", "text": "I'm multimodal at the door. Drop a photo of a disassembled laptop and I read every labelled part -- keyboard, battery, the DDR memory, the cooling fans. You can speak to me or type. Pictures and voice, not just a chat box."},
    {"id": "genesis", "text": "From that one input I create the entire master-data set in S/4HANA in a single deterministic run -- the finished good, every component, their purchase-info-records and costs, the bill of materials, the routing, and a production version. Born ready to plan."},
    {"id": "planning", "text": "Then I run MRP against real demand -- planned orders for what we build, purchase requisitions for what we buy -- so the plan reflects the actual SAP system, not a guess on a whiteboard."},
    {"id": "board", "text": "An expert board -- Engineering, Procurement, Compliance, Finance -- reviews that plan grounded in the real SAP data. It catches what a checklist can't: two keyboards on a laptop, or a single vendor quietly supplying ninety percent of the parts."},
    {"id": "correction", "text": "When the board flags something, I don't just report it -- I fix it. I adjust the bill of materials and re-run the plan. A self-correcting loop, not a one-shot answer."},
    {"id": "discipline", "text": "Underneath, every step is traced, every confidence is calibrated, and every write to SAP passes an approval gate you confirm by click or by voice. Nothing reaches your system silently."},
    {"id": "panels", "text": "Your workspace is three columns: talk to me on the left, watch every agent action unfold in the middle, and see the structured SAP results build on the right."},
    {"id": "cta", "text": "So let's build something real. Drop a product image, or just ask me to plan a material -- and watch it go from a photo to a finished, reviewed production plan."},
]
_tour_cache = None          # LLM-refreshed narration once available; falls back to _TOUR_BAKED
_tour_refreshing = False    # guard so only one background refresh runs at a time


async def _generate_tour(focus: str):
    """Generate fresh tour narration via the LLM (slow, ~15s). Returns a list of {id,text} or None."""
    prompt = (
        "You are the guide for Design2Make, an AI agent system that turns a real product into SAP "
        "S/4HANA master data and a production plan -- the whole 'design -> make -> plan -> review -> "
        "fix -> re-plan' loop. Give a confident SPOKEN TOUR that conveys its DEPTH and BREADTH to a "
        "live audience. ONE compelling line of narration per section id below -- 25-45 words, first "
        "person, conversational, no markdown/bullets/headings. Make it feel like a system that thinks "
        "and acts, not a chatbot. Use a concrete laptop example where it helps.\n"
        "- intro: what Design2Make is and the vision -- one product to SAP master data and a plan, as one loop.\n"
        "- multimodal: it's MULTIMODAL in -- drop a PHOTO of a disassembled product (or speak), and it "
        "reads every labelled part; voice and images, not just typing.\n"
        "- genesis: from that it creates the WHOLE master-data set in S/4HANA in one deterministic run -- "
        "the finished good, every component, their purchase-info-records and costs, the bill of materials, "
        "the routing, and a production version -- born ready to plan.\n"
        "- planning: then it runs MRP -- demand, planned orders for what we make, purchase requisitions for "
        "what we buy.\n"
        "- board: an EXPERT BOARD -- Engineering, Procurement, Compliance, Finance -- reviews the plan "
        "GROUNDED in the real SAP data and catches what a checklist won't, like two keyboards on a laptop, "
        "or a single vendor supplying ninety percent of the parts.\n"
        "- correction: when the board flags something, it FIXES it -- adjusts the bill of materials -- and "
        "RE-PLANS. The self-correcting loop, not a one-shot.\n"
        "- discipline: every step is traced, every confidence is calibrated, and every write to SAP passes "
        "an APPROVAL GATE you confirm by click or voice -- nothing happens silently.\n"
        "- panels: the three columns -- talk on the left, watch every agent action in the middle, see the "
        "structured results on the right.\n"
        "- cta: invite them to try -- drop a product image, or ask it to plan a material; let's build something real.\n"
        + (f"\nCurrent on-screen focus (weave in briefly if relevant): {focus}\n" if focus else "")
        + 'Return STRICT JSON: {"sections":[{"id":"intro","text":"..."}, ...]} -- every id once, in order.'
    )
    try:
        resp = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model=LLM_MODEL, temperature=0.5, max_tokens=500,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}])
        sections = json.loads(resp.choices[0].message.content).get("sections", [])
    except Exception as e:
        logging.warning(f"[tour] narration failed: {e}")
        return None
    secs = [{"id": s.get("id"), "text": (s.get("text") or "").strip()}
            for s in sections if (s.get("text") or "").strip()]
    return secs or None


@app.post("/api/explain_app")
async def api_explain_app(payload: dict):
    """PRODUCT EXPLAINER -- a short SPOKEN TOUR of what Design2Make is + what each panel does
    (the reference's 'Aria tour'). Serves curated narration INSTANTLY (no LLM wait); the LLM
    refreshes it once in the background for a fresher next run. Audio is fetched per-section by
    the client via /api/tts. Fired only by the 🎙 Tour button."""
    global _tour_cache, _tour_refreshing
    focus = (payload or {}).get("focus") or ""
    voice = os.getenv("TOUR_VOICE", "nova")
    if _tour_cache is None and not _tour_refreshing:
        _tour_refreshing = True

        async def _refresh():
            global _tour_cache, _tour_refreshing
            try:
                secs = await _generate_tour(focus)
                if secs:
                    _tour_cache = secs
            finally:
                _tour_refreshing = False
        asyncio.create_task(_refresh())
    base = _tour_cache or _TOUR_BAKED
    out = [{"id": s["id"], "label": _TOUR_LABELS.get(s["id"], ""), "text": s["text"]} for s in base]
    return {"sections": out, "voice": voice}


_TTS_MEM: dict[str, str] = {}                 # sha1(voice|text) -> b64, this server run
_TTS_DIR = _HERE / "tts_cache"                # persisted clips -> deterministic across runs


def _tts_key(voice: str, text: str) -> str:
    import hashlib
    return hashlib.sha1(f"{voice}|{text}".encode("utf-8")).hexdigest()


@app.post("/api/tts")
async def api_tts(payload: dict):
    """One short TTS clip -- TIME-BOXED, RETRIED on transient errors, and CACHED by (voice,text)
    in memory + on disk. So a tour/demo is DETERMINISTIC and a long narration never 'runs out' of
    voice: once a line is spoken its clip is reused for free on every later run. Returns
    {audio_b64:""} only after retries fail, and the client falls back to captions for that one line."""
    text = ((payload or {}).get("text") or "").strip()
    if not text:
        return {"audio_b64": ""}
    voice = (payload or {}).get("voice") or os.getenv("TOUR_VOICE", "nova")
    text = text[:600]
    key = _tts_key(voice, text)
    if key in _TTS_MEM:                                    # hot cache
        return {"audio_b64": _TTS_MEM[key], "cached": True}
    fp = _TTS_DIR / f"{key}.mp3"
    try:                                                  # warm (disk) cache
        if fp.exists():
            b64 = base64.b64encode(fp.read_bytes()).decode()
            _TTS_MEM[key] = b64
            return {"audio_b64": b64, "cached": True}
    except Exception:
        pass
    timeout = float(os.getenv("TOUR_TTS_TIMEOUT", "9"))
    for attempt in range(3):                              # ride out transient 429 / network blips
        try:
            sp = await asyncio.wait_for(
                asyncio.to_thread(openai_client.audio.speech.create,
                                  model="tts-1", voice=voice, input=text),
                timeout=timeout)
            _TTS_MEM[key] = base64.b64encode(sp.content).decode()
            try:
                _TTS_DIR.mkdir(exist_ok=True); fp.write_bytes(sp.content)
            except Exception:
                pass
            return {"audio_b64": _TTS_MEM[key]}
        except Exception as e:
            logging.warning(f"[tts] attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))
    return {"audio_b64": ""}


@app.get("/api/kg")
async def api_kg():
    """The knowledge-graph ontology (nodes + edges) for the /kg.html visualization. Reads the
    LIVE graph (graph_store/ontology.json, seeded if absent), so add_relation growth shows up."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "kg_graph_view", os.path.join("mcp_server", "graph.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        g = mod._load()
        nodes = [{"id": n, **{k: v for k, v in d.items()}} for n, d in g.nodes(data=True)]
        edges = [{"source": u, "target": v, "relation": d.get("relation", "")}
                 for u, v, d in g.edges(data=True)]
        return {"nodes": nodes, "edges": edges,
                "counts": {"nodes": len(nodes), "edges": len(edges)}}
    except Exception as e:                                # noqa: BLE001
        return {"nodes": [], "edges": [], "error": str(e)}


# The React (Vite) UI is now the PRIMARY app at "/" (built into static_v2/, kg.html bundled in).
# The previous vanilla UI is preserved at "/legacy" as a fallback; /v2 redirects to root for old links.
# All on the single port -- same /ws + /api endpoints. Runs Python-only (no Node needed at runtime).
_V2_DIR = _HERE / "static_v2"
if _V2_DIR.exists():
    @app.get("/v2")
    def _v2_index():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/")
    app.mount("/legacy", StaticFiles(directory=STATIC_DIR, html=True), name="legacy")
    app.mount("/", StaticFiles(directory=_V2_DIR, html=True), name="static")
else:                                                  # fallback if the React build is absent
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    import warnings
    warnings.filterwarnings("ignore", message=r".*\[EXPERIMENTAL\].*")   # quiet ADK feature-flag noise
    try:
        # timeout_graceful_shutdown: after Ctrl+C, wait at most N seconds for open
        # connections (the websocket loop + MCP teardown) before force-closing.
        uvicorn.run(app, host="0.0.0.0", port=8000, timeout_graceful_shutdown=5)
    finally:
        # uvicorn has fully shut down here ("Finished server process"), but the MCP stdio-subprocess
        # reader threads are non-daemon and keep the interpreter alive -- so the shell never returns
        # and you have to kill the window. Force-exit: nothing is left to flush, and the child MCP
        # processes get stdin-EOF when we die and self-terminate. One Ctrl+C now returns the prompt.
        os._exit(0)