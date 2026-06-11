#!/usr/bin/env python3
"""
sap_planning_mcp.py - an MCP server exposing SAP planning (MRP + demand) over our
self-owned ctypes RFC channel. Tools are thin typed wrappers over the validated
generic client in rfc_client.py.

The SAP NW RFC SDK dependency lives ONLY in this process. Your ADK app connects
over MCP and never imports the SDK - that is the whole point of running it apart.

------------------------------------------------------------------------------
RUN (a separate process, in this folder so it can import rfc_client + find the SDK):
    pip install "mcp[cli]"
    python sap_planning_mcp.py            # SSE on http://127.0.0.1:8001/sse
    # MCP_TRANSPORT=streamable_http ...   # newer single-endpoint transport (needs up-to-date `mcp`)
    # MCP_TRANSPORT=stdio ...             # stdio (client spawns it)

This process still needs the .env (SAP_RFC_*) and the vendored nwrfcsdk, exactly
like rfc_demand_mrp.py. The ADK app needs neither - only the URL.

NOT tested here (no SDK / SAP / network). The SAP behaviour is already proven by
rfc_demand_mrp.py; these tools just expose it over MCP. If your FastMCP version
rejects host/port on the constructor, pass them to mcp.run() instead.
------------------------------------------------------------------------------
"""

import ctypes
import os
from datetime import date, timedelta

from typing import Annotated

from pydantic import Field
from mcp.server.fastmcp import FastMCP

from rfc_client import connect, call, sdk, RFC_ERROR_INFO

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8001"))

mcp = FastMCP("sap_planning_mcp", host=HOST, port=PORT)

# Defaults mirroring the ABAP test program
SALES_ORG, DISTR_CHAN, DIVISION, ITEM = "1710", "10", "00", "000010"


# ----- shared helpers (DRY) -----
def _matnr(material: str) -> str:
    """Zero-pad numeric materials to 18 (MATN1/ALPHA) for the BAPI."""
    return material.zfill(18) if material.isdigit() else material


def _close(conn):
    err = RFC_ERROR_INFO()
    sdk.RfcCloseConnection(conn, ctypes.byref(err))


def _commit(conn):
    call(conn, "BAPI_TRANSACTION_COMMIT", imports={"WAIT": "X"}, read_structs=["RETURN"])


def _read_db_table(conn, table, fields, where):
    out = call(conn, "RFC_READ_TABLE",
               imports={"QUERY_TABLE": table, "DELIMITER": "|"},
               table_imports={"FIELDS": [{"FIELDNAME": f} for f in fields],
                              "OPTIONS": [{"TEXT": where}]},
               read_table="DATA")
    rows = []
    for r in out.get("DATA", []):
        vals = [v.strip() for v in r.get("WA", "").split("|")]
        rows.append(dict(zip(fields, vals)))
    return rows


_MATL_LABEL = {"FERT": "Finished product", "HALB": "Semi-finished",
               "HAWA": "Bought part", "ROH": "Raw material"}


def _int(v):
    try:
        return int(str(v).strip() or 0)
    except ValueError:
        return 0


def _mrp_row(r):
    """One MRP_LISTS row -> the cascade entry the UI renderer consumes."""
    proc = (r.get("PROC_TYPE") or "").strip()          # E/X in-house, F external
    typ = (r.get("MATL_TYPE") or "").strip()
    mat = (r.get("MATERIAL_LONG") or r.get("MATERIAL") or "").strip().lstrip("0") or "0"
    exception = r.get("NEW_EXCPT") == "X" or any(
        r.get(f"IND_EXCMESS_0{i}") == "X" for i in range(1, 9))
    return {"mat": mat, "type": typ, "label": _MATL_LABEL.get(typ, typ),
            "level": _int(r.get("LL_CODE")), "proc": proc,
            "output": "purchase_req" if proc == "F" else "planned_order",
            "exception": exception}


def _run_timestamp(rows):
    """Format MRP_DATE/MRP_TIME off the first list row as 'YYYY-MM-DD HH:MM'."""
    if not rows:
        return ""
    d, t = rows[0].get("MRP_DATE", ""), rows[0].get("MRP_TIME", "")
    if len(d) == 8 and len(t) >= 4:
        return f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}"
    return ""


