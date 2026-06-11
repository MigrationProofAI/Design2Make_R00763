"""mcp_server/discipline.py -- the S0-S8 DISCIPLINE spine (the 🛡 band of the diagram).

Cross-cutting governance that WRAPS every stage of the design->make->plan loop. A stage hands
in what it did + the facts it read back; the spine scores it, records the decision, accounts the
cost, decides whether to escalate, writes the verified objects to the instance KG, and remembers
the inputs so a later re-run reopens ONLY what was invalidated.

It is PURE except two persistence sidecars -- the reopen ledger and the KG instance writeback --
which are the deterministic memory the diagram's 🧠 Brain band promises. All read-back I/O stays
in the caller (genesis), so the scoring logic is unit-testable with synthetic facts.

The 9 rungs (verbatim labels from design2make-architecture.svg, 🛡 DISCIPLINE band; S1/S3/S4/S6
filled from the surrounding diagram -- read-back gate, assurance/policy, Brain/KG, evidence-grounded):

  S0 trace + cost      every stage serialized (what / outcome / dur) + its cost (writes, tokens, $)
  S1 grounding         every claim tied to a real read-back fact, not an assertion
  S2 confidence        every stage emits a CALIBRATED 0-1 (+ why) -- derived from reality, not vibes
  S3 verification      writes confirmed by OData read-back before a stage is "done"
  S4 policy            deterministic policy/guardrail verdicts attached (reuses the assurance engine)
  S5 decision record   options considered / choice / rationale, logged per stage
  S6 writeback         verified objects -> KG instance graph (no dup re-create)
  S7 escalation        low confidence / hard fail / policy error -> human gate
  S8 reopen-only       a re-run touches ONLY stages whose inputs changed (input-hash ledger)
"""
import os
import sys
import json
import time
import hashlib

sys.path.insert(0, os.path.dirname(__file__))
# Reuse the SAME deterministic checks the boardroom grounds on -- one policy engine, not two.
from assurance import (_policies, check_master_data, check_country_of_origin,   # noqa: E402
                       check_weights, check_status)
from mcp.server.fastmcp import FastMCP                                          # noqa: E402

mcp = FastMCP("discipline")
_ROOT = os.path.dirname(os.path.dirname(__file__))
_LEDGER = os.path.join(_ROOT, "genesis_ledger.json")     # S8: {material: {stage: inputs_hash}}
_KG_INST = os.path.join(_ROOT, "kg_instances.json")      # S6: {nodes:{id:..}, edges:[..]}
CONF_THRESHOLD = 0.7                                      # below this a stage escalates (S7)


