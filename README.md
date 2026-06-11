# Design2Make

Turn a **real product** into **SAP S/4HANA master data + a production plan** — as one connected loop:
**design → make → plan → expert review → fix → re-plan**.

Built on **Google ADK** (agents) + **MCP** (tools). The app is multimodal in (drop a photo of a
disassembled product, or speak/type), creates the whole master-data set (finished good, components,
purchase-info-records, costs, BOM, routing, production version), runs MRP, convenes a grounded expert
board, and self-corrects — every write gated by an explicit, voice-or-click approval.

> 🔒 **Private repo.** The SAP **NWRFC SDK** is bundled (`mcp_remote/nwrfcsdk/`) for the owner's own
> cross-machine use — it is SAP-licensed, so **keep this repository private** (don't redistribute).
> All `.env` files (SAP + OpenAI credentials) are gitignored and never committed; the app itself
> holds no SAP creds — it only knows the two remote MCP URLs.

---

## Architecture

```
┌─ app (this repo root) ── FastAPI + ADK, :8000 ───────────────────────────┐
│   React UI (served from static_v2/)                                       │
│   local stdio MCP servers (mcp_server/): sap · make · vector · graph ·    │
│       genesis · assurance · serper · cocktail   (OData REST, no RFC)       │
└───────────────────────────────────────────────────────────────────────────┘
        │ MCP over SSE (URLs only — no SAP creds in the app)
        ▼
┌─ mcp_remote/ ── separate RFC processes (carry SAP RFC creds + NWRFC SDK) ─┐
│   sap_planning_mcp.py   :8001   MRP + demand        (NWRFC)                │
│   sap_prodvers_mcp.py   :8002   production version  (MKAL/RFC)             │
└───────────────────────────────────────────────────────────────────────────┘
```

## Layout

```
Design2Make_R00763/
├── run.sh / run.bat        ONE-COMMAND launch: RFC :8001 + :8002 + app :8000
├── main.py                 FastAPI app + ADK runner (:8000)
├── mcp_server/             local stdio MCP servers (OData REST)
├── frontend/               React (Vite) source
├── static_v2/              BUILT React bundle (committed → Python-only run)
├── pyproject.toml, uv.lock
├── env.example             copy → .env (app) and mcp_remote/.env (RFC)
└── mcp_remote/             the two RFC MCP servers (:8001 / :8002)
    ├── sap_planning_mcp.py · sap_prodvers_mcp.py · rfc_*.py
    └── nwrfcsdk/           SAP NWRFC SDK — bundled (LINUX build; private repo)
```

---

## Prerequisites
- **Python 3.13**. That's it — `run.bat` / `run.sh` **auto-install [`uv`](https://docs.astral.sh/uv/)**
  (the dependency manager) if it's missing, then `uv sync` the deps.
- An **OpenAI API key** (LLMs + embeddings + TTS).
- For the RFC servers: access to an **SAP system over RFC**. **No `pyrfc`** — they call the NWRFC SDK
  directly via `ctypes`. The SDK is **bundled** (`mcp_remote/nwrfcsdk/`, **Linux** `.so`) — run on
  **Linux / WSL / Docker / a Linux VM**; on **Windows**, swap in the Windows NWRFC SDK (`.dll`s).
- (Optional) **Node 18+** to rebuild the React UI; not needed to run (the bundle is committed).

## Setup & run

```bat
git clone https://github.com/MigrationProofAI/Design2Make_R00763.git
cd Design2Make_R00763

REM fill in credentials — the ONLY manual step (creds are never in the repo):
copy env.example .env                          REM  Linux: cp env.example .env
copy env.example mcp_remote\.env               REM  Linux: cp env.example mcp_remote/.env
notepad .env  &  notepad mcp_remote\.env       REM  OPENAI_API_KEY, SAP OData, SAP_RFC_* values

REM then ONE command — installs uv if missing, uv sync, and starts :8001 + :8002 + :8000:
run.bat                                        REM  Linux: ./run.sh
```

Then open **http://localhost:8000**. Search, genesis, the board, the tour/demo and screen recording
all work **without** the RFC servers; only **MRP / demand / production-version** need them.

> Prefer to run `uv sync` yourself first? Install uv once:
> `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows) or
> `curl -LsSf https://astral.sh/uv/install.sh | sh` (Linux) — then `uv sync` works.

<details><summary>Start things by hand instead of <code>run.sh</code></summary>

```bash
export SAPNWRFC_HOME="$PWD/mcp_remote/nwrfcsdk"
export LD_LIBRARY_PATH="$SAPNWRFC_HOME/lib:$LD_LIBRARY_PATH"
( cd mcp_remote && MCP_PORT=8001 python sap_planning_mcp.py ) &
( cd mcp_remote && MCP_PORT=8002 python sap_prodvers_mcp.py ) &
uv run main.py
```
</details>

### Rebuild the UI (optional)
```bash
npm --prefix frontend install
npm --prefix frontend run build     # → static_v2/
```

---

## Notes
- **Destructive actions** (genesis create, run MRP, create demand, production version) are
  **confirm-gated** — nothing writes to SAP without explicit approval (click or voice).
- The Conductor (boardroom chair) runs on `gpt-4o`; every other agent runs on a cheap model.
- First semantic search builds the embedding index (`index_materials`); created materials are
  written back so duplicates are caught on the next run.
