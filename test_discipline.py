"""Unit tests for the S0-S8 discipline spine -- pure logic, no SAP. Run: uv run python test_discipline.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "mcp_server"))
import discipline as D  # noqa: E402


def test_confidence_calibration():
    assert D.score_confidence("created", True)[0] == 0.95     # read-back-confirmed earns the top score
    assert D.score_confidence("created", False)[0] == 0.6     # created but unverified is NOT trusted
    assert D.score_confidence("exists", False)[0] == 1.0      # idempotent
    assert D.score_confidence("failed", False)[0] == 0.0
    print("ok  S2 confidence is calibrated to verification state")


def test_weakest_link_and_escalation():
    sp = D.Spine("genesis")                                   # material=None -> reopen reads nothing
    sp.record("a", "create", "created", obj=None, verified=True)    # 0.95
    sp.record("b", "create", "created", obj=None, verified=False)   # 0.6  -> escalates (<0.7)
    sp.record("c", "reuse", "exists", obj=None)                     # 1.0
    sp.record("d", "create", "failed")                             # 0.0  -> escalates
    dossier = sp.finalize(persist=False)
    assert dossier["overall_confidence"] == 0.0, "overall is the WEAKEST link, not the average"
    assert dossier["verdict"].startswith("ESCALATE")
    stages_escalated = {e["stage"] for e in dossier["escalations"]}
    assert stages_escalated == {"b", "d"}, stages_escalated      # only the sub-threshold ones
    print("ok  S2->S7  weakest-link rollup + only low-confidence stages escalate")


def test_policy_error_caps_confidence():
    # A FERT with a RESTRICTED country of origin must drag confidence down even if 'created+verified'.
    bad = {"Product": "X", "ProductType": "FERT", "BaseUnit": "EA", "ProductGroup": "L001",
           "CountryOfOrigin": "RU", "GrossWeight": "1", "NetWeight": "1", "WeightUnit": "KG",
           "CrossPlantStatus": ""}
    sp = D.Spine("genesis")
    rec = sp.record("parent", "create FERT", "created", obj=bad, verified=True)
    assert rec["confidence"] <= 0.4, rec["confidence"]          # policy ERROR caps trust
    assert any(f["severity"] == "error" for f in rec["policy"])
    print("ok  S4->S2  a policy ERROR caps stage confidence")


def test_kg_merge_and_edges():
    sp = D.Spine("genesis")
    sp.add_kg("P", "FERT", description="Laptop")               # attrs set
    sp.add_kg("P", "FERT", edges=[("uses", "C", {"quantity": 2})])  # later call must KEEP description
    assert sp.kg_nodes["P"]["description"] == "Laptop", sp.kg_nodes["P"]
    assert sp.kg_edges == [{"source": "P", "relation": "uses", "target": "C", "quantity": 2}]
    print("ok  S6  KG node attrs merge across repeated add_kg calls")


def test_reopen_ledger():
    with tempfile.TemporaryDirectory() as td:
        D._LEDGER = os.path.join(td, "ledger.json")           # redirect sidecars off the real files
        D._KG_INST = os.path.join(td, "kg.json")
        # run 1: first time -> everything reopened
        sp1 = D.Spine("genesis", material="M1")
        sp1.record("parent", "create", "created", obj=None, verified=True, inputs={"desc": "v1"})
        sp1.record("bom", "create", "created", obj=None, verified=True, inputs={"comp": ["A", "B"]})
        plan1 = sp1.finalize()["reopen_plan"]
        assert plan1 == {"parent": "reopened (inputs changed)", "bom": "reopened (inputs changed)"}, plan1
        # run 2: parent inputs UNCHANGED, bom inputs CHANGED -> only bom reopens (S8)
        sp2 = D.Spine("genesis", material="M1")
        sp2.record("parent", "create", "created", obj=None, verified=True, inputs={"desc": "v1"})
        sp2.record("bom", "create", "created", obj=None, verified=True, inputs={"comp": ["A", "B", "C"]})
        plan2 = sp2.finalize()["reopen_plan"]
        assert plan2 == {"parent": "unchanged", "bom": "reopened (inputs changed)"}, plan2
    print("ok  S8  re-run reopens ONLY the stage whose inputs changed")


if __name__ == "__main__":
    test_confidence_calibration()
    test_weakest_link_and_escalation()
    test_policy_error_caps_confidence()
    test_kg_merge_and_edges()
    test_reopen_ledger()
    print("\nALL DISCIPLINE TESTS PASSED")
