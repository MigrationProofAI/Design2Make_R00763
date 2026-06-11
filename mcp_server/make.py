"""MCP server exposing the SAP "Make" master-data objects as confirm-gated tools.

sap.py owns API_PRODUCT_SRV (the material itself). The objects you build AROUND a
material live on OTHER OData services, so they get their own server here:

    Purchase Info Record  -> API_INFORECORD_PROCESS_SRV          (the BOUGHT path)
    Cost condition        -> API_PURGPRCGCONDITIONRECORD_SRV     (price scales)
    Bill of Material      -> API_BILL_OF_MATERIAL_SRV;v=2        (the MADE structure)
    Routing               -> API_PRODUCTION_ROUTING              (the MADE operations)

The create logic (services, entity sets, payload field names, business-rule defaults
and gotchas) is LIFTED from May_2026/Design2Make's Agents*Create*.py + the
SAPODataClientExtended payload builders, and re-grounded here as MCP tools.

Safety: every write is confirm-gated EXACTLY like sap.py's create_material --
confirm=false (default) returns a PREVIEW of the exact request and writes nothing;
confirm=true performs the write. Read-back is the caller's/tests' job (use sap.py's
_sap_get or the get_* tools) so a "success" is proven, not assumed.

Connection reuse: we import sap.py's shared TLS session + auth + _sap_get rather than
re-implementing them (same precedent as vector.py importing from sap). One session,
one CSRF flow, one .env -- so this server can never drift from sap.py's connection.

Run standalone:  python ./mcp_server/make.py     (ADK launches it over stdio)
"""
import os
import sys
import json
import datetime

import requests

# Reuse sap.py's connection layer verbatim (TLS adapter, auth, client, GET helper).
sys.path.insert(0, os.path.dirname(__file__))
from sap import (sap_session, SAP_BASE_URL, SAP_CLIENT, SAP_USER, SAP_PASS,  # noqa: E402
                 _esc, _sap_get, change_material_view)
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("make")

# --- service roots (after host) ------------------------------------------------
INFOREC_SRV = f"{SAP_BASE_URL}/sap/opu/odata/sap/API_INFORECORD_PROCESS_SRV"
COND_SRV    = f"{SAP_BASE_URL}/sap/opu/odata/sap/API_PURGPRCGCONDITIONRECORD_SRV"
BOM_SRV     = f"{SAP_BASE_URL}/sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV;v=2"
ROUTING_SRV = f"{SAP_BASE_URL}/sap/opu/odata/sap/API_PRODUCTION_ROUTING"

# alias -> root, used by the generic change tool + previews
_SERVICES = {"inforecord": INFOREC_SRV, "cost": COND_SRV,
             "bom": BOM_SRV, "routing": ROUTING_SRV}

# _sap_get PREPENDS the base, so READS take a RELATIVE path; the *_SRV (full) urls are for _post writes.
INFOREC_PATH = INFOREC_SRV.replace(SAP_BASE_URL, "")
BOM_PATH     = BOM_SRV.replace(SAP_BASE_URL, "")
ROUTING_PATH = ROUTING_SRV.replace(SAP_BASE_URL, "")

# Defaults verified on vhcals4hci in the May_2026 project (design2make_config.py).
_DEF_PURORG   = os.getenv("SAP_PURCH_ORG", "1710")
_DEF_PURGROUP = os.getenv("SAP_PURCH_GROUP", "001")
_DEF_PLANT    = os.getenv("SAP_PLANT", "1710")
_DEF_CURRENCY = os.getenv("SAP_CURRENCY", "USD")
_DEF_LANG     = os.getenv("SAP_LANG", "EN")

# Write params: match Design2Make (it passes sap-language on every write). PIR/cost
# already succeed without it, but BOM/routing transactions can be language-sensitive.
_WRITE_PARAMS = {"sap-client": SAP_CLIENT, "sap-language": _DEF_LANG}


# ---- shared write plumbing ---------------------------------------------------
def _odata_date(d: datetime.date) -> str:
    """SAP OData v2 wants /Date(<epoch-millis-UTC>)/ for Edm.DateTime fields
    (BOM/cost validity). Date-only, midnight UTC -- matches Design2Make's
    odata_date_y_m_d()."""
    epoch = datetime.datetime(1970, 1, 1)
    ms = int((datetime.datetime(d.year, d.month, d.day) - epoch).total_seconds() * 1000)
    return f"/Date({ms})/"


def _csrf(service_root: str) -> str:
    """Fetch a CSRF token from THIS service's root on the shared session. OData v2
    rejects any write without it; the same sap_session (cookies) is reused for the
    write. Each service issues against its own root, so we fetch per service."""
    resp = sap_session.get(
        f"{service_root}/", params={"sap-client": SAP_CLIENT},
        auth=(SAP_USER, SAP_PASS),
        headers={"X-CSRF-Token": "Fetch", "Accept": "application/json"},
        timeout=120, verify=False)
    return resp.headers.get("x-csrf-token", "")


def _preview(verb: str, url: str, body: dict | list | None) -> str:
    body_str = "" if body is None else "\n" + json.dumps(body, indent=2, ensure_ascii=False)
    return ("PREVIEW -- nothing written. Confirm with the user, then call again "
            f"with confirm=true.\n{verb} {url}{body_str}")


_DATA = "@@DATA@@"     # sentinel: <readable text>@@DATA@@<json>. main.py strips it for chat/activity
                       # and parses the JSON for a typed Data card. Genesis (which calls these tools
                       # in-process) strips it too -- see genesis._plain.


def _card(text: str, kind: str, status: str, **fields) -> str:
    """Append a compact structured block so a make object renders as a typed card."""
    try:
        return text + "\n" + _DATA + json.dumps({"kind": kind, "status": status, **fields},
                                                 ensure_ascii=False, default=str)
    except Exception:
        return text


