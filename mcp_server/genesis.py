"""mcp_server/genesis.py -- the multimodal GENESIS orchestrator.

Given a structured genesis spec (parent assembly + components + routing) -- produced from
an IMAGE + a CSV by the vision agent -- create the whole master-data set in SAP, in order:

    parent FERT (born routable)
      -> component materials (bought=HAWA/ROH, made=HALB/FERT)
      -> PIR + cost  (for each BOUGHT component)
      -> BOM   (parent + components)
      -> routing (parent)

DESIGN: the LLM does PERCEPTION (image -> spec); THIS does EXECUTION (spec -> SAP),
deterministically, so the long multi-write chain can't be fumbled by a cheap model.
Confirm-gated: confirm=false PREVIEWS the plan; confirm=true creates.

Standalone:  python ./mcp_server/genesis.py    (ADK launches it over stdio)
"""
import os
import sys
import csv
import json
import re
import asyncio
import threading

sys.path.insert(0, os.path.dirname(__file__))
from sap import build_material_payload, create_material, get_material   # noqa: E402
from make import (create_info_record, create_cost_condition,           # noqa: E402
                  create_bom, create_routing)
from discipline import Spine                                           # noqa: E402  (S0-S8 spine)
from mcp.server.fastmcp import FastMCP                                  # noqa: E402
from mcp import ClientSession                                          # noqa: E402  (MCP CLIENT ->
from mcp.client.sse import sse_client                                  # noqa: E402   remote :8002 prodver)

mcp = FastMCP("genesis")
_DEF_PLANT = os.getenv("SAP_PLANT", "1710")
# The production version (MKAL) lives on YOUR remote RFC MCP server -- the ADK app holds no SAP SDK,
# so genesis reaches it the same way the agents do: over MCP. URL matches main.py's PRODVER_MCP_URL.
PRODVER_MCP_URL = os.getenv("PRODVER_MCP_URL", "http://127.0.0.1:8002/sse")
_DEF_ROUTING = [{"operation": "10", "text": "Final Assembly", "work_center": "ASSEMBLY"},
                {"operation": "20", "text": "Packaging", "work_center": "PACK01"}]


# ---- helpers ----------------------------------------------------------------
def _exists(matnr) -> bool:
    return bool(matnr) and '"Product"' in get_material(str(matnr))


def _readback(matnr):
    """S3 verification: re-read a just-touched material from SAP. Returns its 'd' dict (the
    grounding fact for S1/S2/S4) or None if the read-back fails -- which is itself a finding."""
    try:
        d = json.loads(get_material(str(matnr)))
        return d.get("d") if isinstance(d, dict) else None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _new_matnr(res: str) -> str | None:
    m = re.search(r'"Product":"(\w+)"', res)
    return m.group(1) if m else None


def _dossier(d: dict) -> str:
    """Render the 🛡 DISCIPLINE dossier (S0-S8) as an appended section: a human headline +
    the full structured record, so the agent AND the Activity panel can both read it."""
    head = (f"\n\n===== 🛡 DISCIPLINE DOSSIER (S0-S8) =====\n"
            f"verdict: {d['verdict']}   overall confidence: {d['overall_confidence']}   "
            f"cost: {d['cost']['writes']} writes / {d['cost']['wall_ms']} ms   "
            f"KG: +{d['kg_written']['nodes']} nodes / +{d['kg_written']['edges']} edges")
    for e in d["escalations"]:
        detail = e.get("why") or e.get("fact") or ""
        head += f"\n  ⚠ S7 ESCALATE {e['stage']}: {e['reason']}" + (f" ({detail})" if detail else "")
    # Ship the agent the ROLLUP only -- the full per-stage `stages[]` (verbose policy/grounding/inputs)
    # is ~27k tokens and the agent only needs verdict/confidence/escalations/decisions/cost. The full
    # detail is durably in the sidecars (kg_instances.json + genesis_ledger.json), not lost.
    slim = {k: v for k, v in d.items() if k != "stages"}
    return head + "\n```json\n" + json.dumps(slim, indent=2) + "\n```"


