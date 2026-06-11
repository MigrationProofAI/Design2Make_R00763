#!/usr/bin/env python3
"""
sap_prodvers_mcp.py - an MCP server exposing the SAP production-version write
(ZD2M_PROD_VERS_MAINTAIN) over our self-owned ctypes RFC channel.

Same shape as sap_planning_mcp.py, but a SEPARATE process and concern: planning
stays planning (sap_planning_mcp.py, port 8001); this is the master-data / genesis-tail
write that makes a material plannable. A production version ties the material's BOM +
routing together so MRP can plan it - so call it as the LAST genesis step, after BOM and
routing exist, and BEFORE run_mrp.

The SAP NW RFC SDK dependency lives ONLY in this process. Your ADK app connects over MCP
and never imports the SDK.

RUN (a separate process, in this folder so it can import rfc_client + find the SDK):
    pip install "mcp[cli]"
    python sap_prodvers_mcp.py            # SSE on http://127.0.0.1:8002/sse
    # MCP_TRANSPORT=streamable_http ...   # newer single-endpoint transport (needs up-to-date mcp)
    # MCP_TRANSPORT=stdio ...             # stdio (client spawns it)

Needs the .env (SAP_RFC_*) and the vendored nwrfcsdk, like the other RFC processes. The SAP
behaviour is proven by rfc_prod_vers.py; this tool just exposes it over MCP. Not tested here
(no SDK / SAP / network).
"""

import ctypes
import os
from typing import Annotated

from pydantic import Field
from mcp.server.fastmcp import FastMCP

from rfc_client import connect, call, sdk, RFC_ERROR_INFO

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8002"))

mcp = FastMCP("sap_prodvers_mcp", host=HOST, port=PORT)


def _close(conn):
    err = RFC_ERROR_INFO()
    sdk.RfcCloseConnection(conn, ctypes.byref(err))


# ----- create_production_version -----
@mcp.tool(
    name="create_production_version",
    annotations={"title": "Create/maintain a production version in SAP",
                 "readOnlyHint": False, "destructiveHint": True,
                 "idempotentHint": True, "openWorldHint": True},
)
def create_production_version(
    material: Annotated[str, Field(description="Material number, plain e.g. '11116' (the FM zero-pads it)", min_length=1, max_length=40)],
    plant: Annotated[str, Field(description="Plant, e.g. '1710'")] = "1710",
    version: Annotated[str, Field(description="Production version key (VERID), e.g. '0001'")] = "0001",
    text: Annotated[str, Field(description="Version description (TEXT1)")] = "D2M production version",
    valid_from: Annotated[str, Field(description="Valid-from YYYYMMDD; blank = today (FM default)")] = "",
    valid_to: Annotated[str, Field(description="Valid-to YYYYMMDD; blank = 99991231 (FM default)")] = "",
    bom_usage: Annotated[str, Field(description="BOM usage STLAN; default '1' (production) - genesis makes a production BOM")] = "1",
    bom_alt: Annotated[str, Field(description="BOM alternative STLAL; default '01' (the one genesis creates)")] = "01",
    routing_type: Annotated[str, Field(description="Task-list type PLNTY, e.g. 'N'; blank = let SAP resolve")] = "",
    routing_group: Annotated[str, Field(description="Routing group PLNNR, e.g. '50000217'; blank = let SAP resolve")] = "",
    routing_counter: Annotated[str, Field(description="Routing counter PLNAL, e.g. '1'; blank = let SAP resolve")] = "",
    testrun: Annotated[bool, Field(description="Validate only, no commit. Per the confirm gate: testrun=True first, then testrun=False to write.")] = False,
    extra_fields: Annotated[dict, Field(description="Any other MKAL fields verbatim, e.g. {'BSTMI':'0','BSTMA':'99999999'}")] = {},
) -> dict:
    """Create or maintain a production version (MKAL) via ZD2M_PROD_VERS_MAINTAIN.

    Call as the LAST genesis step - after the BOM and routing exist - and BEFORE run_mrp;
    without a version, multi-level planning has nothing to explode against.

    Confirm gate (non-negotiable for writes): call once with testrun=True to validate,
    surface the result, get confirmation, then call again with testrun=False to commit.
    The ABAP wrapper commits internally on a real run, so there is no separate commit step.

    The wrapper normalizes MATNR (pass plain '11116') and defaults the validity dates, so
    only material/plant/version/text are truly required.

    Returns:
      {"success": bool, "testrun": bool, "production_version": "<verid>",
       "material", "plant", "message",
       "messages": [{"type","message"}, ...]}    # full BAPIRET2 log from ET_RETURN
    """
    material, plant = material.strip(), plant.strip()
    mkal = {"MATNR": material, "WERKS": plant, "VERID": version, "TEXT1": text}
    if valid_from:      mkal["ADATU"] = valid_from
    if valid_to:        mkal["BDATU"] = valid_to
    if bom_usage:       mkal["STLAN"] = bom_usage
    if bom_alt:         mkal["STLAL"] = bom_alt
    if routing_type:    mkal["PLNTY"] = routing_type
    if routing_group:   mkal["PLNNR"] = routing_group
    if routing_counter: mkal["PLNAL"] = routing_counter
    for k, v in (extra_fields or {}).items():
        mkal[k.upper()] = v

    conn, _ = connect()
    try:
        out = call(
            conn, "ZD2M_PROD_VERS_MAINTAIN",
            struct_imports={"IS_MKAL": mkal},
            imports={"IV_TESTRUN": "X" if testrun else ""},
            read_exports=["EV_SUCCESS", "EV_MESSAGE"],
            read_table="ET_RETURN",
        )
        success = out.get("EV_SUCCESS", "").strip() == "X"
        msgs = [{"type": r.get("TYPE"), "message": r.get("MESSAGE")}
                for r in out.get("ET_RETURN", [])]
        return {"success": success, "testrun": testrun,
                "production_version": version, "material": material, "plant": plant,
                "message": out.get("EV_MESSAGE", ""), "messages": msgs}
    finally:
        _close(conn)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "sse")
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport=transport)  # "sse" (default; works on older mcp) or "streamable_http" (newer mcp)