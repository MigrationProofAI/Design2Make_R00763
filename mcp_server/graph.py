"""MCP server: the KNOWLEDGE graph (NetworkX) -- the conceptual / ontology layer.

PRINCIPLE (deliberate): the KG holds KNOWLEDGE -- type-level concepts, rules and
dependencies -- NOT transactional instances. SAP is the system of record for
instances ("material 11056 is in plant 1710"); the KG knows the CONCEPTS
("a FERT has a sales view and needs a BOM", "a ROH needs a PIR"). Agents REASON from
the KG, then ACT on SAP. So nothing here is a specific material/plant/order.

Seeded with SAP material-management ontology; extend with add_relation (the learning
loop for knowledge). Persisted to ../graph_store/ontology.json. Scale path: swap
NetworkX -> Neo4j behind these same tools.
"""
import os
import json

import networkx as nx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("graph")
STORE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "graph_store")
PATH = os.path.join(STORE, "ontology.json")

_G: nx.DiGraph | None = None


def _seed() -> nx.DiGraph:
    """The seed ontology of SAP material management (concepts + rules)."""
    G = nx.DiGraph()
    types = {
        "ROH":  {"name": "Raw material",     "valuation_class": "3000", "procurement": "external (F)"},
        "HALB": {"name": "Semi-finished",    "valuation_class": "7900", "procurement": "in-house (E)"},
        "FERT": {"name": "Finished product", "valuation_class": "7920", "procurement": "in-house (E)"},
        "HAWA": {"name": "Trading good",     "valuation_class": "3100", "procurement": "external (F)"},
        "VERP": {"name": "Packaging",        "valuation_class": "3000", "procurement": "external (F)"},
    }
    for t, props in types.items():
        G.add_node(t, kind="material_type", **props)
    for n in ["BasicData", "Purchasing", "MRP", "Sales", "Valuation",
              "WorkScheduling", "StorageLocation", "SupplyPlanning"]:
        G.add_node(n, kind="view")
    for n in ["BOM", "Routing", "PIR", "SourceList", "TaxClassification"]:
        G.add_node(n, kind="object")
    for n in ["SalesOrg", "DistributionChannel", "Plant", "CompanyCode", "Country"]:
        G.add_node(n, kind="org_concept")

    def rel(a, r, b):
        G.add_edge(a, b, relation=r)

    for t in types:                                   # all types: basic + valuation + MRP
        rel(t, "has_view", "BasicData"); rel(t, "has_view", "Valuation"); rel(t, "has_view", "MRP")
    for t in ["FERT", "HAWA"]:                         # sold types -> Sales view
        rel(t, "has_view", "Sales")
    for t in ["ROH", "HAWA", "VERP"]:                  # procured types -> Purchasing + PIR
        rel(t, "has_view", "Purchasing"); rel(t, "requires", "PIR")
    rel("ROH", "requires", "SourceList")
    for t in ["FERT", "HALB"]:                         # in-house -> BOM + Routing + WorkScheduling
        rel(t, "requires", "BOM"); rel(t, "requires", "Routing"); rel(t, "has_view", "WorkScheduling")
    rel("Routing", "requires", "WorkScheduling")       # routing needs the work-scheduling view (CZCL/002)
    rel("Sales", "requires", "TaxClassification")      # view-level dependencies
    rel("Sales", "needs", "SalesOrg"); rel("Sales", "needs", "DistributionChannel")
    rel("Purchasing", "enabled_by", "PIR")
    rel("MRP", "needs", "StorageLocation")
    rel("Plant", "in", "CompanyCode"); rel("CompanyCode", "in", "Country")
    rel("SalesOrg", "sells_through", "DistributionChannel")
    return G


def _save() -> None:
    os.makedirs(STORE, exist_ok=True)
    data = {
        "nodes": [{"id": n, **d} for n, d in _G.nodes(data=True)],
        "edges": [{"source": u, "target": v, **d} for u, v, d in _G.edges(data=True)],
    }
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load() -> nx.DiGraph:
    global _G
    if _G is None:
        if os.path.exists(PATH):
            with open(PATH, encoding="utf-8") as f:
                data = json.load(f)
            _G = nx.DiGraph()
            for n in data["nodes"]:
                n = dict(n); _G.add_node(n.pop("id"), **n)
            for e in data["edges"]:
                e = dict(e); u, v = e.pop("source"), e.pop("target"); _G.add_edge(u, v, **e)
        else:
            _G = _seed(); _save()
    return _G


@mcp.tool()
def requirements_for(material_type: str) -> str:
    """What does a material TYPE need? Returns its views, required objects, valuation
    class, procurement, and view-level dependencies -- the CONCEPTUAL knowledge an
    agent uses to reason about a create. (NOT instance data -- query SAP for instances.)

    Args:
        material_type: e.g. "FERT", "ROH", "HAWA", "HALB", "VERP".
    """
    G = _load()
    t = material_type.strip().upper()
    if t not in G or G.nodes[t].get("kind") != "material_type":
        known = [n for n, d in G.nodes(data=True) if d.get("kind") == "material_type"]
        return f"Unknown material type '{material_type}'. Known: {known}."
    node = G.nodes[t]
    views, requires, needs = [], [], []
    for _, tgt, d in G.out_edges(t, data=True):
        r = d.get("relation")
        (views if r == "has_view" else requires if r == "requires" else needs).append(tgt)
    deps = {}
    for v in views:                                    # e.g. Sales -> requires TaxClassification
        sub = [f"{d['relation']} {tgt}" for _, tgt, d in G.out_edges(v, data=True)]
        if sub:
            deps[v] = sub
    return json.dumps({
        "material_type": t, "name": node.get("name"),
        "valuation_class": node.get("valuation_class"),
        "procurement": node.get("procurement"),
        "views": views, "requires": requires,
        "view_dependencies": deps,
    }, indent=2)


@mcp.tool()
def neighbors(node: str) -> str:
    """How a concept connects: its outgoing (relation -> target) and incoming relations.
    Explore the ontology, e.g. neighbors('Sales') -> requires TaxClassification."""
    G = _load()
    if node not in G:
        return f"Unknown concept '{node}'. Try list_concepts."
    out = [{"relation": d.get("relation"), "target": t} for _, t, d in G.out_edges(node, data=True)]
    inc = [{"relation": d.get("relation"), "source": s} for s, _, d in G.in_edges(node, data=True)]
    return json.dumps({"node": node, "kind": G.nodes[node].get("kind"),
                       "out": out, "in": inc}, indent=2)


@mcp.tool()
def list_concepts() -> str:
    """List every concept in the knowledge graph, grouped by kind."""
    G = _load()
    by_kind: dict = {}
    for n, d in G.nodes(data=True):
        by_kind.setdefault(d.get("kind", "other"), []).append(n)
    return json.dumps({"concepts": by_kind, "edges": G.number_of_edges()}, indent=2)


@mcp.tool()
def add_relation(subject: str, relation: str, object: str) -> str:
    """Add a concept-level relation (the learning loop for KNOWLEDGE), e.g.
    'FERT requires QualityView'. Do NOT add instance/transactional facts -- those
    belong in SAP. Persists to the ontology.

    Args:
        subject: the concept the relation starts from (e.g. "FERT").
        relation: the relationship (e.g. "requires", "has_view").
        object: the concept it points to (e.g. "QualityView").
    """
    G = _load()
    for n in (subject, object):
        if n not in G:
            G.add_node(n, kind="concept")
    G.add_edge(subject, object, relation=relation)
    _save()
    return f"Remembered concept: {subject} -{relation}-> {object}."


if __name__ == "__main__":
    mcp.run(transport="stdio")
