"""mcp_server/assurance.py -- the DETERMINISTIC assurance engine behind the boardroom.

Facts by code, judgment by the board. This runs the org POLICIES (policies.json) and the
data RULES over the REAL created objects (material header + components + their PIRs/vendors)
and emits structured FINDINGS. It never reasons or invents a standard -- a finding is a fact:
  {check, object, fact, against (policy/rule id), severity, verdict}.
The board (Conductor -> critics -> chair) reads the findings and supplies the JUDGMENT
(accept with rationale / find an alternate / escalate). Severity comes from policy, not a model.

The CoO miss that motivated this: "check country of origin" is an instruction a model can skip;
`CountryOfOrigin in restricted_regions` is code that cannot. So the checks are CODE.

Reads are reused from the proven sap.py tools (get_material, explore_entity). No writes.
"""
import os
import sys
import json
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
from sap import get_material, explore_entity                 # noqa: E402  (proven read tools)
from mcp.server.fastmcp import FastMCP                        # noqa: E402

mcp = FastMCP("assurance")
_POLICY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "policies.json")
FACT_FIELDS = ["Product", "ProductType", "BaseUnit", "ProductGroup", "CountryOfOrigin",
               "GrossWeight", "NetWeight", "WeightUnit", "CrossPlantStatus"]
_BOUGHT = {"HAWA", "ROH", "VERP"}


def _policies() -> dict:
    with open(_POLICY_PATH, encoding="utf-8") as f:
        return json.load(f)


def _num(v):
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def _F(check, obj, fact, against, severity, verdict):
    return {"check": check, "object": str(obj), "fact": fact,
            "against": against, "severity": severity, "verdict": verdict}


# ---- PURE deterministic checks (unit-testable; no I/O) ----------------------------
def check_master_data(obj, P):
    md = P["master_data"]
    req = list(md["required_all"]) + (list(md["required_fert"]) if obj.get("ProductType") == "FERT" else [])
    return [_F("master_data", obj.get("Product"), f"{f} is missing", "P-MD", md["missing_severity"], "fail")
            for f in req if not str(obj.get(f) or "").strip()]


def check_country_of_origin(obj, P):
    c = P["country_of_origin"]
    coo = str(obj.get("CountryOfOrigin") or "").strip().upper()
    if not coo:
        return [_F("country_of_origin", obj.get("Product"), "CountryOfOrigin not specified",
                   "P-CoO", c["missing_severity"], "fail")] if c["required"] else []
    if coo in c["restricted_regions"]:
        return [_F("country_of_origin", obj.get("Product"), f"origin {coo} is RESTRICTED",
                   "P-CoO", c["restricted_severity"], "fail")]
    if coo in c["elevated_review_regions"]:
        return [_F("country_of_origin", obj.get("Product"), f"origin {coo} needs elevated review",
                   "P-CoO", c["elevated_severity"], "review")]
    return [_F("country_of_origin", obj.get("Product"), f"origin {coo} OK", "P-CoO", "info", "pass")]


def check_weights(obj, P):
    out, w = [], P["weights"]
    g, n = _num(obj.get("GrossWeight")), _num(obj.get("NetWeight"))
    if (g is not None and g < 0) or (n is not None and n < 0):
        out.append(_F("weights", obj.get("Product"), "weight is negative", "P-WT", w["nonnegative_severity"], "fail"))
    if g is not None and n is not None and n > g:
        out.append(_F("weights", obj.get("Product"), f"net {n} > gross {g}", "P-WT", w["net_le_gross_severity"], "fail"))
    return out


def check_status(obj, P):
    s = P["status"]
    if obj.get("ProductType") == "FERT" and str(obj.get("CrossPlantStatus") or "").strip() == s["fert_design_status_code"]:
        return [_F("status", obj.get("Product"), "FERT left in Design status (01)", "P-ST", s["fert_in_design_severity"], "review")]
    return []


