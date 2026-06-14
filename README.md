# Design2Make (D2M)

**From a product photo or a sentence to live SAP master data — designed, created, planned, and assured.**

Design2Make is a multimodal agent for **SAP S/4HANA master-data and planning automation**, built on the
**Google Agent Development Kit (ADK)** with tools served over **MCP (Model Context Protocol)**. Show it a
product — an image, a spec sheet, or just a description — and it will design the bill of materials, create
the finished good and its components in SAP, build the **BOM / routing / purchasing info records / costing /
production version**, run **MRP** against real demand, and put the whole assembly through a **deterministic
assurance board** — narrating every step and learning from your corrections.

> **Private repo.** It bundles the SAP **NWRFC SDK** (SAP-licensed, not redistributable). Keep it
> private / SAP-internal.

---

## What it does

| Mode | What happens |
|---|---|
| **Genesis** | *image → manufacturing data.* Identifies the parent finished good + its components, **semantically dedups** against what already exists in SAP (with a confidence score → auto-reuse), then creates the FERT + HALB/HAWA components + BOM + routing + PIR + costing. |
| **Create / change** | A confirm-gated pipeline (Intake → Validate/Enrich → Writer) that creates or changes a material master with **verified codes** (valuation class, tax, views), enriching missing specs from the web. |
| **Make** | Purchasing info records, costing, BOM, routing, production version. |
| **Planning** | Create demand and run **MRP** (planned orders / purchase reqs) against the real SAP system. |
| **Boardroom** | A **deterministic** assurance review — code-level checks (country-of-origin, **plant-extension**, sourcing concentration, master-data completeness, weights, status) over the *real* created objects, judged by a four-lens board (Engineering / Procurement / Compliance / Finance) with severities from `policies.json`. **Facts by code, judgment by the board.** |
| **Learning loop** *(opt-in)* | Captures your corrections as lessons, recalls the relevant ones into later sessions, and **promotes** recurring ones into deterministic checks that can't be forgotten. |

Everything is visible in the UI: a conversation pane, a live **Agent Activity** trace (every tool call +
result), and typed **Structured Data** cards (material, PIR, cost, BOM, routing, MRP, assurance).

---

## Quickstart

Prereqs: `git` and [`uv`](https://docs.astral.sh/uv/) — the launcher installs `uv` for you if it's missing.

```bash
git --version && uv --version

git clone https://github.com/MigrationProofAI/Design2Make_R00763.git
cd Design2Make_R00763

# create your .env from the template, then fill in your keys + SAP connection
copy env.example .env          # Windows   (cp env.example .env on macOS/Linux)

run.bat                        # Windows   (./run.sh on macOS/Linux)
```

`run.bat` installs deps (`uv sync`), starts the **planning MCP on :8001** and the **production-version MCP
on :8002**, then runs the app on **http://localhost:8000**. Open that URL.

> The app (**:8000**) runs fine on its own. The **:8001 / :8002** RFC servers need the **Windows** NWRFC SDK
> `.dll` in `mcp_remote/nwrfcsdk/` (the bundle is the Linux build); until then those two windows will error
> on the SDK — that's expected, and only the NWRFC-based planning / production-version operations are affected.

---

## Configuration (`.env`)

Copy `env.example` → `.env` and fill it in. The essentials:

| Key | What |
|---|---|
| `OPENAI_API_KEY` | LLMs + embeddings + TTS |
| `SAP_HOST` · `SAP_USER` · `SAP_PASS` · `SAP_CLIENT` · `SAP_PLANT` | the S/4HANA OData connection (material, PIR, cost, BOM, routing) |
| `PLANNING_MCP_URL` · `PRODVER_MCP_URL` | the :8001 / :8002 RFC MCP servers (MRP / demand / production version) |
| `OPENAI_MODEL_CONDUCTOR` | the one non-cheap agent — the boardroom conductor (default `gpt-4o`) |
| `DEDUP_THRESHOLD` | cosine ≥ this auto-reuses an existing material (semantic dedup) |
| `D2M_LEARNING` | set `1` to enable the learning loop (capture / recall / promote) |
| `SERPER_API_KEY` | optional web-search enrichment |

Secrets (`.env`) and runtime data (`lessons.jsonl`, `*.db`, `vector_store/`) are **gitignored** — never commit them.

---

## Try it — Genesis

Switch to **Genesis**, attach a product photo (or paste a spec), and ask it to build the assembly, e.g.:

> **"Build this assembly. Use supplier 17300001, dedup against existing materials, and enrich any missing
> specs (net/gross weight, dimensions) from the web."** *(attach the product photo)*

It reads the image, proposes the parent + components, dedups against SAP, and — on your confirm — creates the
material master, components, BOM, routing, PIR and costing, narrating each step in the Activity pane and
dropping a **Genesis** card in the Data pane. Then ask the **Boardroom** to review it.

---

## Architecture

```
React UI  ──ws──▶  FastAPI + ADK Runner  ──▶  intent router ──▶ specialist agent
                                                   │
   search · create_change · make · genesis · planning · boardroom · validate
                                                   │
            local MCP servers (mcp_server/)                 remote MCP (mcp_remote/, over RFC)
  sap · make · genesis · assurance · graph(KG) · vector(dedup) · serper     planning :8001 · prodvers :8002
```

- **Deterministic where it counts** — assurance checks + the rule engine are *code* (`assurance.py`,
  `verdict.py`, `policies.json`, `rules.md`); the model judges and explains, it doesn't invent standards.
- **Safe by default** — every SAP write is **confirm-gated** (preview → approve); `run_mrp` /
  `create_demand` / `create_production_version` are human-gated.
- **Endless sessions** — a token-budget trimmer keeps long conversations under the model window, plus
  source-side caps on large reads so a single turn can't blow the context.

**Key files:** `main.py` (router + WebSocket + UI) · `create_pipeline.py` (create/change) ·
`learning.py` (the loop) · `mcp_server/*` (tools) · `mcp_remote/*` (RFC servers) ·
`policies.json` + `rules.md` (the deterministic standards) · `frontend/` (React source → `static_v2/` build) ·
`ARCHITECTURE.md` (deeper design notes).

---

*Built with Google ADK + MCP, OpenAI (GPT-4o / embeddings), FAISS, and SAP S/4HANA (OData + NWRFC).*