def _post(service_root: str, entity: str, payload: dict,
          token: str | None = None) -> tuple[bool, int, str]:
    """POST a (deep-insert) payload. Returns (ok, status, text). token may be
    reused across a multi-POST flow (cost scales) to avoid re-fetching."""
    if token is None:
        token = _csrf(service_root)
    if not token:
        return False, 0, "Could not obtain a CSRF token from SAP (write blocked)."
    try:
        resp = sap_session.post(
            f"{service_root}/{entity}", params=_WRITE_PARAMS,
            auth=(SAP_USER, SAP_PASS),
            headers={"X-CSRF-Token": token, "Content-Type": "application/json",
                     "Accept": "application/json"},
            json=payload, timeout=120, verify=False)
        resp.raise_for_status()
        return True, resp.status_code, resp.text
    except requests.exceptions.RequestException as e:
        resp = getattr(e, "response", None)
        detail = resp.text[:800] if resp is not None else ""
        status = resp.status_code if resp is not None else 0
        return False, status, f"{e}\n{detail}"


def _extract(text: str, field: str) -> str | None:
    """Pull a key out of an OData v2 JSON response (d.{field} or d.results[0].{field})."""
    try:
        d = json.loads(text).get("d", {})
    except (json.JSONDecodeError, AttributeError):
        return None
    if field in d:
        return d[field]
    rows = d.get("results")
    if isinstance(rows, list) and rows and field in rows[0]:
        return rows[0][field]
    return None


# ---- work-center resolution (code/name -> CRHD internal id) ------------------
# The routing API accepts ONLY WorkCenterInternalID ("Property 'WorkCenter' is invalid"),
# and the work-center master OData is 403 on this appliance. So we resolve a human
# work-center code/name (e.g. "PACK01" / "packaging") to its internal id from a
# maintained map (work_centers.json), letting users/agents speak in business terms.
_WORK_CENTERS = None