def check_sourcing(vendor_by_comp, P):
    """vendor_by_comp: {bought_component: vendor or None}. Flags missing PIR + concentration."""
    out, s = [], P["sourcing"]
    for c, v in vendor_by_comp.items():
        if not v:
            out.append(_F("sourcing", c, "no PIR / source of supply", "P-SRC", s["missing_pir_severity"], "fail"))
    have = [v for v in vendor_by_comp.values() if v]
    if have:
        top, n = Counter(have).most_common(1)[0]
        share = n / len(have)
        if share > s["max_single_vendor_share"]:
            out.append(_F("sourcing", "(assembly)", f"vendor {top} supplies {n}/{len(have)} bought parts ({share:.0%})",
                          "P-SRC", s["share_severity"], "review"))
        elif n >= s["single_vendor_review_count"]:
            out.append(_F("sourcing", "(assembly)", f"vendor {top} on {n} components", "P-SRC", s["share_severity"], "review"))
    return out


def check_plant_extension(comp_ext: dict, plant: str, P: dict):
    """comp_ext: {component: True|False|None extended-to-`plant`}. Flags BOM components NOT extended to
    the plant -- a created component that isn't plant-extended can't be added to the plant BOM or
    procured/produced there. None (the read failed) is NEVER flagged: we don't raise a false 'missing'.
    This is the DETERMINISTIC promotion of the recurring correction 'first extend the materials' -- a
    code check the board can't forget, vs a soft lesson recall might miss."""
    c = P.get("plant_extension") or {}
    if not c.get("required", True):
        return []
    sev = c.get("missing_severity", "error")
    return [_F("plant_extension", comp, f"component not extended to plant {plant}", "P-PLANT", sev, "fail")
            for comp, ext in comp_ext.items() if ext is False]


def _summary(findings):
    sev = Counter(f["severity"] for f in findings if f["verdict"] != "pass")
    return {"error": sev.get("error", 0), "warning": sev.get("warning", 0),
            "review": sum(1 for f in findings if f["verdict"] == "review"),
            "total_findings": sum(1 for f in findings if f["verdict"] != "pass")}


# ---- I/O: read the real object graph, then run the pure checks --------------------
def _read(mat):
    try:
        d = json.loads(get_material(str(mat)))
        return d.get("d") if isinstance(d, dict) else None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _pir_vendor(comp):
    try:
        raw = explore_entity("A_PurchasingInfoRecord", filter=f"Material eq '{comp}'",
                             service="API_INFORECORD_PROCESS_SRV")
        d = json.loads(raw).get("d", {})
        rows = d.get("results", [d])
        return rows[0].get("Supplier") if rows else None
    except (json.JSONDecodeError, ValueError, TypeError, KeyError):
        return None


def _plant_extended(comp, plant):
    """True if `comp` is extended to `plant` (an A_ProductPlant row exists), False if confirmed absent,
    None if the read failed (unknown -> never flagged). Reuses the proven explore_entity read tool."""
    try:
        raw = explore_entity("A_ProductPlant", filter=f"Product eq '{comp}' and Plant eq '{plant}'",
                             service="API_PRODUCT_SRV")
        if not isinstance(raw, str) or raw.startswith("SAP request failed"):
            return None
        d = json.loads(raw).get("d", {})
        rows = d.get("results", [d]) if isinstance(d, dict) else []
        return bool(rows and rows[0].get("Product"))
    except (json.JSONDecodeError, ValueError, TypeError, KeyError):
        return None


def _read_bom_components(material, plant):
    """Read the material's REAL BOM -> {component matnr: total QUANTITY}. Authoritative, and carries
    quantity so the board can judge composition (e.g. a laptop with a Keyboard x2). {} on failure.
    Sums quantity across BOM items of the same component."""
    try:
        raw = explore_entity("MaterialBOM",
                             filter=f"Material eq '{material}' and Plant eq '{plant}'",
                             service="API_BILL_OF_MATERIAL_SRV;v=2", depth=2)
        comps = {}

        def _walk(node):
            if isinstance(node, dict):
                c = node.get("BillOfMaterialComponent")
                if c:
                    c = str(c).lstrip("0") or "0"
                    comps[c] = comps.get(c, 0) + (_num(node.get("BillOfMaterialItemQuantity")) or 1)
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)

        _walk(json.loads(raw))
        return comps
    except (json.JSONDecodeError, ValueError, TypeError, KeyError):
        return {}


def _desc(matnr):
    """EN description of a material -- '11163' means nothing to world-knowledge, 'Keyboard' does."""
    try:
        raw = explore_entity("A_ProductDescription",
                             filter=f"Product eq '{matnr}' and Language eq 'EN'", service="API_PRODUCT_SRV")
        d = json.loads(raw).get("d", {})
        rows = d.get("results", [d])
        return rows[0].get("ProductDescription", "") if rows else ""
    except (json.JSONDecodeError, ValueError, TypeError, KeyError):
        return ""


