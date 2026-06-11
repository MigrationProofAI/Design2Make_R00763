"""LIVE change test: PATCH header fields on existing material 11055 (our ZZTEST
HAWA). Sets a GTIN (ProductStandardID) + weight, then reads back. Safe to delete.
"""
import importlib
import json

sap = importlib.import_module("mcp_server.sap")
upd = getattr(sap.update_material, "fn", sap.update_material)

MAT = "11055"
changes = {
    "ProductStandardID": "730143315289",   # GTIN/UPC -> enables barcode lookup
    "GrossWeight": "0.250",
    "WeightUnit": "KG",
}

print("=== PREVIEW (confirm=False) ===")
print(upd(material_id=MAT, fields=changes, confirm=False)[:260])

print("\n=== LIVE UPDATE (confirm=True) ===")
print(upd(material_id=MAT, fields=changes, confirm=True))

# read back the header to confirm the change stuck
raw = sap._sap_get(f"{sap.PRODUCT_SRV}/A_Product('{MAT}')")
d = json.loads(raw)["d"]
print("\nRead-back:", {k: d.get(k) for k in
                       ["Product", "ProductStandardID", "GrossWeight", "WeightUnit"]})