def _load_work_centers() -> dict:
    """Load work_centers.json: {plant: [ {work_center, internal_id, desc, aliases} ]}."""
    global _WORK_CENTERS
    if _WORK_CENTERS is None:
        path = os.path.join(os.path.dirname(__file__), "work_centers.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            _WORK_CENTERS = {k: v for k, v in data.items() if not k.startswith("_")}
        except (OSError, json.JSONDecodeError):
            _WORK_CENTERS = {}
    return _WORK_CENTERS


def _resolve_work_center(query: str, plant: str = _DEF_PLANT) -> tuple[str | None, list]:
    """Resolve a work-center code/name/alias to its internal id for a plant.
    A digit-only query is already an internal id. Returns (internal_id_or_None,
    candidate_rows) -- internal_id is None when nothing or several things match."""
    q = str(query).strip()
    if not q:
        return None, []
    if q.isdigit():                              # already an internal id
        return q, []
    rows = _load_work_centers().get(str(plant), [])
    ql = q.lower()
    for r in rows:                               # 1) exact work-center code
        if r.get("work_center") and r["work_center"].lower() == ql:
            return r["internal_id"], [r]
    for r in rows:                               # 2) exact alias
        if any(ql == a.lower() for a in r.get("aliases", [])):
            return r["internal_id"], [r]
    for r in rows:                               # 3) exact description
        if (r.get("desc", "") or "").lower() == ql:
            return r["internal_id"], [r]
    hits = [r for r in rows                      # 4) substring -- only if unique
            if ql in (r.get("desc", "") or "").lower()
            or any(ql in a.lower() for a in r.get("aliases", []))]
    return (hits[0]["internal_id"], hits) if len(hits) == 1 else (None, hits)


@mcp.tool()
def find_work_center(query: str, plant: str = _DEF_PLANT) -> str:
    """Resolve a work-center CODE or NAME (e.g. 'PACK01' or 'packaging') to its SAP
    internal id for a plant -- needed by create_routing, which accepts only the internal
    id. The work-center master service isn't reachable here, so this reads a maintained
    map (mcp_server/work_centers.json) verified from live routing operations.

    Args:
        query: work-center code ('PACK01'), description or keyword ('packaging', 'assembly').
        plant: plant code, default '1710'.
    """
    iid, matches = _resolve_work_center(query, plant)
    if iid:
        m = matches[0] if matches else {}
        return json.dumps({"query": query, "plant": plant, "internal_id": iid,
                           "work_center": m.get("work_center"), "desc": m.get("desc")})
    known = matches or _load_work_centers().get(str(plant), [])
    return json.dumps({"query": query, "plant": plant, "internal_id": None,
                       "candidates": [{"work_center": r.get("work_center"),
                                       "internal_id": r.get("internal_id"),
                                       "desc": r.get("desc")} for r in known],
                       "hint": "Pick one; pass its code as work_center to create_routing."},
                      indent=2)


# =============================================================================
# 1) PURCHASE INFO RECORD  (the BOUGHT path)   ME11
# =============================================================================
def _build_pir_payload(material: str, supplier: str, *, purchasing_org: str = _DEF_PURORG,
                       purchasing_group: str = _DEF_PURGROUP, currency: str = _DEF_CURRENCY,
                       net_price: float = 0.01, order_unit: str = "EA",
                       lead_time_days: int = 10, min_order_qty: int = 1,
                       supplier_material_number: str | None = None,
                       category: str = "0") -> dict:
    """A_PurchasingInfoRecord deep insert. The PIR is created at PURCHASING-ORG level
    (category "0", no Plant on the org row) -- the org data carries price + lead time."""
    org = {
        "PurchasingInfoRecordCategory": category,
        "PurchasingOrganization": purchasing_org,
        "PurchasingGroup": purchasing_group,
        "Currency": currency,
        "NetPriceAmount": str(net_price),
        "PurgDocOrderQuantityUnit": order_unit,
        "MaterialPlannedDeliveryDurn": str(lead_time_days),
        "MinimumPurchaseOrderQuantity": str(min_order_qty),
        "StandardPurchaseOrderQuantity": str(min_order_qty),
    }
    payload = {
        "Supplier": supplier,
        "Material": material,
        "PurgDocOrderQuantityUnit": order_unit,
        "to_PurgInfoRecdOrgPlantData": {"results": [org]},
    }
    if supplier_material_number:
        payload["SupplierMaterialNumber"] = str(supplier_material_number)[:35]
    return payload


@mcp.tool()
def create_info_record(material: str, supplier: str, purchasing_org: str = _DEF_PURORG,
                       purchasing_group: str = _DEF_PURGROUP, currency: str = _DEF_CURRENCY,
                       net_price: float = 0.01, lead_time_days: int = 10,
                       min_order_qty: int = 1, supplier_material_number: str | None = None,
                       confirm: bool = False) -> str:
    """Create a Purchase Info Record (material<->supplier price + lead time) in SAP.

    This is the BOUGHT-component path: it tells purchasing/MRP who supplies the
    material, at what price, with what delivery time -- so MRP can raise a purchase
    requisition against it.

    SAFETY GATE: confirm=false (default) only PREVIEWS the exact POST -- nothing is
    written. Preview, get the user's approval, then call again with confirm=true.

    Args:
        material: the material/product number the PIR is for, e.g. "3258".
        supplier: SAP supplier (business partner) code, e.g. "USSU-VSF04".
        purchasing_org: purchasing org, default "1710".
        purchasing_group: purchasing group, default "001".
        currency: price currency, default from .env (USD).
        net_price: net price amount (decimals ok), default 0.01.
        lead_time_days: planned delivery time in days, default 10.
        min_order_qty: minimum/standard purchase order quantity, default 1.
        supplier_material_number: the vendor's part number (MPN), optional, 35 chars.
        confirm: must be true to actually create.
    """
    payload = _build_pir_payload(
        material, supplier, purchasing_org=purchasing_org,
        purchasing_group=purchasing_group, currency=currency, net_price=net_price,
        lead_time_days=lead_time_days, min_order_qty=min_order_qty,
        supplier_material_number=supplier_material_number)
    entity = "A_PurchasingInfoRecord"
    _cardf = dict(material=material, supplier=supplier, purch_org=purchasing_org,
                  price=net_price, currency=currency, lead_time_days=lead_time_days)
    if not confirm:
        return _card(_preview("POST", f"{INFOREC_SRV}/{entity}", payload), "pir", "preview", **_cardf)
    ok, status, text = _post(INFOREC_SRV, entity, payload)
    if not ok:
        return f"Create info record failed (HTTP {status}): {text}"
    num = _extract(text, "PurchasingInfoRecord")
    return _card(f"Created purchase info record {num} for material {material} / supplier "
                 f"{supplier} (HTTP {status}). Read back with get_info_record('{num}').",
                 "pir", "written", number=num, **_cardf)


@mcp.tool()
def get_info_record(info_record: str) -> str:
    """Read a Purchase Info Record header back by its OWN number (e.g. 53000xxxxx). For
    "show the PIR for material X", use read_pir instead -- a material number is NOT a PIR number."""
    return _sap_get(f"{INFOREC_SRV}/A_PurchasingInfoRecord('{_esc(info_record)}')")


@mcp.tool()
def read_pir(material: str, supplier: str = "") -> str:
    """READ Purchase Info Record(s) for a MATERIAL. The user usually gives the material (and maybe a
    supplier), NOT the PIR's own number -- so this finds the PIR(s) by material and shows each
    supplier's price, currency, lead time and min order qty. Use for "show me the PIR for X".

    Args:
        material: the material/product number, e.g. "11221".
        supplier: optional -- narrow to one supplier.
    """
    filt = f"Material eq '{_esc(material)}'" + (f" and Supplier eq '{_esc(supplier)}'" if supplier else "")
    raw = _sap_get(f"{INFOREC_PATH}/A_PurchasingInfoRecord",
                   {"$filter": filt, "$expand": "to_PurgInfoRecdOrgPlantData", "$top": 25})
    try:
        rows = json.loads(raw).get("d", {}).get("results", [])
    except (json.JSONDecodeError, AttributeError):
        rows = []
    if not rows:
        return _card(f"No purchase info record found for material {material}"
                     + (f" / supplier {supplier}" if supplier else "") + ".",
                     "pir", "read", material=material, supplier=supplier or None, sources=[])
    sources = []
    for r in rows:
        o = ((r.get("to_PurgInfoRecdOrgPlantData") or {}).get("results") or [{}])[0]
        sources.append({"number": r.get("PurchasingInfoRecord"), "supplier": r.get("Supplier"),
                        "price": o.get("NetPriceAmount"), "currency": o.get("Currency"),
                        "purch_org": o.get("PurchasingOrganization"),
                        "lead_time_days": o.get("MaterialPlannedDeliveryDurn"),
                        "min_qty": o.get("MinimumPurchaseOrderQuantity")})
    f = sources[0]
    text = (f"Purchase info record(s) for {material} -- " +
            "; ".join(f"{s['supplier']} @ {s['price']} {s.get('currency') or ''}".strip() for s in sources))
    return _card(text, "pir", "read", material=material, number=f.get("number"), supplier=f.get("supplier"),
                 price=f.get("price"), currency=f.get("currency"), purch_org=f.get("purch_org"),
                 lead_time_days=f.get("lead_time_days"), min_qty=f.get("min_qty"), sources=sources)


# =============================================================================
# 2) BILL OF MATERIAL  (the MADE structure)   CS01
# =============================================================================
def _build_bom_header(material: str, plant: str, *, bom_usage: str = "1",
                      alternative: str = "1", base_qty: float = 1, base_unit: str = "EA",
                      valid_from: datetime.date | None = None,
                      valid_to: datetime.date | None = None) -> dict:
    """MaterialBOM HEADER only (no items). The all-in-one deep insert fails BOM/171 on this
    service (items get processed before the header is loaded), so the header is POSTed first
    to get the BOM number, then items are POSTed to MaterialBOMItem."""
    today = datetime.date.today()
    return {
        "Material": material,
        "Plant": plant,
        "BillOfMaterialCategory": "M",                  # M = material BOM
        "BillOfMaterialVariantUsage": bom_usage,        # 1 = production
        "BillOfMaterialVariant": alternative,
        "BillOfMaterialVersion": "",
        "EngineeringChangeDocument": "",
        "BillOfMaterialStatus": "1",                    # 1 = active
        # Backdate valid-FROM ~5y: MRP backward-schedules planned orders, so the BOM must be
        # valid BEFORE today or MRP can't select it ("No BOM selected", MD408).
        "HeaderValidityStartDate": _odata_date(valid_from or (today - datetime.timedelta(days=365 * 5))),
        "HeaderValidityEndDate": _odata_date(valid_to or today.replace(year=today.year + 10)),
        "BOMHeaderQuantityInBaseUnit": str(base_qty),
        "BOMHeaderBaseUnit": base_unit,
    }


def _build_bom_items(bom_no: str, material: str, plant: str, components: list[dict], *,
                     alternative: str = "1") -> list[dict]:
    """MaterialBOMItem rows for an existing BOM header. Items auto-number 0010, 0020, ...
    components: [{"component": <matnr>, "quantity"?: <n>, "unit"?: "EA", "item_category"?: "L"}]."""
    rows = []
    for i, c in enumerate(components):
        rows.append({
            "BillOfMaterial": bom_no, "BillOfMaterialCategory": "M",
            "BillOfMaterialVariant": alternative, "BillOfMaterialVersion": "",
            "Material": material, "Plant": plant,
            "BillOfMaterialItemNumber": str((i + 1) * 10).zfill(4),
            "BillOfMaterialComponent": c["component"],
            "BillOfMaterialItemQuantity": str(c.get("quantity", 1)),
            "BillOfMaterialItemUnit": c.get("unit", "EA"),
            "BillOfMaterialItemCategory": c.get("item_category", "L"),  # L = stock item
        })
    return rows


@mcp.tool()
def create_bom(material: str, plant: str, components: list[dict],
               bom_usage: str = "1", alternative: str = "1",
               base_quantity: float = 1, base_unit: str = "EA",
               confirm: bool = False) -> str:
    """Create a Bill of Material (parent material + its components) in SAP.

    This is the MADE-component structure: the parent's child materials and their
    quantities -- what MRP explodes to drive dependent demand.

    SAFETY GATE: confirm=false (default) only PREVIEWS the exact POST -- nothing
    written. Preview, get approval, then call again with confirm=true.

    Args:
        material: the parent (assembly) material, e.g. a FERT.
        plant: plant code, e.g. "1710".
        components: list of {"component": <matnr>, "quantity": <n>, "unit"?: "EA",
                    "item_category"?: "L"}. Items auto-number 0010, 0020, ...
        bom_usage: BOM usage, default "1" (production).
        alternative: alternative BOM, default "1".
        base_quantity: header base quantity, default 1.
        base_unit: header base unit, default "EA".
        confirm: must be true to actually create.
    """
    if not components:
        return "create_bom needs at least one component."
    header = _build_bom_header(material, plant, bom_usage=bom_usage, alternative=alternative,
                               base_qty=base_quantity, base_unit=base_unit)
    comp_list = [{"component": c.get("component"), "quantity": c.get("quantity", 1)} for c in components]
    if not confirm:
        sample = _build_bom_items("<assigned by SAP>", material, plant, components,
                                  alternative=alternative)
        return _card(_preview("POST", f"{BOM_SRV}/MaterialBOM", header)
                     + f"\n\nthen per component POST {BOM_SRV}/MaterialBOMItem\n"
                     + json.dumps(sample, indent=2, ensure_ascii=False),
                     "bom", "preview", material=material, plant=plant, components=comp_list)
    token = _csrf(BOM_SRV)                            # reuse one token across header + items
    ok, status, text = _post(BOM_SRV, "MaterialBOM", header, token=token)
    if not ok:
        return f"Create BOM header failed (HTTP {status}): {text}"
    num = _extract(text, "BillOfMaterial")
    added, failures = 0, []
    for row in _build_bom_items(num, material, plant, components, alternative=alternative):
        ok2, st2, tx2 = _post(BOM_SRV, "MaterialBOMItem", row, token=token)
        if ok2:
            added += 1
        else:
            failures.append(f"item {row['BillOfMaterialItemNumber']}: HTTP {st2} {tx2[:120]}")
    msg = f"Created BOM {num} for {material} @ plant {plant} with {added}/{len(components)} item(s)."
    if failures:
        msg += " Item failures: " + "; ".join(failures)
    return _card(msg, "bom", "written", material=material, plant=plant, number=num,
                 components=comp_list, items=added, of=len(components), failures=len(failures))


# ---- BOM item edit (add / remove a component on an existing BOM) -------------
_BOM_ITEM_KEY = ("BillOfMaterial", "BillOfMaterialCategory", "BillOfMaterialVariant",
                 "BillOfMaterialVersion", "BillOfMaterialItemNodeNumber",
                 "HeaderChangeDocument", "Material", "Plant")


def _get_bom(material: str, plant: str, alternative: str = "1") -> tuple | None:
    """Read a BOM alternative + its items. Returns (BillOfMaterial, [items]) or None."""
    raw = _sap_get("/sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV;v=2/MaterialBOM",
                   {"$filter": f"Material eq '{material}' and Plant eq '{plant}' "
                               f"and BillOfMaterialVariant eq '{alternative}'",
                    "$expand": "to_BillOfMaterialItem", "$top": 1})
    try:
        rows = json.loads(raw).get("d", {}).get("results", [])
    except (json.JSONDecodeError, AttributeError):
        return None
    if not rows:
        return None
    return rows[0]["BillOfMaterial"], rows[0].get("to_BillOfMaterialItem", {}).get("results", [])


@mcp.tool()
def get_bom(material: str, plant: str = "", alternative: str = "") -> str:
    """READ an existing Bill of Material -- the parent's components and quantities. Use this for
    "show me the BOM for X". The user often gives ONLY the material: plant defaults to the system
    plant, and if no alternative is given the common ones are tried. Renders as a BOM card.

    Args:
        material: the parent (assembly) material, e.g. "11066".
        plant: plant code (default = system plant, e.g. "1710").
        alternative: BOM alternative; empty = try "1", "01", "2".
    """
    plant = plant or _DEF_PLANT
    alts = [alternative] if alternative else ["1", "01", "2"]
    for alt in alts:
        raw = _sap_get(f"{BOM_PATH}/MaterialBOM",
                       {"$filter": f"Material eq '{_esc(material)}' and Plant eq '{_esc(plant)}' "
                                   f"and BillOfMaterialVariant eq '{_esc(alt)}'",
                        "$expand": "to_BillOfMaterialItem", "$top": 1})
        try:
            rows = json.loads(raw).get("d", {}).get("results", [])
        except (json.JSONDecodeError, AttributeError):
            rows = []
        if not rows:
            continue
        h = rows[0]
        items = h.get("to_BillOfMaterialItem", {}).get("results", [])
        comps = [{"component": it.get("BillOfMaterialComponent") or it.get("Material"),
                  "quantity": it.get("BillOfMaterialComponentQuantity") or it.get("BillOfMaterialItemQuantity"),
                  "unit": it.get("BillOfMaterialComponentUnit") or it.get("BillOfMaterialItemUnit"),
                  "item": it.get("BillOfMaterialItemNumber"),
                  "category": it.get("BillOfMaterialItemCategory")} for it in items]
        lines = "\n".join(f"  - {c['component']}  x{c['quantity']} {c.get('unit') or ''}".rstrip()
                          for c in comps) or "  (no items)"
        text = (f"BOM {h.get('BillOfMaterial')} for {material} @ plant {plant} "
                f"(alt {alt}, usage {h.get('BillOfMaterialVariantUsage') or h.get('BillOfMaterialUsage') or '?'}) -- "
                f"{len(comps)} component(s):\n{lines}")
        return _card(text, "bom", "read", material=material, plant=plant, number=h.get("BillOfMaterial"),
                     alternative=alt, usage=h.get("BillOfMaterialVariantUsage") or h.get("BillOfMaterialUsage"),
                     base_quantity=h.get("BillOfMaterialHeaderQuantity") or h.get("BillOfMaterialBaseQuantity"),
                     base_unit=h.get("BillOfMaterialHeaderUnit") or h.get("BillOfMaterialUnit"), components=comps)
    return _card(f"No BOM found for {material} @ plant {plant} (tried alternative {', '.join(alts)}).",
                 "bom", "read", material=material, plant=plant, components=[], number=None)


@mcp.tool()
def add_bom_component(material: str, plant: str, component: str, alternative: str = "1",
                      quantity: float = 1, unit: str = "EA", item_category: str = "L",
                      confirm: bool = False) -> str:
    """Add a component to an EXISTING BOM alternative. Finds the BOM, picks the next free
    item number, and POSTs the item -- so you just name the component.

    SAFETY GATE: confirm=false (default) only PREVIEWS.

    Args:
        material: the parent material whose BOM to edit.
        plant: plant code, e.g. "1710".
        component: the component material to add (leading zeros are stripped).
        alternative: BOM alternative/variant (default "1").
        quantity / unit / item_category: item details (defaults 1 / EA / L).
        confirm: must be true to actually write.
    """
    component = str(component).lstrip("0") or "0"
    bom = _get_bom(material, plant, alternative)
    if not bom:
        return (f"No BOM found for {material} @ plant {plant} alternative {alternative}. "
                "Create one first with create_bom.")
    bom_no, items = bom
    nextnum = str(max((int(it["BillOfMaterialItemNumber"]) for it in items), default=0) + 10).zfill(4)
    row = {"BillOfMaterial": bom_no, "BillOfMaterialCategory": "M",
           "BillOfMaterialVariant": alternative, "BillOfMaterialVersion": "",
           "Material": material, "Plant": plant, "BillOfMaterialItemNumber": nextnum,
           "BillOfMaterialComponent": component, "BillOfMaterialItemQuantity": str(quantity),
           "BillOfMaterialItemUnit": unit, "BillOfMaterialItemCategory": item_category}
    if not confirm:
        return _preview("POST", f"{BOM_SRV}/MaterialBOMItem", row)
    ok, st, tx = _post(BOM_SRV, "MaterialBOMItem", row)
    if not ok:
        return f"Add component {component} failed (HTTP {st}): {tx[:300]}"
    return (f"Added component {component} as item {nextnum} to BOM {bom_no} "
            f"({material} @ {plant}, alt {alternative}).")


@mcp.tool()
def remove_bom_component(material: str, plant: str, component: str, alternative: str = "1",
                         confirm: bool = False) -> str:
    """Remove a component from an EXISTING BOM alternative, by component number. Finds the
    item, resolves its full key (incl. the internal node number), and DELETEs it.

    SAFETY GATE: confirm=false (default) only PREVIEWS.

    Args:
        material: the parent material whose BOM to edit.
        plant: plant code, e.g. "1710".
        component: the component material to remove (leading zeros are stripped).
        alternative: BOM alternative/variant (default "1").
        confirm: must be true to actually delete.
    """
    component = str(component).lstrip("0") or "0"
    bom = _get_bom(material, plant, alternative)
    if not bom:
        return f"No BOM found for {material} @ plant {plant} alternative {alternative}."
    bom_no, items = bom
    tgt = next((it for it in items
                if str(it["BillOfMaterialComponent"]).lstrip("0") == component), None)
    if not tgt:
        present = sorted(str(it["BillOfMaterialComponent"]).lstrip("0") for it in items)
        return f"Component {component} is not in BOM {bom_no} alt {alternative}. Present: {present}."
    keys = {k: tgt[k] for k in _BOM_ITEM_KEY}
    res = change_material_view("MaterialBOMItem", keys=keys, operation="delete",
                               service="API_BILL_OF_MATERIAL_SRV", confirm=confirm)
    if confirm and "OK" in res:
        return (f"Removed component {component} (item {tgt['BillOfMaterialItemNumber']}) "
                f"from BOM {bom_no} ({material} @ {plant}, alt {alternative}).")
    return res


# =============================================================================
# 3) COST CONDITION  (price scales)   MEK1   -- two-step: header then scale lines
# =============================================================================
def _build_cost_header(material: str, supplier: str, price: float, *,
                       purchasing_org: str = _DEF_PURORG, currency: str = _DEF_CURRENCY,
                       condition_type: str = "PPR0", condition_table: str = "018",
                       valid_from: datetime.date | None = None,
                       valid_to: datetime.date | None = None) -> dict:
    """A_PurgPrcgConditionRecord header + nested validity (supplier/material mapping).
    PPR0 = purchase price with quantity scales; table 018."""
    today = datetime.date.today()
    vf = _odata_date(valid_from or today)
    vt = _odata_date(valid_to or today.replace(year=today.year + 10))
    return {
        "ConditionTable": condition_table,
        "ConditionApplication": "M",            # M = purchasing
        "ConditionType": condition_type,
        "ConditionSequentialNumber": "01",
        "ConditionRateValue": str(price),
        "ConditionRateValueUnit": currency,
        "ConditionQuantity": "1",
        "ConditionQuantityUnit": "EA",
        "ConditionValidityStartDate": vf,
        "ConditionValidityEndDate": vt,
        "to_PurgPrcgCndnRecdValidity": {"results": [{
            "ConditionValidityStartDate": vf,
            "ConditionValidityEndDate": vt,
            "ConditionApplication": "M",
            "ConditionType": condition_type,
            "Supplier": supplier,
            "Material": material,
            "PurchasingOrganization": purchasing_org,
            "PurchasingInfoRecordCategory": "0",
            "PurgDocOrderQuantityUnit": "EA",
        }]},
    }


def _build_scale_rows(condition_record: str, price_breaks: list[dict],
                      currency: str = _DEF_CURRENCY) -> list[dict]:
    """A_PurgPrcgCndnRecordScale rows -- one per quantity break."""
    rows = []
    for i, pb in enumerate(price_breaks):
        rows.append({
            "ConditionRecord": condition_record,
            "ConditionSequentialNumber": "01",
            "ConditionScaleLine": str(i + 1).zfill(4),
            "ConditionScaleQuantity": str(pb["qty"]),
            "ConditionScaleQuantityUnit": "EA",
            "ConditionRateValue": str(pb["price"]),
            "ConditionRateValueUnit": currency,
        })
    return rows


@mcp.tool()
def create_cost_condition(material: str, supplier: str, price: float,
                          purchasing_org: str = _DEF_PURORG, currency: str = _DEF_CURRENCY,
                          price_breaks: list[dict] | None = None,
                          condition_type: str = "PPR0", confirm: bool = False) -> str:
    """Create a purchasing price condition (PPR0) for a material/supplier, with
    optional quantity price-breaks (scales).

    TWO-STEP on SAP: POST the condition header (returns a ConditionRecord), then POST
    one scale line per price break. The preview shows BOTH steps.

    SAFETY GATE: confirm=false (default) only PREVIEWS -- nothing written.

    Args:
        material: material number.
        supplier: SAP supplier code.
        price: base net price (the first/qty-1 price).
        purchasing_org: default "1710".
        currency: default from .env (USD).
        price_breaks: optional list of {"qty": <n>, "price": <p>} for scale pricing,
                      e.g. [{"qty":1,"price":45.5},{"qty":10,"price":42.75}].
        condition_type: default "PPR0".
        confirm: must be true to actually create.
    """
    header = _build_cost_header(material, supplier, price,
                                purchasing_org=purchasing_org, currency=currency,
                                condition_type=condition_type)
    _cardf = dict(material=material, supplier=supplier, price=price, currency=currency)
    if not confirm:
        out = _preview("POST", f"{COND_SRV}/A_PurgPrcgConditionRecord", header)
        if price_breaks:
            sample = _build_scale_rows("<ConditionRecord from step 1>", price_breaks, currency)
            out += ("\n\nthen per price-break POST "
                    f"{COND_SRV}/A_PurgPrcgCndnRecordScale\n"
                    + json.dumps(sample, indent=2, ensure_ascii=False))
        return _card(out, "cost", "preview", scales=len(price_breaks or []), **_cardf)
    token = _csrf(COND_SRV)                      # reuse one token across the flow
    ok, status, text = _post(COND_SRV, "A_PurgPrcgConditionRecord", header, token=token)
    if not ok:
        return f"Create cost condition failed (HTTP {status}): {text}"
    cond = _extract(text, "ConditionRecord")
    if not price_breaks:
        return _card(f"Created cost condition {cond} for {material}/{supplier} (HTTP {status}).",
                     "cost", "written", number=cond, scales=0, **_cardf)
    added, failures = 0, []
    for row in _build_scale_rows(cond, price_breaks, currency):
        ok2, st2, tx2 = _post(COND_SRV, "A_PurgPrcgCndnRecordScale", row, token=token)
        if ok2:
            added += 1
        else:
            failures.append(f"line {row['ConditionScaleLine']}: HTTP {st2} {tx2[:120]}")
    msg = f"Created cost condition {cond} for {material}/{supplier} with {added} scale line(s)."
    if failures:
        msg += " Scale failures: " + "; ".join(failures)
    return _card(msg, "cost", "written", number=cond, scales=added, **_cardf)


# =============================================================================
# 4) ROUTING  (the MADE operations)   CA01   -- 3-level deep insert
# =============================================================================
def _build_routing_payload(material: str, plant: str, operations: list[dict], *,
                           description: str = "", usage: str = "1",
                           status: str = "4",
                           valid_from: datetime.date | None = None) -> dict:
    """ProductionRoutingHeader deep insert (header -> to_MatlAssgmt + to_Sequence ->
    to_Operation). Status "4" (released) is REQUIRED. Operation numbers carry NO
    leading zeros. Work centers are referenced by their INTERNAL id (CRHD), and
    ValidityStartDate is ISO (NOT the /Date()/ format BOM/cost use).

    operations: list of {"operation": "10", "text": "...",
                         "work_center_internal_id": "10000057",
                         "control_profile"?: "YBP1", "setup_time"?: 30,
                         "run_time"?: 2, "unit"?: "EA"}.
    """
    # Backdate ~5y so the task list is valid before today (MRP/production backward-scheduling).
    vf = (valid_from or (datetime.date.today() - datetime.timedelta(days=365 * 5))).strftime("%Y-%m-%dT00:00:00")
    sap_ops = []
    for op in operations:
        sap_ops.append({
            "Operation": str(op["operation"]),               # no leading zeros
            "OperationText": str(op.get("text", ""))[:40],
            "Plant": plant,
            "OperationControlProfile": op.get("control_profile", "YBP1"),
            "WorkCenterInternalID": op["work_center_internal_id"],
            "OperationReferenceQuantity": "1",
            "OperationUnit": op.get("unit", "EA"),
            "OpQtyToBaseQtyNmrtr": "1",
            "OpQtyToBaseQtyDnmntr": "1",
            "StandardWorkQuantity1": str(op.get("setup_time", 0)),
            "StandardWorkQuantityUnit1": "MIN",
            "StandardWorkQuantity2": str(op.get("run_time", 0)),
            "StandardWorkQuantityUnit2": "MIN",
        })
    return {
        "Plant": plant,
        "BillOfOperationsDesc": (description or f"Routing {material}")[:40],
        "BillOfOperationsUsage": usage,
        "BillOfOperationsStatus": status,                    # 4 = released (required)
        "BillOfOperationsUnit": "EA",
        "MinimumLotSizeQuantity": "1",
        "MaximumLotSizeQuantity": "99999999",
        "ValidityStartDate": vf,
        "to_MatlAssgmt": {"results": [{"Product": material, "Plant": plant}]},
        "to_Sequence": {"results": [{
            "SequenceCategory": "0",
            "to_Operation": {"results": sap_ops},
        }]},
    }


@mcp.tool()
def create_routing(material: str, plant: str, operations: list[dict],
                   description: str = "", confirm: bool = False) -> str:
    """Create a production Routing (the operation sequence) for a made material.

    This is the MADE-operations side: the work centers, setup/run times and order of
    operations -- what costing rolls up labour from and what capacity planning loads.

    SAFETY GATE: confirm=false (default) only PREVIEWS the exact POST -- nothing
    written. Routing is released (status 4) on create.

    Args:
        material: the made (FERT/HALB) material the routing is for.
        plant: plant code, e.g. "1710".
        operations: list of {"operation": "10", "text": "...",
                    "work_center": "PACK01" (code/name, resolved via find_work_center) OR
                    "work_center_internal_id": "<CRHD id>", "control_profile"?: "YBP1",
                    "setup_time"?: <min>, "run_time"?: <min>, "unit"?: "EA"}.
                    Operation numbers carry NO leading zeros. Prefer the human work-center
                    code (e.g. "PACK01"); the tool resolves it to the internal id.
        description: routing description (<=40 chars).
        confirm: must be true to actually create.
    """
    if not operations:
        return "create_routing needs at least one operation."
    resolved = []
    for i, o in enumerate(operations):
        o = dict(o)
        iid = o.get("work_center_internal_id")
        if not iid and o.get("work_center"):
            iid, cands = _resolve_work_center(o["work_center"], plant)
            if not iid:
                names = [c.get("work_center") or c.get("desc") for c in
                         (cands or _load_work_centers().get(str(plant), []))]
                return (f"operation {i}: couldn't resolve work center "
                        f"{o['work_center']!r} at plant {plant}. Known: {names}. "
                        "Use find_work_center to pick one.")
        if not iid:
            return (f"operation {i} needs a work_center (e.g. 'PACK01') or "
                    "work_center_internal_id. Use find_work_center to resolve a name.")
        o["work_center_internal_id"] = iid
        resolved.append(o)
    payload = _build_routing_payload(material, plant, resolved, description=description)
    ops_card = [{"operation": o.get("operation"), "text": o.get("text"),
                 "work_center": o.get("work_center")} for o in operations]
    if not confirm:
        return _card(_preview("POST", f"{ROUTING_SRV}/ProductionRoutingHeader", payload),
                     "routing", "preview", material=material, plant=plant, operations=ops_card)
    ok, status, text = _post(ROUTING_SRV, "ProductionRoutingHeader", payload)
    if not ok:
        return f"Create routing failed (HTTP {status}): {text}"
    grp = _extract(text, "ProductionRoutingGroup")
    return _card(f"Created routing group {grp} for {material} @ plant {plant} with "
                 f"{len(operations)} operation(s), released (HTTP {status}).",
                 "routing", "written", material=material, plant=plant, number=grp, operations=ops_card)


def _routing_rows(entity: str, filt: str) -> list:
    raw = _sap_get(f"{ROUTING_PATH}/{entity}", {"$filter": filt, "$top": 50})
    try:
        return json.loads(raw).get("d", {}).get("results", [])
    except (json.JSONDecodeError, AttributeError):
        return []


@mcp.tool()
def get_routing(material: str, plant: str = "") -> str:
    """READ the production Routing (operation sequence) assigned to a MATERIAL. Use for "show the
    routing for X". The user usually gives ONLY the material -- plant defaults to the system plant.
    Renders as a routing card. (Best-effort: the routing read API is navigated material -> group ->
    operations; if your system uses different entity names it returns 'not found' rather than error.)

    Args:
        material: the made material, e.g. "11219".
        plant: plant code (default = system plant).
    """
    plant = plant or _DEF_PLANT
    asg = (_routing_rows("ProductionRoutingMatlAssgmt", f"Product eq '{_esc(material)}' and Plant eq '{_esc(plant)}'")
           or _routing_rows("ProductionRtgMatlAssgmt", f"Material eq '{_esc(material)}' and Plant eq '{_esc(plant)}'"))
    if not asg:
        return _card(f"No routing found for {material} @ plant {plant}.",
                     "routing", "read", material=material, plant=plant, operations=[], number=None)
    grp = asg[0].get("ProductionRoutingGroup")
    ctr = asg[0].get("ProductionRouting")            # the group COUNTER (field name is ProductionRouting)
    op_filter = f"ProductionRoutingGroup eq '{_esc(grp)}'" + (f" and ProductionRouting eq '{_esc(ctr)}'" if ctr else "")

    def _nz(v):                                       # drop 0.000 / blank standard values
        try:
            return v if v not in (None, "") and float(v) > 0 else None
        except (TypeError, ValueError):
            return v or None
    wc_map = {}                                       # internal id -> human work-center code
    for wcs in (_load_work_centers() or {}).values():
        for w in wcs:
            if w.get("internal_id"):
                wc_map[str(w["internal_id"])] = w.get("work_center") or w["internal_id"]
    ops = []
    for o in _routing_rows("ProductionRoutingOperation", op_filter):
        wcid = o.get("WorkCenterInternalID")
        ops.append({"operation": o.get("Operation"), "text": o.get("OperationText"),
                    "work_center": wc_map.get(str(wcid), wcid), "control": o.get("OperationControlProfile"),
                    "setup": _nz(o.get("StandardWorkQuantity1")), "run": _nz(o.get("StandardWorkQuantity2")),
                    "unit": o.get("StandardWorkQuantityUnit2") or o.get("OperationUnit")})
    ops.sort(key=lambda x: int(x["operation"]) if str(x.get("operation")).isdigit() else 0)
    text = (f"Routing group {grp} for {material} @ plant {plant} -- {len(ops)} operation(s): "
            + " -> ".join(f"{o['operation']} {o.get('text') or ''}".strip() for o in ops))
    return _card(text, "routing", "read", material=material, plant=plant, number=grp, counter=ctr, operations=ops)


# =============================================================================
# generic CHANGE (PATCH/POST/DELETE a child/keyed row on any make service)
# =============================================================================
@mcp.tool()
def change_make_object(service: str, entity: str, keys: dict, fields: dict | None = None,
                       operation: str = "update", confirm: bool = False) -> str:
    """Change a make object: PATCH/extend/delete a keyed row on one of the make services.

    Mirrors sap.py's change_material_view but across the four make services. OData v2
    has no deep update, so each row is addressed by its OWN full key.
        operation="update" -> PATCH the keyed row (e.g. a PIR's net price, a BOM item qty).
        operation="add"    -> POST a new child row.
        operation="delete" -> DELETE the keyed row.

    SAFETY GATE: confirm=false (default) only PREVIEWS -- nothing written.

    Args:
        service: one of "inforecord", "cost", "bom", "routing".
        entity: the entity set to address, e.g. "A_PurgInfoRecdOrgPlantData",
                "A_BillOfMaterialItem", "A_PurgPrcgCndnRecordScale".
        keys: the FULL key of the row as {field: value}.
        fields: {ExactODataField: new_value}; for "add" the non-key fields (keys merged).
        operation: "update" (default), "add", or "delete".
        confirm: must be true to actually write.
    """
    root = _SERVICES.get(service.strip().lower())
    if not root:
        return f"Unknown service '{service}'. Use one of {sorted(_SERVICES)}."
    entity = entity.strip().lstrip("/")
    op = operation.strip().lower()
    if op not in ("update", "add", "delete"):
        return "operation must be 'update', 'add', or 'delete'."

    if op == "add":
        url, body, verb = f"{root}/{entity}", {**keys, **(fields or {})}, "POST"
    else:
        keypred = ",".join(f"{k}='{_esc(v)}'" for k, v in keys.items())
        url = f"{root}/{entity}({keypred})"
        body = dict(fields or {}) if op == "update" else None
        verb = "PATCH" if op == "update" else "DELETE"

    if not confirm:
        return _preview(verb, url, body)

    token = _csrf(root)
    if not token:
        return "Could not obtain a CSRF token from SAP (write blocked)."
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json",
               "Accept": "application/json"}
    try:
        if verb == "POST":
            resp = sap_session.post(url, params=_WRITE_PARAMS,
                                    auth=(SAP_USER, SAP_PASS), headers=headers,
                                    json=body, timeout=120, verify=False)
        elif verb == "DELETE":
            resp = sap_session.delete(url, params=_WRITE_PARAMS,
                                      auth=(SAP_USER, SAP_PASS), headers=headers,
                                      timeout=120, verify=False)
        else:
            resp = sap_session.patch(url, params=_WRITE_PARAMS,
                                     auth=(SAP_USER, SAP_PASS), headers=headers,
                                     json=body, timeout=120, verify=False)
            if resp.status_code == 405:          # some releases want the MERGE tunnel
                headers["X-HTTP-Method"] = "MERGE"
                resp = sap_session.post(url, params=_WRITE_PARAMS,
                                        auth=(SAP_USER, SAP_PASS), headers=headers,
                                        json=body, timeout=120, verify=False)
        resp.raise_for_status()
        tail = f" {resp.text[:300]}" if resp.text.strip() else ""
        return f"{verb} {entity} OK (HTTP {resp.status_code})." + tail
    except requests.exceptions.RequestException as e:
        resp = getattr(e, "response", None)
        detail = resp.text[:600] if resp is not None else ""
        return f"{op} on {entity} failed: {e}\n{detail}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