def _discipline_summary(d: dict) -> dict:
    """The S0-S8 roll-up compacted for the structured card (verdict / confidence / escalations)."""
    return {"verdict": d.get("verdict"), "confidence": d.get("overall_confidence"),
            "writes": (d.get("cost") or {}).get("writes"),
            "escalations": [{"stage": e.get("stage"), "reason": e.get("reason"),
                             "detail": e.get("why") or e.get("fact") or ""}
                            for e in d.get("escalations", [])]}


def _run_async(make_coro):
    """Run a coroutine to completion from inside a SYNC FastMCP tool. The tool body executes IN the
    server's running event loop, so asyncio.run() here would raise -- run it in a private thread with
    its own loop instead. Re-raises any failure to the caller."""
    box = {}

    def worker():
        try:
            box["v"] = asyncio.run(make_coro())
        except BaseException as e:                  # noqa: BLE001 -- surface to the caller thread
            box["e"] = e

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


# Lot size on the MKAL: BSTMI = minimum, BSTMA = maximum (the :8002 tool takes them via extra_fields).
# Hard-coded 1..10000 for the demo; parameterize via the spec later.
_LOT_SIZE = {"BSTMI": "1", "BSTMA": "10000"}


def _create_production_version(material, plant, desc):
    """Create the PRODUCTION VERSION (MKAL) on the remote :8002 RFC MCP server -- the final
    master-data object that binds the BOM (alt 01 / usage 1) + sets lot size 1..10000 so MRP can
    plan it (clears MD408). Returns (ok: bool, message: str). ok=False (with a clear reason) if
    :8002 is unreachable or the FM reports failure -- never raises."""
    args = {"material": str(material), "plant": str(plant), "version": "0001",
            "text": f"{(desc or str(material))[:28]} version 1",
            "bom_usage": "1", "bom_alt": "01", "testrun": False, "extra_fields": _LOT_SIZE}

    async def _go():
        async with sse_client(PRODVER_MCP_URL, timeout=5, sse_read_timeout=60) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                res = await session.call_tool("create_production_version", args)
                txt = " ".join(c.text for c in res.content if getattr(c, "type", "") == "text").strip()
                return res.isError, txt

    try:
        is_err, txt = _run_async(_go)
    except Exception as e:                          # :8002 down / refused / timeout / protocol error
        leaf = e                                    # anyio nests the real cause in ExceptionGroup(s)
        while getattr(leaf, "exceptions", None):
            leaf = leaf.exceptions[0]
        return False, f":8002 prodver unreachable/failed -- {type(leaf).__name__}: {leaf}"

    # The FM returns {"success": bool, "message", "messages":[{type,message}]} as JSON text -- read it.
    try:
        d = json.loads(txt)
        if isinstance(d, dict) and "success" in d:
            detail = d.get("message") or "; ".join(
                m.get("message", "") for m in d.get("messages", []) if m.get("message")) or "(no message)"
            return bool(d["success"]), detail
    except (ValueError, TypeError):
        pass
    ok = not is_err and not any(x in txt.lower()
                                for x in ("error", "fail", "exception", "not found", "invalid"))
    return ok, (txt or "(no text)")


def _create_material(spec: dict, plant: str) -> tuple[str | None, str]:
    """Create one material (parent or component) via the verified payload builder.
    FERT parents are born routable (procurement E + work-scheduling) and get a sales view."""
    ptype = spec.get("type", "HAWA")
    payload = json.loads(build_material_payload(
        description=(spec.get("description") or spec.get("name") or "Material")[:40],
        product_type=ptype, plant=plant,
        sales_org="1710" if ptype == "FERT" else None,
    ))["fields"]
    res = create_material(fields=payload, confirm=True)
    return _new_matnr(res), res


# ---- semantic DEDUP at the very start (scoped) + write-back -----------------
# Reuse the vector engine's pure functions IN-PROCESS so genesis can ask "does this already exist?"
# BEFORE it creates, and write each new material back into the index so the NEXT run sees it.
try:
    from vector import _search as _vec_search, _add_material as _vec_add, _DUP_THRESHOLD as _VEC_THR
    _DEDUP_OK = True
