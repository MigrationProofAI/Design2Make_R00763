# Design2Make · ADK+MCP — Status Board
_Last updated: 2026-06-06_

The agreed plan is a 10-track roadmap toward the design→make→plan→re-genesis demo.
Legend: ✅ done · 🟡 partial · ⛔ blocked · ⏳ pending · 👤 your court

## The 10 tracks
| # | Track | Status | Notes |
|---|---|---|---|
| 1 | Wrap PIR/cost/BOM/routing as confirm-gated MCP tools | ✅ | **all 4 live** (PIR, cost, routing, BOM) |
| 2 | **Planning (MRP) — remote RFC MCP server** | ✅ | you delivered it (`MCPCTypeNWRFC`, SSE :8001); I wired the client side — `planning` intent + Planning agent + remote `run_mrp`/`create_demand`. App imports no SAP SDK. |
| 3 | Thread make-tools behind the router | ✅ | `make` intent + Make agent + sticky routing + work-center resolution |
| 4 | Lock Hormuz round-trip as golden case #6 | ⏳⛔ | premature — needs the flow to run (BOM + MRP) before numbers exist |
| 5 | Shallow boardroom (one parallel-critique pass) | ⏳ | after the flow runs end-to-end |
| 6 | Dumb re-genesis loop (re-run changed material's chain) | ⏳ | needs MRP + BOM |
| 7 | Harden CAD vertical (self-heal, GPT-4o vs frontier) | ⏳ | parallel / off critical path |
| 8 | BTP harness for Joule (streamable-HTTP MCP) | ⏳ | the transport switch; unlocks hot-reload too |
| 9 | Discipline hooks (confidence, decision records, escalation) | 🟡 | **S0–S8 spine built** (`mcp_server/discipline.py`), wired into genesis; boardroom + planning next |
| 10 | Demo arc + MRP-free fallback | ⏳⛔ | the headline; gated on the above |

## Track 1 — what's live (all verified against SAP)
| Capability | Tool | Status |
|---|---|---|
| Purchase Info Record (create/change) | `create_info_record` | ✅ live (e.g. 5300006996) |
| Cost condition (create) | `create_cost_condition` | ✅ live |
| Routing (create, 3-level, released) | `create_routing` | ✅ live (resolves `PACK01`→internal id) |
| BOM (create, two-step header→items) | `create_bom` | ✅ live (e.g. 00000405) |
| Generic child change (any service) | `change_make_object` / `change_material_view` | ✅ live |

## 🚀 Multimodal genesis (NEW) — the Design2Make spine, live
- **`mcp_server/genesis.py` → `run_genesis(spec, confirm)`** — one deterministic call creates a
  whole assembly: parent FERT (born routable) → component materials → PIR+cost (bought) → BOM →
  routing → **PRODUCTION VERSION (MKAL, binds BOM alt 01/usage 1)**. confirm=false previews the plan.
  Design: vision does perception (image→spec), this does execution (spec→SAP), so the long
  write-chain can't be fumbled by a cheap model.
- **Now truly born MRP-ready** — the prodver is the final genesis stage (clears MD408), reached over
  MCP to your remote `:8002` server (app holds no SAP SDK). If `:8002` is down it does NOT crash:
  the stage fails and the S0–S8 dossier ESCALATES "production_version" (material/BOM/routing still made).
- **GenesisAgent (vision) + `genesis` router intent** — drop an image of a disassembled product +
  structured details → previewed plan → confirm → full creation. Smoke-tested 6/6 routing.
- **Proven live:** ASUS laptop scoped run → FERT 11067 + Panel Kit 11068 + Keyboard 11069 (+PIR+cost
  each) + BOM 00000406 + routing 50000212. `genesis_laptop.csv` = the 11-part supplement.

## Cross-cutting capability built (the "lego")
- ✅ **FERT born routable** — `build_material_payload` gives FERT/HALB procurement E + work-scheduling view (KG: `Routing requires WorkScheduling`). Verified FERT 11065/11066 → routing.
- ✅ **Metadata discovery, any service** — `find_field`/`list_fields(service=…)` return fields + KEYS + NAVS.
- ✅ **`explore_entity`** — full nested object from business keys (e.g. PIR by material+supplier), in one call.
- ✅ **`change_material_view` hardened** — service-aware + type-aware keys (`datetime'…'`) + auto-etag (If-Match) + echoes keys in body. Proven: PIR **price** 95→90→92 and **Incoterms** DDP, both 204.
- ✅ **Router** — `make` intent + **sticky routing** (a bare "go ahead"/"proceed" stays on the prior specialist).
- ✅ **Cost control** — per-agent model split: reads/routing on `gpt-4o-mini`, writers on `gpt-4o` (`OPENAI_MODEL` / `OPENAI_MODEL_WRITE`).
- ✅ **Ops** — bounded shutdown (Ctrl+C ≤5s) + WinError 10054 silenced.
- ✅ **Learning loop** — `knowledge.md` captures verified recipes (PIR price location, Incoterms, etc.) and is injected into the write agents.

## 🛡 Discipline spine (S0–S8) — built, wired on genesis
`mcp_server/discipline.py` is the cross-cutting governance band from the architecture diagram.
Pure + unit-tested (`test_discipline.py`, 5/5); genesis does the SAP read-back and hands facts in.
- **S0** trace+cost (writes + wall-ms per stage; tokens/$ ready for the agent surface) · **S1** grounding (each stage carries its read-back fact) · **S2** confidence — *calibrated from reality* (created+read-back=0.95, created-unverified=0.6, exists=1.0, failed=0.0) · **S3** real `get_material` read-back after each material create · **S4** policy (reuses the assurance checks) · **S5** decision records (choice/because/alternatives) · **S6** writeback — **makes the KG read-write** (`kg_instances.json`: uses/supplied_by/routed_thru) · **S7** escalation (conf<0.7 **or** policy error → human) · **S8** reopen-only (input-hash ledger `genesis_ledger.json`).
- Overall confidence = **weakest link**; `run_genesis(confirm=true)` appends a 🛡 DISCIPLINE DOSSIER and the Genesis agent surfaces its verdict + escalations.
- **Next pass:** import the same `Spine` into the boardroom (per-critic confidence + chair decision record) and planning (MRP-exception escalation); optional `/api/kg/instances` to show the live graph.

## Backlog
- ⏳ **create-time semantic dedup (genesis)** — embed the spec, check for a near-duplicate BEFORE creating (test S/4 already has DDR5 module 3258/3260/3261 triplicated). Must be **scoped to a material-number range** (`material_low`..`material_high`) so the similarity search only considers the relevant band. Today: FAISS exists for *search*, not for a create-time gate.
- ⏳ **codebook for PIR/routing** — `list_allowed_values` only covers material fields; extend `codebook_extract.py` to PIR/routing coded values (discovery half done).
- ⏳ **formalize discoveries → KG** (folds into Track 9).
- 🟡 **AM/483 "address texts do not exist"** — intermittent supplier master-data validation on PIR writes; may need an RFC/BAPI path (cf. the `SAP_RFC_*` creds in env.txt) or SAP-side BP address fix.

### Drawbacks captured from session c891df0a (full build→plan→board arc — the richest failure-finder)
**Routing & orchestration**
- ✅ **Over-sticky continuation** (FIXED) — a turn opening "Ok…" glued to the previous intent *before* the classifier ran, so "create demand + run mrp" and "update CoO" stuck on **genesis** and spawned junk FERTs 11230–11248. Fix shipped: `_NEW_TASK_RE` vetoes the sticky shortcut when a different operation is named + softened classifier context.
- ⏳ **Genesis fabricates a spec for non-genesis asks** — given "create the demand", it built a `run_genesis` spec with a component literally named *"Demand"* (created FERT 11231). Add an agent-level guard: genesis ONLY builds master data from an image/parts-list; decline plan/demand/field-update/MRP and name the right specialist (defense-in-depth behind the routing fix).
- ⏳ **Questions about on-screen data mis-route** — "why doesn't the MRP report show purchase-req qty?" routed to `search`. A question about the CURRENT structured output should go to planning/the explainer, not search.

**Idempotency & data hygiene**
- ⏳ **No genesis dedup** (see item above) — concrete proof: **5+ duplicate ASUS laptops** created (11219, 11230, 11232, 11233, 11244, 11246, 11248); the real one is **11219**.
- ⏳ **Make fans out to duplicate matches** — `add_bom_component(RAM 3258)` added it to BOTH 11219 AND the junk 11246. When a request could hit multiple materials, confirm WHICH — never silently act on all.

**Assurance / boardroom (the biggest cluster)**
- ⏳ **CoO false positive for bought parts** — the engine checks the MATERIAL-MASTER `CountryOfOrigin` only; it ignores the PIR/supplier-derived origin (PIRs already show US). Fix: for HAWA/ROH, resolve CoO from the **supplier's country** (read the BP master via the `Supplier` the PIR already returns).
- ⏳ **Board asserts FALSE absences** — it claimed "no planned order" and "CoO missing" when planned order **4000001929** existed and PIRs showed US. Grounding gap: `read_mrp_results` didn't surface the planned order into the dossier; CoO read the wrong field. The board must not assert absence it didn't verify.
- ⏳ **Board drifts off-role on off-script prompts** — asked to "capture limitations", the Conductor/Engineering/Compliance produced session SUMMARIES instead of grounded critique (only Proc/Fin held pipe-format). Harden role instructions to stay in-role regardless of the prompt.
- ⏳ **No learning writeback from corrections** — the user corrected the board twice (planned order exists; PIRs show US) and the *next* board run repeated the same false escalation. Corrections must persist (knowledge.md / KG) so the next run doesn't re-miss.

**Planning output / rendering**
- ⏳ **MRP card lacks the numbers** — the planning structured card omits the **planned-order number + qty** and **purchase-req number + qty + date** (user asked explicitly). `read_mrp_results` + `renderMrpResult` must surface them — the same data the board needs to stop making false claims.

## Parking lot
- **Hot-reload MCP servers/tools** (no full restart) — via MCP `tools/list_changed` + file-watch, and/or long-running streamable-HTTP servers (ties to Track 8).
- **ADK orchestrator migration** — `ParallelAgent`/`SequentialAgent` are deprecated (→ `Workflow`); still work in ADK 2.1.0. When ADK is eventually bumped past removal, migrate ALL dashed orchestrators: boardroom (Sequential+Parallel), genesis create-pipeline (Sequential), rule engine (Sequential+Parallel+Loop). Not urgent; gated on the ADK upgrade.

## Blocked on you (the critical path to the demo)
**Track 2.** The moment you share the MRP `$metadata` (and the BOM Z OData), I wire
`run_mrp` / `read_mrp_result` + unblock `create_bom` the same session — and that unblocks
the plan side, then Tracks 4 / 6 / 10.
