"""LIVE sub-view change test on existing material 11055: PATCH a description,
PATCH a tax classification, POST a new (German) description. Reads back. Safe to delete.
"""
import importlib
import json

sap = importlib.import_module("mcp_server.sap")
chg = getattr(sap.change_material_view, "fn", sap.change_material_view)
MAT = "11055"

print("=== PREVIEW (confirm=False) ===")
print(chg(entity="A_ProductDescription", keys={"Product": MAT, "Language": "EN"},
          fields={"ProductDescription": "ZZTEST changed via change_material_view"},
          operation="update", confirm=False)[:240])

print("\n=== 1) PATCH existing EN description ===")
print(chg(entity="A_ProductDescription", keys={"Product": MAT, "Language": "EN"},
          fields={"ProductDescription": "ZZTEST changed via change_material_view"},
          operation="update", confirm=True))

print("\n=== 2) PATCH existing tax classification US/UTXJ  1 -> 0 ===")
print(chg(entity="A_ProductSalesTax",
          keys={"Product": MAT, "Country": "US", "TaxCategory": "UTXJ"},
          fields={"TaxClassification": "0"}, operation="update", confirm=True))

print("\n=== 3) ADD a new German description row (POST) ===")
print(chg(entity="A_ProductDescription", keys={"Product": MAT, "Language": "DE"},
          fields={"ProductDescription": "ZZTEST DE Beschreibung"},
          operation="add", confirm=True))

# read back
raw = sap._sap_get(f"{sap.PRODUCT_SRV}/A_Product('{MAT}')",
                   {"$expand": "to_Description,to_SalesDelivery/to_SalesTax"})
d = json.loads(raw)["d"]
print("\nDescriptions:", [(x["Language"], x["ProductDescription"])
                          for x in d["to_Description"]["results"]])
for sd in d["to_SalesDelivery"]["results"]:
    for t in sd["to_SalesTax"]["results"]:
        print("Tax:", t["Country"], t["TaxCategory"], "->", repr(t["TaxClassification"]))