except Exception as _e:                                  # faiss/openai missing -> degrade gracefully
    _DEDUP_OK = False
    _VEC_THR = 0.92
    def _vec_search(*a, **k): return []
    def _vec_add(*a, **k): return (False, "vector engine unavailable")

_DEDUP_FROM = os.getenv("DEDUP_FROM", "")                 # SAP-style range to scope dedup within
_DEDUP_TO = os.getenv("DEDUP_TO", "")
_DEDUP_THR = float(os.getenv("DEDUP_THRESHOLD", str(_VEC_THR)))   # cosine >= this => auto-reuse
_DATA = "@@DATA@@"                                        # sentinel: <readable text>@@DATA@@<json>


def _dedup(description: str, lo: str = "", hi: str = "") -> dict:
    """Closest EXISTING material to `description` (scoped to [lo, hi]) with a confidence score and a
    verdict. is_duplicate => we should reuse it instead of minting a near-identical twin."""
    desc = (description or "").strip()
    if not _DEDUP_OK or not desc:
        return {"checked": False, "is_duplicate": False, "threshold": _DEDUP_THR, "match": None, "candidates": []}
    cands = _vec_search(desc, 5, lo or _DEDUP_FROM, hi or _DEDUP_TO)
    best = cands[0] if cands else None
    is_dup = bool(best and best["score"] >= _DEDUP_THR)
    return {"checked": True, "is_duplicate": is_dup, "threshold": round(_DEDUP_THR, 3),
            "match": best if is_dup else None, "candidates": cands}


def _emit(text: str, data: dict) -> str:
    """Return the human report with a machine-readable structured payload appended after a sentinel.
    main.py strips everything from the sentinel on for chat/activity, and parses the JSON for the
    Data panel -- so the same call drives a readable dossier AND a typed card."""
    try:
        return text + "\n" + _DATA + json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return text


def _plain(s: str) -> str:
    """Strip a make-tool's @@DATA@@ card block: genesis builds its own roll-up payload, so it only
    wants the readable text for its report + discipline records."""
    return s.split(_DATA, 1)[0].rstrip() if isinstance(s, str) and _DATA in s else s


def genesis_from_csv(path: str) -> dict:
    """Load a genesis spec from a CSV (the 'something else' that complements the image).
    Columns: role(parent|bought|made), name, material, description, type, vendor, price,
    quantity, unit. One parent row + N component rows."""
    parent, comps = {}, []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            role = (r.get("role") or "").strip().lower()
            row = {"name": r.get("name", "").strip(),
                   "material": (r.get("material") or "").strip() or None,
                   "description": r.get("description", "").strip(),
                   "type": (r.get("type") or "HAWA").strip(),
                   "vendor": (r.get("vendor") or "").strip() or None,
                   "price": float(r["price"]) if r.get("price") else None,
                   "quantity": float(r["quantity"]) if r.get("quantity") else 1,
                   "unit": (r.get("unit") or "EA").strip()}
            if role == "parent":
                parent = {"description": row["description"] or row["name"],
                          "type": row["type"], "plant": _DEF_PLANT,
                          "material": row["material"]}
            else:
                row["role"] = "made" if role == "made" else "bought"
                comps.append(row)
    return {"parent": parent, "components": comps}