# ----- create_demand -----
@mcp.tool(
    name="create_demand",
    annotations={"title": "Create sales-order demand in SAP",
                 "readOnlyHint": False, "destructiveHint": True,
                 "idempotentHint": False, "openWorldHint": True},
)
def create_demand(
    material: Annotated[str, Field(description="Material number, e.g. '11062'", min_length=1, max_length=40)],
    plant: Annotated[str, Field(description="Plant, e.g. '1710'")] = "1710",
    quantity: Annotated[str, Field(description="Order quantity, e.g. '100'")] = "100",
    customer: Annotated[str, Field(description="Sold-to customer, e.g. 'USCU_S03'")] = "USCU_S03",
) -> dict:
    """Create a sales order (demand) via BAPI_SALESORDER_CREATEFROMDAT2, then commit.

    Each call creates a NEW order, so demand accumulates across calls.

    Returns: {"sales_order": "<number>"} on success,
             or {"error": "<reason>", "messages": ["<bapi msg>", ...]} on failure.
    """
    material, plant = material.strip(), plant.strip()
    matnr = _matnr(material)
    req_date = (date.today() + timedelta(days=28)).strftime("%Y%m%d")
    conn, _ = connect()
    try:
        out = call(
            conn, "BAPI_SALESORDER_CREATEFROMDAT2",
            struct_imports={
                "ORDER_HEADER_IN": {"DOC_TYPE": "TA", "SALES_ORG": SALES_ORG,
                                    "DISTR_CHAN": DISTR_CHAN, "DIVISION": DIVISION,
                                    "REQ_DATE_H": req_date},
                "ORDER_HEADER_INX": {"DOC_TYPE": "X", "SALES_ORG": "X", "DISTR_CHAN": "X",
                                     "DIVISION": "X", "REQ_DATE_H": "X"},
            },
            table_imports={
                "ORDER_PARTNERS": [{"PARTN_ROLE": "AG", "PARTN_NUMB": customer}],
                "ORDER_ITEMS_IN": [{"ITM_NUMBER": ITEM, "MATERIAL": matnr,
                                    "PLANT": plant, "TARGET_QTY": quantity}],
                "ORDER_ITEMS_INX": [{"ITM_NUMBER": ITEM, "MATERIAL": "X", "PLANT": "X",
                                     "TARGET_QTY": "X"}],
                "ORDER_SCHEDULES_IN": [{"ITM_NUMBER": ITEM, "REQ_QTY": quantity,
                                        "REQ_DATE": req_date}],
                "ORDER_SCHEDULES_INX": [{"ITM_NUMBER": ITEM, "REQ_QTY": "X", "REQ_DATE": "X"}],
            },
            read_exports=["SALESDOCUMENT"],
            read_table="RETURN",
        )
        errs = [r.get("MESSAGE") for r in out.get("RETURN", []) if r.get("TYPE") in ("E", "A")]
        if errs:
            return {"error": "sales order creation failed", "messages": errs}
        _commit(conn)
        return {"sales_order": out.get("SALESDOCUMENT", "").strip()}
    finally:
        _close(conn)


# ----- run_mrp -----
@mcp.tool(
    name="run_mrp",
    annotations={"title": "Run material MRP in SAP",
                 "readOnlyHint": False, "destructiveHint": True,
                 "idempotentHint": False, "openWorldHint": True},
)
def run_mrp(
    material: Annotated[str, Field(description="Material number, e.g. '11082'", min_length=1, max_length=40)],
    plant: Annotated[str, Field(description="Plant, e.g. '1710'")] = "1710",
    multi_level: Annotated[bool, Field(
        description="Plan the whole BOM (MD02). False = header material only (MD03).")] = True,
    planning_mode: Annotated[str, Field(
        description="1=adapt (normal), 2=re-explode BOM, 3=delete & recreate. Use '3' for "
                    "demos: it wipes and rebuilds the plan every run so the full cascade is "
                    "visibly created each time.")] = "1",
) -> dict:
    """Run material MRP via BAPI_MATERIAL_PLANNING, commit, and return the planned cascade.

    multi_level=True (default) plans the finished product AND every lower BOM level
    (sub-assemblies + bought parts) -> MULTI_LEVEL_PLANNING='X' (MD02).
    multi_level=False plans only the header material (MD03).

    planning_mode='3' (delete & recreate) is the demo setting: it pairs with regenerative
    selection so every run wipes and rebuilds, and the run deltas show the full counts each
    time instead of zeros on an already-planned material. '1' (adapt) is the production default.

    Returns the exact shape the UI renderer (mrp_result_view.html) consumes:
      {
        "material", "plant", "status", "multiLevel", "timestamp", "message",
        "run": {"purchaseReqsCreated","plannedOrdersCreated",
                "plannedOrdersDeleted","errors"},
        "materials": [ {"mat","type","label","level","proc","output","exception"}, ... ]
      }
    proc E/X (in-house) -> output "planned_order"; proc F (external) -> "purchase_req".
    """
    material, plant = material.strip(), plant.strip()
    matnr = _matnr(material)
    conn, _ = connect()
    try:
        out = call(
            conn, "BAPI_MATERIAL_PLANNING",
            imports={"MATERIAL_LONG": matnr, "PLANT": plant},
            struct_imports={"MRP_PLAN_PARAM": {
                # mode 3 (delete & recreate) needs regenerative selection ("G") to actually
                # re-pick an already-planned material; net change ("N") would skip it.
                "PROC_TYPE": "G" if planning_mode == "3" else "N",
                "CREATE_PURREQ": "1",                              # purchase reqs directly
                "CREATE_SCHED_LINES": "3", "CREATE_MRP_LIST": "1",
                "PLANNING_MODE": planning_mode, "SCHEDULING_PLDORDS": "1",
                "MULTI_LEVEL_PLANNING": "X" if multi_level else "",  # MD02 vs MD03
                "PLAN_UNCHANGED_COMP": "X",
            }},
            read_structs=["RETURN", "MRP_STATISTIC"],
            read_table="MRP_LISTS",
        )
        ret = out.get("RETURN", {})
        if ret.get("TYPE") not in ("E", "A"):
            _commit(conn)

        stat = out.get("MRP_STATISTIC", {})
        rows = out.get("MRP_LISTS", [])
        return {
            "material": material, "plant": plant,
            "status": ret.get("TYPE"), "multiLevel": multi_level,
            "timestamp": _run_timestamp(rows), "message": ret.get("MESSAGE"),
            "run": {
                "purchaseReqsCreated": _int(stat.get("NO_EBAN_INS")),
                "plannedOrdersCreated": _int(stat.get("NO_PLAF_INS")),
                "plannedOrdersDeleted": _int(stat.get("NO_PLAF_DEL")),
                "errors": _int(stat.get("NO_TERMINATIONS")) + _int(stat.get("NO_SHORT_DUMPS")),
            },
            "materials": [_mrp_row(r) for r in rows],
        }
    finally:
        _close(conn)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "sse")
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport=transport)  # "sse" (default; works on older mcp) or "streamable_http" (newer mcp)