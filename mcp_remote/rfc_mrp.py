#!/usr/bin/env python3
"""
rfc_mrp.py - run single-material MRP (BAPI_MATERIAL_PLANNING) through our own
ctypes RFC channel, then commit. Mirrors the MRP leg of your ABAP test program.

It reuses the generic layer in rfc_client.py (scalar + structure imports, and
structure + table exports), so this file is only the BAPI-specific wiring.

USAGE:
    python rfc_mrp.py <MATERIAL> [PLANT]
    # or set SAP_RFC_MATNR (+ optional SAP_RFC_WERKS) in your .env

IMPORTANT: MRP only produces planned orders / purchase requisitions if the
material has demand (a sales order, PIR, ...) in that plant. If you ran your ABAP
test, pass the SAME material - it created a sales order, so MRP has something to
plan. With no demand the call still succeeds (RETURN type 'S'); MRP_STATISTIC
just reports zeros. Either way, a clean RETURN proves the BAPI call works.

Not tested in my sandbox (no SDK / SAP / network) - the new surface vs what you
already ran is RfcGetStructure (structure set/get). If anything needs a layout
tweak it will surface there. The printed signature at the top lets you confirm
the exact parameter names against your system.
"""

import ctypes
import os
import sys

from rfc_client import connect, call, describe, sdk, RFC_ERROR_INFO

PLANT_DEFAULT = "1710"

# MRP control parameters. The one that matters is MULTI_LEVEL_PLANNING:
#   blank = single-level (MD03) -> plans only the header material (FG), no components.
#   "X"   = multi-level  (MD02) -> plans the FG AND every lower BOM level.
MRP_PARAM = {
    "PROC_TYPE": "N",             # N = net change, total horizon (use "G" for full regen)
    "CREATE_PURREQ": "1",         # 1 = purchase requisitions directly (external parts -> real PRs)
    "CREATE_SCHED_LINES": "3",    # 3 = schedule lines
    "CREATE_MRP_LIST": "1",       # 1 = MRP list
    "PLANNING_MODE": "1",         # 1 = adapt planning data
    "SCHEDULING_PLDORDS": "1",    # 1 = basic dates
    "MULTI_LEVEL_PLANNING": "X",  # X = explode and plan ALL BOM levels (MD02). THE FIX.
    "PLAN_UNCHANGED_COMP": "X",   # also (re)evaluate unchanged components in the explosion
}


def main():
    material = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SAP_RFC_MATNR", "")).strip()
    plant = (sys.argv[2] if len(sys.argv) > 2 else os.environ.get("SAP_RFC_WERKS", PLANT_DEFAULT)).strip()
    if not material:
        sys.exit("Usage: python rfc_mrp.py <MATERIAL> [PLANT]   (or set SAP_RFC_MATNR in .env)")

    # numeric materials must be zero-padded to 18 (MATN1/ALPHA) for the BAPI
    matnr = material.zfill(18) if material.isdigit() else material

    conn, _keep = connect()

    print("=== Signature of BAPI_MATERIAL_PLANNING (discovered, typed) ===")
    _desc, params = describe(conn, "BAPI_MATERIAL_PLANNING")
    for pm in params:
        print(f"  {pm['direction']:<8} {pm['name']:<22} {pm['type']}")

    print(f"\n=== Running MRP for {material} / plant {plant} ===")
    out = call(
        conn, "BAPI_MATERIAL_PLANNING",
        imports={"MATERIAL_LONG": matnr, "PLANT": plant},
        struct_imports={"MRP_PLAN_PARAM": MRP_PARAM},
        read_structs=["RETURN", "MRP_STATISTIC"],
        read_table="MRP_LISTS",
    )

    ret = out.get("RETURN", {})
    print("\nRETURN:")
    print("  TYPE   :", ret.get("TYPE"))
    print("  MESSAGE:", ret.get("MESSAGE"))

    print("\nMRP_STATISTIC (non-empty fields):")
    for k, v in out.get("MRP_STATISTIC", {}).items():
        if str(v).strip():
            print(f"  {k:<26} {v}")

    lists = out.get("MRP_LISTS", [])
    print(f"\nMRP_LISTS: {len(lists)} row(s)")
    for r in lists[:20]:
        print("  ", r)

    # Persist results on the SAME connection / LUW (skip if MRP errored).
    if ret.get("TYPE") not in ("E", "A"):
        commit = call(conn, "BAPI_TRANSACTION_COMMIT",
                      imports={"WAIT": "X"}, read_structs=["RETURN"])
        ctype = commit.get("RETURN", {}).get("TYPE") or "(ok)"
        print(f"\nCOMMIT: {ctype}  ->  check MD04 for {material}")

    err = RFC_ERROR_INFO()
    sdk.RfcCloseConnection(conn, ctypes.byref(err))
    print("\nDone.")


if __name__ == "__main__":
    main()