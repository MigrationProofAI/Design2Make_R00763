# Learned knowledge — material create / change

Durable, confirmed facts the pipeline applies. One lesson per line. New lessons are
appended by the `remember` tool when an agent confirms something non-obvious
(especially right after fixing an error).

## Field names (use the EXACT OData field; never guess)
- Size / dimensions -> SizeOrDimensionText (NOT "SizeDimension").
- Net weight -> NetWeight; gross weight -> GrossWeight; both pair with WeightUnit (e.g. "KG").
- GTIN / barcode / UPC -> ProductStandardID.
- Cross-plant material status -> CrossPlantStatus.

## Codes
- Valuation class by material type: ROH 3000, HAWA 3100, HALB 7900, FERT 7920.

## Business rules
- A sales view (to_SalesDelivery) needs the COMPLETE tax set or SAP raises MG/172:
  DE/TTX1 and US/UTXJ, each TaxClassification "1" (Full tax). Blank is rejected on create.
- Supply planning (to_ProductSupplyPlanning) does NOT persist as a nested deep insert;
  add it separately via change_material_view add (POST A_ProductSupplyPlanning).
- To ADD a sales org to an EXISTING material: change_material_view add on
  A_ProductSalesDelivery with view fields ONLY (e.g. ItemCategoryGroup) and NO
  to_SalesTax. Tax is product-level (A_ProductSalesTax key = Product+Country+TaxCategory,
  already maintained from create); re-sending it on a child add triggers API_PRD_MSG/003
  "Cannot process multiple products in a single change set request".
- Material number is auto-assigned when Product is omitted (internal numbering).
- To CHANGE a purchasing / PIR PRICE: the rate is NOT on the PIR header or its validity
  entity. It lives on A_PurgPrcgConditionRecord in service API_PURGPRCGCONDITIONRECORD_SRV
  (key = ConditionRecord only). Find the ConditionRecord by expanding the PIR
  (explore_entity A_PurchasingInfoRecord -> to_PurgInfoRecdOrgPlantData ->
  to_PurInfoRecdPrcgCndnValidity), then PATCH via
  change_material_view(entity="A_PurgPrcgConditionRecord", keys={"ConditionRecord": <num>},
  fields={"ConditionRateValue": <new>, "ConditionRateValueUnit": <currency>},
  service="API_PURGPRCGCONDITIONRECORD_SRV"). You MUST send BOTH the rate AND its unit
  together (else PRCG_CNDNRECORD_API/020); the row is etag-protected (the tool handles If-Match).
- change_material_view is service-aware: for a NON-product child entity pass service=... and
  discover the exact key first with explore_entity / list_fields (datetime keys are handled).
- PIR org-level fields (Incoterms, tolerances, purchasing group, delivery time) live on
  A_PurgInfoRecdOrgPlantData in API_INFORECORD_PROCESS_SRV; key = PurchasingInfoRecord +
  PurchasingInfoRecordCategory + PurchasingOrganization + Plant (Plant='' at EKORG level).
  e.g. change Incoterms: change_material_view("A_PurgInfoRecdOrgPlantData", keys={...},
  fields={"IncotermsClassification": "DDP"}, service="API_INFORECORD_PROCESS_SRV"). The tool
  echoes the keys into the PATCH body automatically (this service needs it, else CM_MGW_RT/022).

## Enrichment
- Net/gross weight and dimensions can be found on the web via google_search
  (e.g. "AMD Ryzen 7 9800X3D net weight kg"). Cite the source; never fabricate a spec.
- Division for new materials defaults to 00.
- No specific code for WeightUnit in the system, but "KG" is used as default.
- Net weight for material 11064 is updated using 'NetWeight' and 'WeightUnit' fields.
- The procurement type 'X' for both in-house and external procurement can be set using the 'ProcurementType' field in the entity 'A_ProductPlant'.
- Dependent requirements in MRP are represented by the fields 'Reservation' and 'ReservationItem' in the entity 'A_MaterialDocumentItem' of the service 'API_MATERIAL_DOCUMENT_SRV'.
- The entity 'A_MaterialDocumentItem' in 'API_MATERIAL_DOCUMENT_SRV' has keys 'MaterialDocumentYear', 'MaterialDocument', and 'MaterialDocumentItem'.
- Routing counter field is PLNAL in create_production_version.
- CountryOfOrigin field is not in the code book.