@mcp.tool()
def assure_assembly(material: str, plant: str = "1710", components: list | None = None) -> str:
    """Run the DETERMINISTIC assurance checks over a created assembly and return a findings dossier.

    Reads the real objects (parent + each component via get_material, plus each BOUGHT component's
    PIR/vendor) and applies policies.json + the data rules. Returns structured FINDINGS for the board
    to JUDGE -- it does not reason or decide. NEVER skips a check (e.g. country of origin is code).

    Args:
        material: the parent finished-good material number.
        plant:    plant (default 1710).
        components: list of component material numbers (e.g. from read_mrp_results). If omitted,
                    only the parent is assessed.

    Returns JSON: {material, plant, facts:{parent, components[]}, findings:[...], summary:{...}}.
    """
    P = _policies()
    findings, facts = [], {}

    parent = _read(material)
    if parent:
        facts["parent"] = {k: parent.get(k) for k in FACT_FIELDS}
        findings += (check_master_data(parent, P) + check_country_of_origin(parent, P)
                     + check_weights(parent, P) + check_status(parent, P))
    else:
        findings.append(_F("master_data", material, "parent material not found", "P-MD", "error", "fail"))

    # Authoritative component list + QUANTITIES: read the REAL BOM so the review checks the actual
    # current composition (not an MRP-derived summary). Merge any passed list; record the source.
    bom = _read_bom_components(material, plant)            # {matnr: quantity}
    comps = list(dict.fromkeys(list(bom.keys()) + list(components or [])))
    facts["bom_source"] = (f"{len(bom)} component(s) read live from the BOM (MaterialBOM)"
                           if bom else "BOM read returned nothing -- using the passed list")

    vendor_by_comp, comp_facts, comp_ext = {}, [], {}
    for c in comps:
        o = _read(c)
        if not o:
            findings.append(_F("master_data", c, "component material not found", "P-MD", "error", "fail"))
            continue
        comp_ext[c] = _plant_extended(c, plant)   # deterministic: is this component on the plant?
        # LEAN component fact: only the judgment-relevant fields (the checks already ran on the full
        # object and produced FINDINGS; the board doesn't need weights/units/groups echoed back).
        # This is the single biggest repeated tool result -- keeping it to ~5 fields, not 11, matters
        # because the board re-grounds on every convene (25x in one session = 110k tokens otherwise).
        comp_facts.append({"Product": o.get("Product"), "description": _desc(c),
                           "bom_quantity": bom.get(c, 1), "ProductType": o.get("ProductType"),
                           "CountryOfOrigin": o.get("CountryOfOrigin")})
        findings += check_master_data(o, P) + check_country_of_origin(o, P) + check_weights(o, P)
        if str(o.get("ProductType") or "").upper() in _BOUGHT:
            vendor_by_comp[c] = _pir_vendor(c)
    findings += check_sourcing(vendor_by_comp, P)
    findings += check_plant_extension(comp_ext, plant, P)   # promoted from the 'extend first' correction
    facts["components"] = comp_facts

    return json.dumps({"material": material, "plant": plant, "facts": facts,
                       "findings": findings, "summary": _summary(findings)}, indent=2)


@mcp.tool()
def read_policy(topic: str = "") -> str:
    """Read the org assurance POLICY as data (policies.json). The board CITES this -- it never
    invents a standard. topic (optional): country_of_origin, sourcing, master_data, weights,
    status, cost. Returns the policy JSON (the whole thing, or that topic's block)."""
    P = _policies()
    return json.dumps({topic: P[topic]} if topic and topic in P else P, indent=2)


@mcp.tool()
def read_rules(topic: str = "") -> str:
    """Read the data RULES (rules.md) -- the deterministic constraints + severities. topic
    (optional) filters to rule blocks containing that keyword (weights, status, mandatory, ...)."""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "rules.md")
    try:
        txt = open(path, encoding="utf-8").read()
    except OSError:
        return "rules.md not found"
    if topic:
        hits = [b.strip() for b in txt.split("\n\n") if topic.lower() in b.lower() and "id:" in b]
        return "\n\n".join(hits) if hits else f"no rule mentions '{topic}'"
    return txt


if __name__ == "__main__":
    mcp.run(transport="stdio")