# ---- tiny JSON sidecar I/O (the only I/O in this module) --------------------------
def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _hash(inputs) -> str:
    return hashlib.sha1(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()[:12]


# ---- S2 confidence: CALIBRATED from real outcome + read-back, never a guess --------
def score_confidence(kind: str, verified: bool):
    """kind in {created, exists, skipped, failed}. Confidence is a fact about verification state,
    not a vibe: you only earn 0.95 by being read back from SAP."""
    if kind == "failed":
        return 0.0, "stage failed"
    if kind == "exists":
        return 1.0, "already present in SAP -- idempotent, nothing to do"
    if kind == "skipped":
        return 1.0, "intentionally skipped (not applicable to this item)"
    if kind == "created":
        return (0.95, "created AND confirmed by OData read-back") if verified \
            else (0.6, "created but NOT read-back-confirmed -- unverified")
    return 0.5, "outcome unknown"


# ---- S4 policy: run the deterministic checks over a read-back object ---------------
def policy_findings(obj) -> list:
    if not obj:
        return []
    P = _policies()
    return (check_master_data(obj, P) + check_country_of_origin(obj, P)
            + check_weights(obj, P) + check_status(obj, P))


class Spine:
    """Accumulates S0-S8 records across one run, then finalizes a dossier + persists memory.

    Usage (in the caller, e.g. genesis):
        sp = Spine("genesis", plant=plant)
        sp.material = pmat
        sp.record("parent", "create FERT", "created", obj=readback, inputs=parent_spec,
                  decision={"choice": "create FERT", "because": "no existing material",
                            "alternatives": ["reuse an existing FERT"]}, writes=1)
        sp.add_kg(pmat, "FERT", description=desc)
        dossier = sp.finalize()
    """

    def __init__(self, surface: str, material=None, plant=None):
        self.surface = surface
        self.material = material
        self.plant = plant
        self.stages = []
        self.kg_nodes = {}
        self.kg_edges = []
        self._t0 = time.perf_counter()

    # ---- S0+S1+S2+S3+S4+S5: record one stage ----
    def record(self, stage, action, kind, *, outcome="", obj=None, verified=None,
               grounded_by="", inputs=None, decision=None,
               writes=0, latency_ms=None, tokens=0, usd=0.0):
        if verified is None:
            verified = bool(obj)                              # a read-back object IS the verification
        conf, why = score_confidence(kind, verified)          # S2
        pol = policy_findings(obj)                            # S4
        if any(f["severity"] == "error" for f in pol):        # a policy error caps trust
            conf, why = min(conf, 0.4), why + "; policy ERROR present"
        rec = {
            "stage": stage, "action": action, "kind": kind,
            "outcome": outcome or kind,
            "grounded_by": grounded_by or ("read-back ok" if obj else "no read-back"),   # S1
            "verified": bool(verified),                                                  # S3
            "confidence": round(conf, 2), "why": why,                                    # S2
            "policy": pol,                                                               # S4
            "decision": decision or {},                                                  # S5
            "cost": {"writes": writes, "latency_ms": latency_ms,                         # S0
                     "tokens": tokens, "usd": usd},
            "inputs_hash": _hash(inputs) if inputs is not None else None,                # S8
        }
        self.stages.append(rec)
        return rec

    # ---- S6: buffer instance nodes/edges for writeback ----
    def add_kg(self, node_id, kind, edges=None, **attrs):
        if node_id:
            nid = str(node_id)
            node = self.kg_nodes.get(nid, {"id": nid})       # MERGE -- repeated calls add edges, keep attrs
            node["kind"] = kind
            node.update({k: v for k, v in attrs.items() if v is not None})
            self.kg_nodes[nid] = node
        for rel, tgt, eattrs in (edges or []):
            if node_id and tgt:
                self.kg_edges.append({"source": str(node_id), "relation": rel,
                                      "target": str(tgt), **(eattrs or {})})

    # ---- S7: escalation = anything the system isn't sure about, or a hard policy fail ----
    def escalations(self, threshold=CONF_THRESHOLD) -> list:
        out = []
        for s in self.stages:
            if s["confidence"] < threshold:
                out.append({"stage": s["stage"], "reason": f"confidence {s['confidence']} < {threshold}",
                            "why": s["why"]})
            for f in s["policy"]:
                if f["severity"] == "error":
                    out.append({"stage": s["stage"], "reason": "policy error",
                                "fact": f["fact"], "against": f["against"]})
        return out

    # ---- S8: compare this run's input-hashes to the last run's -> reopened vs unchanged ----
    def reopen_plan(self) -> dict:
        prev = _load(_LEDGER).get(str(self.material), {})
        plan = {}
        for s in self.stages:
            h = s["inputs_hash"]
            if h is None:
                continue
            plan[s["stage"]] = "reopened (inputs changed)" if prev.get(s["stage"]) != h else "unchanged"
        return plan

    def _persist_ledger(self):
        led = _load(_LEDGER)
        led[str(self.material)] = {s["stage"]: s["inputs_hash"]
                                   for s in self.stages if s["inputs_hash"]}
        _save(_LEDGER, led)

    def _persist_kg(self):
        kg = _load(_KG_INST) or {}
        nodes = kg.get("nodes", {})
        edges = kg.get("edges", [])
        nodes.update(self.kg_nodes)
        seen = {(e["source"], e["relation"], e["target"]) for e in edges}
        for e in self.kg_edges:
            key = (e["source"], e["relation"], e["target"])
            if key not in seen:
                edges.append(e)
                seen.add(key)
        _save(_KG_INST, {"nodes": nodes, "edges": edges})

    # ---- finalize: roll up + persist memory (S6/S8) + emit the dossier ----
    def finalize(self, persist=True) -> dict:
        confs = [s["confidence"] for s in self.stages]
        overall = round(min(confs), 2) if confs else 0.0      # weakest link: chain = least-sure stage
        esc = self.escalations()
        reopen = self.reopen_plan()
        if persist and self.material:
            self._persist_ledger()
            self._persist_kg()
        return {
            "surface": self.surface, "material": self.material, "plant": self.plant,
            "overall_confidence": overall,
            "verdict": "ESCALATE -> human" if esc else ("COMMIT" if overall >= CONF_THRESHOLD else "REVIEW"),
            "escalations": esc,                                                          # S7
            "reopen_plan": reopen,                                                       # S8
            "cost": {"writes": sum(s["cost"]["writes"] for s in self.stages),           # S0
                     "wall_ms": round((time.perf_counter() - self._t0) * 1000),
                     "tokens": sum(s["cost"]["tokens"] for s in self.stages),
                     "usd": round(sum(s["cost"]["usd"] for s in self.stages), 4)},
            "decisions": [{"stage": s["stage"], **s["decision"]}                         # S5
                          for s in self.stages if s["decision"]],
            "kg_written": {"nodes": len(self.kg_nodes), "edges": len(self.kg_edges)},    # S6
            "stages": self.stages,
        }


# ---- MCP read tools (so the boardroom / UI can GROUND on the spine's memory) -------
@mcp.tool()
def read_dossier(material: str) -> str:
    """Read the last DISCIPLINE dossier facts for a material: its reopen ledger (S8) +
    its instance-KG footprint (S6). The boardroom can cite this instead of re-deriving."""
    led = _load(_LEDGER).get(str(material), {})
    kg = _load(_KG_INST) or {}
    nodes = [n for n in kg.get("nodes", {}).values() if n["id"] == str(material)]
    edges = [e for e in kg.get("edges", []) if str(material) in (e["source"], e["target"])]
    return json.dumps({"material": material, "ledger_stages": led,
                       "kg_nodes": nodes, "kg_edges": edges}, indent=2)


@mcp.tool()
def read_instance_kg(material: str = "") -> str:
    """Read the instance Knowledge Graph (S6 writeback) -- the REAL created objects + their
    uses / supplied_by / routed_thru edges. material (optional) filters to that node's neighborhood."""
    kg = _load(_KG_INST) or {"nodes": {}, "edges": []}
    if not material:
        return json.dumps({"nodes": list(kg.get("nodes", {}).values()),
                           "edges": kg.get("edges", [])}, indent=2)
    m = str(material)
    edges = [e for e in kg.get("edges", []) if m in (e["source"], e["target"])]
    ids = {m} | {e["source"] for e in edges} | {e["target"] for e in edges}
    nodes = [n for n in kg.get("nodes", {}).values() if n["id"] in ids]
    return json.dumps({"nodes": nodes, "edges": edges}, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
