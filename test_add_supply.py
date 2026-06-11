"""Add the missing supply-planning view to 11056 via change_material_view (POST),
then verify it persisted. Safe to delete."""
import importlib
import json

sap = importlib.import_module("mcp_server.sap")
chg = getattr(sap.change_material_view, "fn", sap.change_material_view)

keys = {"Product": "11056", "Plant": "1710"}
fields = {
    "LotSizingProcedure": "EX", "MRPType": "PD", "MRPResponsible": "001",
    "ProcurementType": "F", "AvailabilityCheckType": "02",
    "PlannedDeliveryDurationInDays": "10", "TotalReplenishmentLeadTime": "4",
}

print("=== PREVIEW ===")
print(chg(entity="A_ProductSupplyPlanning", keys=keys, fields=fields,
          operation="add", confirm=False)[:220])
print("\n=== ADD (confirm=True) ===")
print(chg(entity="A_ProductSupplyPlanning", keys=keys, fields=fields,
          operation="add", confirm=True)[:300])

raw = sap._sap_get(f"{sap.PRODUCT_SRV}/A_ProductSupplyPlanning",
                   {"$filter": "Product eq '11056'"})
rows = json.loads(raw)["d"]["results"]
print("\nSupply-planning rows for 11056 now:",
      [(r["Product"], r["Plant"], r.get("LotSizingProcedure"), r.get("MRPType")) for r in rows])
