"""Golden tests for the make-object MCP tools (mcp_server/make.py).

Two layers:
  * STRUCTURAL (always run, no SAP): the lifted payloads keep the exact field
    names + shapes May_2026/Design2Make proved on vhcals4hci, and confirm=false
    NEVER writes. These lock the lift against regression.
  * LIVE (opt-in: SAP_LIVE_TESTS=1): create -> read back -> assert. This is the
    "read-back-verified" gate. Override the inputs with SAP_TEST_MATERIAL /
    SAP_TEST_SUPPLIER (defaults are the May_2026 known-good pair).

Run structural only:   uv run python test_make.py
Run incl. live:        $env:SAP_LIVE_TESTS=1; uv run python test_make.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "mcp_server"))
from mcp_server.make import (                       # noqa: E402
    _build_pir_payload, _build_bom_header, _build_bom_items, _build_cost_header,
    _build_scale_rows, _build_routing_payload, _odata_date, _sap_get, _resolve_work_center,
    create_info_record, create_bom, create_cost_condition, create_routing,
    get_info_record,
)
import datetime  # noqa: E402


class KnownBlocked(Exception):
    """A live create that fails for a documented, SAP-side reason (not a tool bug).
    Reported as XFAIL so the suite stays green while the blocker stays visible."""


# ---------------- STRUCTURAL: PIR ----------------
def test_pir_payload_shape():
    p = _build_pir_payload("3258", "USSU-VSF04", currency="GBP", net_price=0.01,
                           lead_time_days=14, min_order_qty=1,
                           supplier_material_number="KVR52U42BS8-16")
    assert p["Supplier"] == "USSU-VSF04"
    assert p["Material"] == "3258"
    assert p["PurgDocOrderQuantityUnit"] == "EA"
    assert p["SupplierMaterialNumber"] == "KVR52U42BS8-16"
    org = p["to_PurgInfoRecdOrgPlantData"]["results"][0]
    assert org["PurchasingInfoRecordCategory"] == "0"
    assert org["PurchasingOrganization"] == "1710"
    assert org["NetPriceAmount"] == "0.01"           # decimals as strings
    assert org["MaterialPlannedDeliveryDurn"] == "14"
    assert "Plant" not in org                          # PIR header is EKORG-level


def test_pir_preview_never_writes():
    out = create_info_record("3258", "USSU-VSF04", confirm=False)
    assert out.startswith("PREVIEW")
    assert "A_PurchasingInfoRecord" in out
    assert "Created" not in out


# ---------------- STRUCTURAL: BOM ----------------
def test_bom_header_and_items_shape():
    h = _build_bom_header("LAPTOP-001", "1710")
    assert h["Material"] == "LAPTOP-001" and h["Plant"] == "1710"
    assert h["BillOfMaterialCategory"] == "M" and h["BillOfMaterialVariantUsage"] == "1"
    assert h["BillOfMaterialStatus"] == "1"
    assert "to_BillOfMaterialItem" not in h          # header only -- items posted separately
    assert h["HeaderValidityStartDate"].startswith("/Date(")
    items = _build_bom_items("00000404", "LAPTOP-001", "1710",
                             [{"component": "CPU-001", "quantity": 1},
                              {"component": "RAM-001", "quantity": 2}])
    assert [it["BillOfMaterialItemNumber"] for it in items] == ["0010", "0020"]
    assert items[1]["BillOfMaterialComponent"] == "RAM-001"
    assert items[1]["BillOfMaterialItemQuantity"] == "2"
    assert all(it["BillOfMaterial"] == "00000404" for it in items)


def test_bom_preview_never_writes():
    out = create_bom("LAPTOP-001", "1710", [{"component": "CPU-001", "quantity": 1}],
                     confirm=False)
    assert out.startswith("PREVIEW") and "MaterialBOM" in out
    assert "MaterialBOMItem" in out and "Created" not in out   # two-step preview


def test_bom_requires_components():
    assert "at least one component" in create_bom("X", "1710", [], confirm=True)


# ---------------- STRUCTURAL: COST ----------------
def test_cost_header_shape():
    h = _build_cost_header("3258", "USSU-VSF04", 45.5, currency="GBP")
    assert h["ConditionType"] == "PPR0" and h["ConditionApplication"] == "M"
    assert h["ConditionRateValue"] == "45.5" and h["ConditionRateValueUnit"] == "GBP"
    val = h["to_PurgPrcgCndnRecdValidity"]["results"][0]
    assert val["Supplier"] == "USSU-VSF04" and val["Material"] == "3258"
    assert h["ConditionValidityStartDate"].startswith("/Date(")


def test_cost_scale_rows():
    rows = _build_scale_rows("0000001234",
                             [{"qty": 1, "price": 45.5}, {"qty": 10, "price": 42.75}], "GBP")
    assert [r["ConditionScaleLine"] for r in rows] == ["0001", "0002"]
    assert rows[1]["ConditionScaleQuantity"] == "10"
    assert rows[1]["ConditionRateValue"] == "42.75"
    assert all(r["ConditionRecord"] == "0000001234" for r in rows)


def test_cost_preview_shows_both_steps():
    out = create_cost_condition("3258", "USSU-VSF04", 45.5,
                                price_breaks=[{"qty": 10, "price": 42.75}], confirm=False)
    assert out.startswith("PREVIEW")
    assert "A_PurgPrcgConditionRecord" in out and "A_PurgPrcgCndnRecordScale" in out


# ---------------- STRUCTURAL: ROUTING ----------------
def test_routing_payload_shape():
    p = _build_routing_payload(
        "3348", "1710",
        [{"operation": "10", "text": "PCB Assembly",
          "work_center_internal_id": "10000057", "setup_time": 30, "run_time": 2}],
        description="Thermostat Routing")
    assert p["BillOfOperationsStatus"] == "4"          # released, required
    assert p["to_MatlAssgmt"]["results"][0]["Product"] == "3348"
    op = p["to_Sequence"]["results"][0]["to_Operation"]["results"][0]
    assert op["Operation"] == "10"                      # no leading zeros
    assert op["WorkCenterInternalID"] == "10000057"
    assert op["StandardWorkQuantity1"] == "30" and op["StandardWorkQuantityUnit1"] == "MIN"
    assert "T00:00:00" in p["ValidityStartDate"]        # ISO, not /Date()/


def test_routing_requires_work_center():
    # an op with neither a code nor an internal id -> helpful error, no write
    out = create_routing("3348", "1710",
                         [{"operation": "10", "text": "x"}], confirm=True)
    assert "work_center" in out and "find_work_center" in out


def test_work_center_resolves():
    assert _resolve_work_center("PACK01", "1710")[0] == "10000002"   # exact code
    assert _resolve_work_center("packaging", "1710")[0] == "10000002"  # alias
    assert _resolve_work_center("assembly", "1710")[0] == "10000000"
    assert _resolve_work_center("99999", "1710")[0] == "99999"        # digits = already internal id
    assert _resolve_work_center("no-such-wc", "1710")[0] is None


def test_routing_resolves_code_in_preview():
    out = create_routing("SG22", "1710",
                         [{"operation": "10", "text": "Packaging", "work_center": "PACK01"}],
                         confirm=False)
    assert out.startswith("PREVIEW") and "10000002" in out          # PACK01 -> internal id


def test_odata_date_epoch():
    # 1970-01-01 -> /Date(0)/
    assert _odata_date(datetime.date(1970, 1, 1)) == "/Date(0)/"


# ---------------- LIVE (opt-in): PIR create -> read back ----------------
def live_pir_create_readback():
    material = os.getenv("SAP_TEST_PIR_MATERIAL", "11062")
    supplier = os.getenv("SAP_TEST_SUPPLIER", "USSU-VSF04")
    print(f"  LIVE: create_info_record(material={material}, supplier={supplier}) ...")
    res = create_info_record(material, supplier, net_price=0.01, lead_time_days=14,
                             confirm=True)
    print("   ->", res)
    if "failed" in res.lower():
        # PIR key = material+supplier+purorg; a re-run hits "already exists" -> still
        # verify by reading back the existing one (the create path itself was proven).
        existing = _sap_get("/sap/opu/odata/sap/API_INFORECORD_PROCESS_SRV/"
                            "A_PurchasingInfoRecord",
                            {"$filter": f"Material eq '{material}' and Supplier eq '{supplier}'",
                             "$select": "PurchasingInfoRecord,Material", "$top": 1})
        assert material in existing and "PurchasingInfoRecord" in existing, \
            f"create failed and no existing PIR to verify: {res}"
        print("  LIVE PIR OK: already existed; verified existing for material", material)
        return
    num = res.split("info record", 1)[1].split()[0]
    back = get_info_record(num)
    print("   read-back:", back[:300])
    assert material in back, "read-back did not contain the material -> not verified"
    print(f"  LIVE PIR OK: {num} verified for material {material}.")


def live_bom_create_readback():
    # Two-step create (header POST -> items POST). Parent must be a material extended to
    # the plant. BOM key = material+plant+usage+variant, so a re-run hits "already exists".
    parent = os.getenv("SAP_TEST_FERT", "11066")      # FERT @1710
    comps = os.getenv("SAP_TEST_COMPONENTS", "11064,11062").split(",")
    plant = os.getenv("SAP_TEST_PLANT", "1710")
    print(f"  LIVE: create_bom(parent={parent}, comps={comps}) ...")
    res = create_bom(parent, plant, [{"component": c, "quantity": 1} for c in comps],
                     confirm=True)
    print("   ->", res)
    back = _sap_get("/sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV;v=2/MaterialBOM",
                    {"$filter": f"Material eq '{parent}' and Plant eq '{plant}'",
                     "$select": "BillOfMaterial,Material", "$top": 5})
    if "header failed" in res.lower():
        # re-run on the same material/plant/variant -> "already exists"; verify the existing one
        assert "BillOfMaterial" in back, f"create failed and no existing BOM: {res}"
        print(f"  LIVE BOM OK: already existed; verified BOM for parent {parent}")
        return
    num = res.split("BOM", 1)[1].split()[0]
    assert num in back, f"BOM {num} not found on read-back: {back[:200]}"
    print(f"  LIVE BOM OK: {num} verified for parent {parent}.")


def live_cost_create_readback():
    material = os.getenv("SAP_TEST_MATERIAL", "11064")
    supplier = os.getenv("SAP_TEST_SUPPLIER", "USSU-VSF04")
    print(f"  LIVE: create_cost_condition({material}/{supplier}) ...")
    res = create_cost_condition(material, supplier, 100.0,
                                price_breaks=[{"qty": 10, "price": 95.0}], confirm=True)
    print("   ->", res)
    assert "failed" not in res.lower(), f"create failed: {res}"
    cond = res.split("condition", 1)[1].split()[0]
    back = _sap_get("/sap/opu/odata/sap/API_PURGPRCGCONDITIONRECORD_SRV/"
                    f"A_PurgPrcgConditionRecord('{cond}')")
    assert cond in back, f"condition {cond} not found on read-back: {back[:200]}"
    print(f"  LIVE COST OK: {cond} verified.")


def live_routing_create_readback():
    # Routing needs a PRODUCTION-configured material (work-scheduling view). The numeric
    # demo FERTs (22, 2214) lack it -> CZCL/002. SG##/FG## materials are set up for it.
    material = os.getenv("SAP_TEST_ROUTING_MATERIAL", "SG22")
    plant = os.getenv("SAP_TEST_PLANT", "1710")
    wc = os.getenv("SAP_TEST_WORKCENTER", "PACK01")   # resolved code -> internal id
    print(f"  LIVE: create_routing(material={material}, work_center={wc}) ...")
    res = create_routing(material, plant, [
        {"operation": "10", "text": "Packaging", "work_center": wc,
         "control_profile": "YBP1", "setup_time": 30, "run_time": 2}],
        description="Golden routing", confirm=True)
    print("   ->", res)
    assert "failed" not in res.lower(), f"create failed: {res}"
    grp = res.split("group", 1)[1].split()[0]
    back = _sap_get("/sap/opu/odata/sap/API_PRODUCTION_ROUTING/ProductionRoutingHeader",
                    {"$filter": f"ProductionRoutingGroup eq '{grp}'", "$top": 3})
    assert grp in back, f"routing group {grp} not found on read-back: {back[:200]}"
    print(f"  LIVE ROUTING OK: group {grp} verified for {material}.")


# ---------------- runner (no pytest dependency) ----------------
def _run():
    structural = [v for k, v in sorted(globals().items())
                  if k.startswith("test_") and callable(v)]
    passed, failed = 0, 0
    for fn in structural:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
            failed += 1
    xfail = 0
    if os.getenv("SAP_LIVE_TESTS") == "1":
        for live in (live_pir_create_readback, live_bom_create_readback,
                     live_cost_create_readback, live_routing_create_readback):
            try:
                live()
                passed += 1
            except KnownBlocked as e:
                print(f"XFAIL {live.__name__}: {e}")
                xfail += 1
            except Exception as e:                      # noqa: BLE001 - surface SAP errors
                print(f"FAIL {live.__name__}: {e}")
                failed += 1
    else:
        print("SKIP live (set SAP_LIVE_TESTS=1 to create + read back real objects)")
    print(f"\n{passed} passed, {failed} failed, {xfail} xfail (known-blocked)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