@mcp.tool()
def run_genesis(spec: dict, confirm: bool = False) -> str:
    """Create a whole assembly's master data from a genesis spec (the heart of Design2Make):
    parent FERT -> component materials -> PIR+cost (bought) -> BOM -> routing.

    SAFETY GATE: confirm=false (default) returns the PLAN (existence checks, what will be
    created) and writes NOTHING. confirm=true performs all creates in order and reports.

    spec = {
      "parent": {"description": str, "type": "FERT", "plant"?: "1710", "material"?: <existing>},
      "components": [{"name", "description", "type" (HAWA/ROH=bought, HALB/FERT=made),
                      "role": "bought"|"made", "vendor"?, "price"?, "quantity"?, "unit"?,
                      "material"?: <existing, or null to create new>}],
      "routing"?: [{"operation","text","work_center"}]   # defaults to Assembly + Packaging
    }
    """
    parent = spec.get("parent") or {}
    plant = parent.get("plant") or _DEF_PLANT
    comps = spec.get("components", [])
    routing = spec.get("routing") or _DEF_ROUTING
    lo, hi = spec.get("dedup_from", _DEDUP_FROM), spec.get("dedup_to", _DEDUP_TO)   # scope the dedup
    # A spec with NO parent FERT is a COMPONENTS-ONLY run (e.g. "create DDR RAM + its PIR + cost"):
    # create the component(s) + PIR/cost, and SKIP BOM/routing/production-version. A bought part has
    # nothing to manufacture, so a production version is meaningless (and fails -- "material not valid").
    has_parent = bool(parent.get("material") or parent.get("description"))

    if not confirm:
        gres = {"kind": "genesis", "mode": "preview", "plant": plant, "parent": None,
                "components": [], "bom": None, "routing": None, "production_version": None, "discipline": None}
        out = ["GENESIS PREVIEW -- nothing written. Confirm to create the full set."]
        if has_parent:
            pex = _exists(parent.get("material"))
            pdd = None if pex else _dedup(parent.get("description"), lo, hi)
            paction = "exists" if pex else ("dedup-reuse" if (pdd and pdd["is_duplicate"]) else "create")
            pmatp = parent.get("material") if pex else (pdd["match"]["Product"] if paction == "dedup-reuse" else None)
            tag = (f"  [exists {pmatp}]" if pex else
                   (f"  [REUSE {pmatp} · ~{pdd['match']['score']} dup]" if paction == "dedup-reuse" else "  [CREATE]"))
            out.append(f"\nPARENT (FERT, born routable @ {plant}): {parent.get('description')}{tag}")
            gres["parent"] = {"description": parent.get("description"), "material": pmatp,
                              "type": parent.get("type", "FERT"), "action": paction, "dedup": pdd}
        else:
            out.append("\n(COMPONENTS-ONLY -- no parent assembly, so NO BOM / routing / production version)")
        out.append(f"\nCOMPONENTS ({len(comps)}):")
        for c in comps:
            cex = _exists(c.get("material"))
            cdd = None if cex else _dedup(c.get("description") or c.get("name"), lo, hi)
            caction = "exists" if cex else ("dedup-reuse" if (cdd and cdd["is_duplicate"]) else "create")
            cmatp = c.get("material") if cex else (cdd["match"]["Product"] if caction == "dedup-reuse" else None)
            tag = (f"exists {cmatp}" if cex else
                   (f"REUSE {cmatp} ~{cdd['match']['score']}" if caction == "dedup-reuse" else "CREATE"))
            extra = (f"  -> PIR + cost @ vendor {c.get('vendor')} / {c.get('price')}"
                     if c.get("role") == "bought" and c.get("vendor") else "")
            dupnote = (f"  (closest {cdd['candidates'][0]['Product']} ~{cdd['candidates'][0]['score']})"
                       if caction == "create" and cdd and cdd["candidates"] else "")
            out.append(f"  - {c.get('name')}: [{tag}] {c.get('type')}/{c.get('role')} x{c.get('quantity')}{extra}{dupnote}")
            gres["components"].append(
                {"name": c.get("name"), "description": c.get("description") or c.get("name"),
                 "type": c.get("type"), "role": c.get("role"), "quantity": c.get("quantity", 1),
                 "material": cmatp, "action": caction, "vendor": c.get("vendor"), "price": c.get("price"),
                 "dedup": cdd})
        if has_parent:
            out.append(f"\nthen BOM (parent + {len(comps)} components)"
                       f"  and ROUTING ({' -> '.join(o['work_center'] for o in routing)})")
            out.append("then PRODUCTION VERSION 0001 (bind BOM alt 01 / usage 1) "
                       "-> the assembly becomes MRP-ready (clears MD408).")
        return _emit("\n".join(out), gres)

    report = []
    sp = Spine("genesis", plant=plant)               # 🛡 S0-S8 spine wraps every stage below
    gres = {"kind": "genesis", "mode": "complete", "plant": plant, "parent": None,
            "components": [], "bom": None, "routing": None, "production_version": None, "discipline": None}

    # 1) PARENT (full-assembly runs only; a components-only run has no FERT) ---
    pmat = None
    if has_parent:
        pdesc = parent.get("description")
        pmat = parent.get("material")
        if _exists(pmat):
            report.append(f"parent exists: {pmat}")
            sp.material = pmat
            sp.record("parent", "reuse FERT", "exists", outcome=f"exists {pmat}", obj=_readback(pmat),
                      inputs={"desc": pdesc, "type": parent.get("type")},
                      decision={"choice": f"reuse existing {pmat}", "because": "material already in SAP",
                                "alternatives": ["create a new FERT"]})
            paction, pdd = "exists", None
        else:
            pdd = _dedup(pdesc, lo, hi)              # <-- dedup BEFORE create
            if pdd["is_duplicate"]:
                pmat = pdd["match"]["Product"]
                report.append(f"parent: reused {pmat} (semantic dup ~{pdd['match']['score']} of '{pdesc}')")
                sp.material = pmat
                sp.record("parent", "dedup-reuse FERT", "exists",
                          outcome=f"reused {pmat} (score {pdd['match']['score']})", obj=_readback(pmat),
                          inputs={"desc": pdesc}, decision={"choice": f"reuse {pmat} (semantic match)",
                          "because": f"score {pdd['match']['score']} >= {_DEDUP_THR} dup threshold",
                          "alternatives": ["create a new FERT"]})
                paction = "dedup-reuse"
            else:
                pmat, res = _create_material({**parent, "type": parent.get("type", "FERT")}, plant)
                if not pmat:
                    sp.record("parent", "create FERT", "failed", outcome=res[:160], inputs={"desc": pdesc})
                    gres["parent"] = {"description": pdesc, "material": None, "action": "failed", "dedup": pdd}
                    return _emit(f"GENESIS ABORTED -- parent create failed: {res[:300]}" + _dossier(sp.finalize()), gres)
                report.append(f"parent FERT created: {pmat}  ({pdesc})")
                sp.material = pmat
                sp.record("parent", "create FERT", "created", outcome=f"created {pmat}", obj=_readback(pmat),
                          writes=1, inputs={"desc": pdesc, "type": parent.get("type", "FERT")},
                          decision={"choice": "create a new FERT (born routable)", "because": "no existing material given",
                                    "alternatives": ["reuse an existing FERT"]})
                _vec_add(pmat, (pdesc or "")[:60])   # <-- write-back so a repeat run sees it
                paction = "created"
        gres["parent"] = {"description": pdesc, "material": pmat, "type": parent.get("type", "FERT"),
                          "action": paction, "dedup": pdd}
        sp.add_kg(pmat, "FERT", description=(pdesc or "")[:40], plant=plant)

    # 2) COMPONENTS (+ PIR/cost for bought) ----------------------------------
    comp_rows = []
    for c in comps:
        mat = c.get("material")
        cdesc = c.get("description") or c.get("name") or ""
        sname = f"component:{c.get('name')}"
        cin = {"name": c.get("name"), "type": c.get("type"), "role": c.get("role"),
               "vendor": c.get("vendor"), "price": c.get("price"), "qty": c.get("quantity", 1)}
        crow = {"name": c.get("name"), "description": cdesc, "type": c.get("type"), "role": c.get("role"),
                "quantity": c.get("quantity", 1), "vendor": c.get("vendor"), "price": c.get("price"),
                "material": None, "action": None, "dedup": None, "pir": None, "cost": None}
        if _exists(mat):
            report.append(f"  - {c['name']}: exists {mat}")
            sp.record(sname, "reuse component", "exists", outcome=f"exists {mat}", obj=_readback(mat),
                      inputs=cin, decision={"choice": f"reuse {mat}", "because": "component already in SAP",
                                            "alternatives": ["create a new component"]})
            crow["action"] = "exists"
        else:
            cdd = _dedup(cdesc, lo, hi)               # <-- dedup BEFORE create
            crow["dedup"] = cdd
            if cdd["is_duplicate"]:
                mat = cdd["match"]["Product"]
                report.append(f"  - {c['name']}: reused {mat} (semantic dup ~{cdd['match']['score']})")
                sp.record(sname, "dedup-reuse component", "exists",
                          outcome=f"reused {mat} (score {cdd['match']['score']})", obj=_readback(mat), inputs=cin,
                          decision={"choice": f"reuse {mat} (semantic match)",
                                    "because": f"score {cdd['match']['score']} >= {_DEDUP_THR} dup threshold",
                                    "alternatives": ["create a new component"]})
                crow["action"] = "dedup-reuse"
            else:
                mat, res = _create_material(c, plant)
                if not mat:
                    report.append(f"  - {c['name']}: CREATE FAILED {res[:120]}")
                    sp.record(sname, "create component", "failed", outcome=res[:120], inputs=cin)
                    crow["action"] = "failed"
                    gres["components"].append(crow)
                    continue
                report.append(f"  - {c['name']}: created {mat} [{c.get('type')}]")
                sp.record(sname, "create component", "created", outcome=f"created {mat}", obj=_readback(mat),
                          writes=1, inputs=cin, decision={"choice": f"create {c.get('type')} component",
                          "because": "no existing material given", "alternatives": ["reuse an existing component"]})
                _vec_add(mat, cdesc[:60])             # <-- write-back
                crow["action"] = "created"
        crow["material"] = mat
        comp_rows.append({"component": mat, "quantity": c.get("quantity", 1)})
        sp.material = sp.material or mat                  # components-only run: anchor on the first part
        sp.add_kg(mat, c.get("type", "HAWA"), description=cdesc[:40])
        sp.add_kg(pmat, "FERT", edges=[("uses", mat, {"quantity": c.get("quantity", 1)})])  # no-op if no parent
        if c.get("role") == "bought" and c.get("vendor"):
            pir = _plain(create_info_record(mat, c["vendor"], confirm=True))
            ok = "Created" in pir
            report.append(f"      PIR: {'ok' if ok else pir[:90]}")
            crow["pir"] = {"status": "ok" if ok else "failed", "vendor": c.get("vendor"), "message": pir[:120]}
            sp.record(f"source:{c.get('name')}", "create PIR", "created" if ok else "failed",
                      outcome=pir[:90], verified=ok, grounded_by=pir[:90], writes=1 if ok else 0,
                      inputs={"comp": mat, "vendor": c.get("vendor")},
                      decision={"choice": f"source from {c.get('vendor')}", "because": "bought part needs a source",
                                "alternatives": ["make in-house", "an alternate vendor"]})
            sp.add_kg(mat, c.get("type", "HAWA"), edges=[("supplied_by", c["vendor"], {})])
            sp.add_kg(c["vendor"], "vendor")
            if c.get("price"):
                cost = _plain(create_cost_condition(mat, c["vendor"], float(c["price"]), confirm=True))
                cok = "Created" in cost
                report.append(f"      cost: {'ok' if cok else cost[:90]}")
                crow["cost"] = {"status": "ok" if cok else "failed", "price": c.get("price"),
                                "vendor": c.get("vendor"), "message": cost[:120]}
                sp.record(f"cost:{c.get('name')}", "create cost", "created" if cok else "failed",
                          outcome=cost[:90], verified=cok, grounded_by=cost[:90], writes=1 if cok else 0,
                          inputs={"comp": mat, "vendor": c.get("vendor"), "price": c.get("price")})
        gres["components"].append(crow)

    # 3-5) BOM + ROUTING + PRODUCTION VERSION -- ONLY for a manufactured parent. A components-only
    #      run stops here: the parts + their PIR/cost exist; there is nothing to assemble or plan-bind.
    if not has_parent:
        report.append("(components-only -- no BOM / routing / production version)")
        gres["discipline"] = _discipline_summary(sp.finalize())
        return _emit("GENESIS COMPLETE (components only):\n" + "\n".join(report) + _dossier(sp.finalize()), gres)

    # 3) BOM (parent + components) -------------------------------------------
    if comp_rows:
        bom = _plain(create_bom(pmat, plant, comp_rows, confirm=True))
        bok = "created" in bom.lower() or "ok" in bom.lower()
        report.append(f"BOM: {bom[:140]}")
        gres["bom"] = {"status": "ok" if bok else "failed", "components": len(comp_rows), "message": bom[:160]}
        sp.record("bom", "create BOM", "created" if bok else "failed", outcome=bom[:140],
                  verified=bok, grounded_by=bom[:140], writes=1 if bok else 0,
                  inputs={"parent": pmat, "components": comp_rows},
                  decision={"choice": f"BOM of {len(comp_rows)} components",
                            "because": "the assembly needs a structure", "alternatives": ["phantom / no BOM"]})

    # 4) ROUTING (parent) ----------------------------------------------------
    rt = _plain(create_routing(pmat, plant, routing,
                description=f"{(parent.get('description') or '')[:28]} routing", confirm=True))
    rok = "created" in rt.lower() or "ok" in rt.lower()
    report.append(f"ROUTING: {rt[:140]}")
    gres["routing"] = {"status": "ok" if rok else "failed",
                       "operations": [{"operation": o.get("operation"), "text": o.get("text"),
                                       "work_center": o.get("work_center")} for o in routing], "message": rt[:160]}
    sp.record("routing", "create routing", "created" if rok else "failed", outcome=rt[:140],
              verified=rok, grounded_by=rt[:140], writes=1 if rok else 0,
              inputs={"parent": pmat, "ops": routing},
              decision={"choice": " -> ".join(o["work_center"] for o in routing),
                        "because": "a made part needs operations", "alternatives": ["external processing"]})
    for o in routing:
        sp.add_kg(o["work_center"], "work_center")
        sp.add_kg(pmat, "FERT", edges=[("routed_thru", o["work_center"], {"op": o.get("operation")})])

    # 5) PRODUCTION VERSION (MKAL) -- the FINAL master-data object. Part of the OBJECT DESIGN, not
    #    planning: without it MRP can't select the BOM (MD408), so the assembly is not truly
    #    "born MRP-ready" until this binds BOM alt 01 / usage 1. Lives on your remote :8002 server.
    pv_ok, pv_msg = _create_production_version(pmat, plant, parent.get("description"))
    report.append(f"PRODUCTION VERSION (lot 1-10000): {'ok -- ' if pv_ok else 'NOT bound -- '}{pv_msg[:140]}")
    gres["production_version"] = {"status": "ok" if pv_ok else "failed", "version": "0001",
                                  "bom_alt": "01", "bom_usage": "1", "lot": "1-10000", "message": pv_msg[:160]}
    sp.record("production_version", "create production version (MKAL @ :8002, lot 1-10000)",
              "created" if pv_ok else "failed", outcome=pv_msg[:140],
              verified=pv_ok, grounded_by=pv_msg[:140], writes=1 if pv_ok else 0,
              inputs={"parent": pmat, "version": "0001", "bom_alt": "01", "bom_usage": "1",
                      "lot_min": "1", "lot_max": "10000"},
              decision={"choice": "bind BOM alt 01 / usage 1 as version 0001, lot size 1-10000",
                        "because": "without the MKAL the BOM can't be selected by MRP (MD408)",
                        "alternatives": ["leave unbound -- NOT MRP-ready"]})
    sp.add_kg(f"PV-{pmat}-0001", "production_version", bound_bom="alt 01 / usage 1")
    sp.add_kg(pmat, "FERT", edges=[("has_production_version", f"PV-{pmat}-0001", {})])

    final = sp.finalize()
    gres["discipline"] = _discipline_summary(final)
    return _emit(f"GENESIS COMPLETE for {pmat}:\n" + "\n".join(report)
                 + _dossier(final), gres)            # S6/S8 persist + roll-up, then append the dossier


if __name__ == "__main__":
    mcp.run(transport="stdio")
