"""Unit test for the material deep-insert payload builder.
NO SAP writes -- build is pure, and create_material is called with confirm=False
(preview only, returns before any HTTP). Safe to delete.  Run: python test_payload.py
"""
import importlib
import json

sap = importlib.import_module("mcp_server.sap")
bmp = getattr(sap.build_material_payload, "fn", sap.build_material_payload)
create = getattr(sap.create_material, "fn", sap.create_material)

# Full payload: header + plant(+supply+storage) + valuation + sales + tax
out = json.loads(bmp(
    description="Pipeline Test Gasket 01", product_type="FERT",
    plant="1710", sales_org="1710"))
f = out["fields"]
print(json.dumps(f, indent=2))

# --- structural assertions (the deep-insert shape must be exactly right) ---
desc = f["to_Description"]["results"][0]
assert desc["ProductDescription"] == "Pipeline Test Gasket 01", desc
plant = f["to_Plant"]["results"][0]
assert plant["Plant"] == "1710"
# supply planning is a BARE object (not results[]) -- the easy thing to get wrong
sp = plant["to_ProductSupplyPlanning"]
assert sp["LotSizingProcedure"] == "EX" and "results" not in sp, sp
assert plant["to_StorageLocation"]["results"][0]["StorageLocation"] == "171A"
assert f["to_Valuation"]["results"][0]["ValuationClass"] == "7920", "FERT -> 7920"
# sales view + COMPLETE tax-classification set (avoids MG/172)
sd = f["to_SalesDelivery"]["results"][0]
assert sd["ProductSalesOrg"] == "1710"
tax = {(t["Country"], t["TaxCategory"]): t["TaxClassification"]
       for t in sd["to_SalesTax"]["results"]}
assert tax == {("DE", "TTX1"): "1", ("US", "UTXJ"): "1"}, tax
# FERT is sales-relevant -> header ItemCategoryGroup NORM
assert f["ItemCategoryGroup"] == "NORM"
print("\n[PASS] ALL STRUCTURE ASSERTIONS PASSED")

# header-only payload (no plant/sales) should omit those sections
mini = json.loads(bmp(description="Header only", product_type="ROH"))["fields"]
assert "to_Plant" not in mini and "to_SalesDelivery" not in mini
assert "ItemCategoryGroup" not in mini  # ROH is not sales-relevant
print("[PASS] optional-section gating works (header-only payload is minimal)")

# preview path -> NO write
print("\n--- create_material(confirm=False) preview (no write) ---")
print(create(fields=f, confirm=False)[:300])
