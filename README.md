# Design2Make

Turn a **real product** into **SAP S/4HANA master data + a production plan** — as one connected loop:
**design → make → plan → expert review → fix → re-plan**.

Built on **Google ADK** (agents) + **MCP** (tools). The app is multimodal in (drop a photo of a
disassembled product, or speak/type), creates the whole master-data set (finished good, components,
purchase-info-records, costs, BOM, routing, production version), runs MRP, convenes a grounded expert
board, and self-corrects — every write gated by an explicit, voice-or-click approval.

> ⚠️ **Security:** never commit `.env` files or the SAP **NWRFC SDK** (licensed, not redistributable).
> Both are gitignored. The app holds **no SAP credentials** — it only knows the two remote MCP URLs.

---

## Architecture

```
┌─ app/ (this repo root) ── FastAPI + ADK, :8000 ──────────────────────────┐
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
├── main.py                 FastAPI app + ADK runner (:8000)
├── mcp_server/             local stdio MCP servers (OData REST)
├── frontend/               React (Vite) source
├── static_v2/              BUILT React bundle (committed → Python-only run)
├── pyproject.toml, uv.lock
├── env.example             copy → .env (app) and mcp_remote/.env (RFC)
└── mcp_remote/             the two RFC MCP servers (:8001 / :8002)
    ├── sap_planning_mcp.py · sap_prodvers_mcp.py · rfc_*.py
    └── nwrfcsdk/           (NOT in repo — download the SAP NWRFC SDK here)
```

---

## Prerequisites
- **Python 3.13** and [`uv`](https://docs.astral.sh/uv/) (`pip install uv`).
- An **OpenAI API key** (LLMs + embeddings + TTS).
- For the RFC servers only: access to an **SAP system over RFC**, the **SAP NWRFC SDK**, and `pyrfc`.
- (Optional) **Node 18+** if you want to rebuild the React UI; not needed to run (the bundle is committed).

## Setup

```bash
# 1. App
cp env.example .env            # fill OPENAI_API_KEY + the local SAP OData (sap.py/make.py) values
uv sync                        # install Python deps

# 2. Remote RFC servers (only if you run planning / production-version)
cp env.example mcp_remote/.env # fill the SAP_RFC_* values (system, client, user, password)
# Download the SAP NWRFC SDK (SAP Support Portal, licensed) and unzip it to:
#   mcp_remote/nwrfcsdk/
# then: pip install pyrfc   (builds against the SDK)
```

## Run

```bash
# (a) start the two RFC servers — each in its own shell (needs the SDK + RFC creds)
cd mcp_remote && MCP_PORT=8001 python sap_planning_mcp.py
cd mcp_remote && MCP_PORT=8002 python sap_prodvers_mcp.py

# (b) start the app
uv run main.py                 # → http://localhost:8000
```

Open `http://localhost:8000`. The app connects to the RFC servers via `PLANNING_MCP_URL` /
`PRODVER_MCP_URL` (defaults `:8001` / `:8002`). Search, genesis, the board, the tour/demo and screen
recording all work without the RFC servers; only **MRP / demand / production-version** need them.

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
