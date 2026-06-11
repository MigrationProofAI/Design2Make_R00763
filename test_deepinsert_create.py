"""LIVE deep-insert test (attempt 4): full payload mirroring material TG20 --
plant + valuation + sales + COMPLETE tax with TaxClassification='1' (Full tax)
for both DE/TTX1 and US/UTXJ, per the SAP GUI screenshot. Reports number/error.
"""
import importlib
import json

sap = importlib.import_module("mcp_server.sap")
bmp = getattr(sap.build_material_payload, "fn", sap.build_material_payload)
create = getattr(sap.create_material, "fn", sap.create_material)

# Mirror TG20: HAWA trading good, base unit PC, material group L001.
fields = json.loads(bmp(
    description="ZZTEST DeepInsert FullTax v2",
    product_type="HAWA", base_unit="PC", product_group="L001",
    valuation_class="3100",          # 3100 = Trading Goods (3000 is raw materials)
    plant="1710", sales_org="1710"))["fields"]

# Complete tax set with classification '1' = Full tax (from the TG20 screenshot).
fields["to_SalesDelivery"]["results"][0]["to_SalesTax"] = {"results": [
    {"Country": "DE", "TaxCategory": "TTX1", "TaxClassification": "1"},
    {"Country": "US", "TaxCategory": "UTXJ", "TaxClassification": "1"},
]}

print("to_SalesDelivery being sent:")
print(json.dumps(fields["to_SalesDelivery"], indent=2))
print("\n=== LIVE CREATE (confirm=True) ===")
print(create(fields=fields, confirm=True))
