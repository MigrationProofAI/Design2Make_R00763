"""Read-only: verify all views of a material were created. Safe to delete.
Usage: python check_material.py [material_id]   (default 11056)
"""
import importlib
import json
import sys

sap = importlib.import_module("mcp_server.sap")
MAT = sys.argv[1] if len(sys.argv) > 1 else "11056"
SRV = sap.PRODUCT_SRV


def get(path, params=None):
    raw = sap._sap_get(path, params)
    try:
        return json.loads(raw)["d"]
    except Exception:
        return {"_error": raw[:200]}


def rows(d, key):
    v = d.get(key, {})
    return v.get("results", []) if isinstance(v, dict) else []


def mark(ok):
    return "[OK]   " if ok else "[MISS] "


d = get(f"{SRV}/A_Product('{MAT}')",
        {"$expand": "to_Description,to_Plant,to_SalesDelivery/to_SalesTax,to_Valuation"})
sp = get(f"{SRV}/A_ProductSupplyPlanning", {"$filter": f"Product eq '{MAT}'"})
st = get(f"{SRV}/A_ProductStorageLocation", {"$filter": f"Product eq '{MAT}'"})

print(f"\n=== Material {MAT}: view checklist ===")
print(mark(d.get("Product") == MAT),
      f"Header: type={d.get('ProductType')} group={d.get('ProductGroup')} "
      f"unit={d.get('BaseUnit')} industry={d.get('IndustrySector')} div={d.get('Division')}")

desc = rows(d, "to_Description")
print(mark(bool(desc)), "Description:", [(x["Language"], x["ProductDescription"]) for x in desc])

plants = rows(d, "to_Plant")
p0 = plants[0] if plants else {}
print(mark(bool(plants)),
      f"Plant view: {[p['Plant'] for p in plants]} | MRPType={p0.get('MRPType')} "
      f"Proc={p0.get('ProcurementType')} PurchGrp={p0.get('PurchasingGroup')} "
      f"ProfitCtr={p0.get('ProfitCenter')}")

spr = rows(sp, "results") if "results" in sp else sp.get("results", [])
print(mark(bool(spr)),
      f"Supply planning: " + (f"LotSizing={spr[0].get('LotSizingProcedure')} "
      f"PlndDelivDays={spr[0].get('PlannedDeliveryDurationInDays')}" if spr else "none"))

str_ = st.get("results", [])
print(mark(bool(str_)),
      "Storage location: " + (", ".join(f"{r['Plant']}/{r['StorageLocation']}" for r in str_) or "none"))

val = rows(d, "to_Valuation")
v0 = val[0] if val else {}
print(mark(bool(val)),
      f"Valuation: class={v0.get('ValuationClass')} price={v0.get('StandardPrice')} "
      f"{v0.get('Currency')} proc={v0.get('InventoryValuationProcedure')}")

sd = rows(d, "to_SalesDelivery")
s0 = sd[0] if sd else {}
print(mark(bool(sd)),
      f"Sales view: org={s0.get('ProductSalesOrg')} channel={s0.get('ProductDistributionChnl')} "
      f"itemCatGrp={s0.get('ItemCategoryGroup')}")

tax = s0.get("to_SalesTax", {}).get("results", []) if s0 else []
print(mark(len(tax) >= 1),
      "Tax classification:", [(t["Country"], t["TaxCategory"], t["TaxClassification"]) for t in tax])
