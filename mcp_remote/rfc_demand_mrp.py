#!/usr/bin/env python3
"""
rfc_demand_mrp.py - the full Z_D2M_TEST_MRP_BAPI flow through our ctypes RFC channel:

    STEP 1  create a sales order (demand)  -> BAPI_SALESORDER_CREATEFROMDAT2 + commit
    STEP 2  run MRP against it             -> BAPI_MATERIAL_PLANNING + commit
    STEP 3  show MRP statistics and lists

Uses the now-complete generic layer in rfc_client.py: scalar / structure / TABLE
imports, and scalar / structure / table exports.

USAGE:
    python rfc_demand_mrp.py <MATERIAL> [PLANT] [QTY]

Defaults mirror your ABAP selection screen: plant 1710, qty 100, customer
USCU_S03, sales area 1710 / 10 / 00, requested delivery date = today + 28 days.

NOT tested in my sandbox. The new surface vs rfc_mrp.py is the table WRITES in
STEP 1 (RfcAppendNewRow). If something fails it is most likely there, or a
sales-order field your system wants that this minimal set omits - but the field
set is copied straight from your working ABAP, so it should match. If your
material is longer than 18 chars, switch the item's MATERIAL field to
MATERIAL_LONG (and the INX flag likewise).
"""

import ctypes
import os
import sys
from datetime import date, timedelta

from rfc_client import connect, call, sdk, RFC_ERROR_INFO

# Defaults from the ABAP selection screen
PLANT = "1710"
QTY = "100"
CUSTOMER = "USCU_S03"
SALES_ORG = "1710"
DISTR_CHAN = "10"
DIVISION = "00"
ITEM = "000010"


def commit(conn):
    call(conn, "BAPI_TRANSACTION_COMMIT", imports={"WAIT": "X"}, read_structs=["RETURN"])


def read_db_table(conn, table, fields, where):
    """Read selected fields of a DB table via RFC_READ_TABLE with a WHERE filter.
    Uses table writes (FIELDS + OPTIONS), so it also confirms RfcAppendNewRow."""
    out = call(
        conn, "RFC_READ_TABLE",
        imports={"QUERY_TABLE": table, "DELIMITER": "|"},
        table_imports={
            "FIELDS": [{"FIELDNAME": f} for f in fields],
            "OPTIONS": [{"TEXT": where}],
        },
        read_table="DATA",
    )
    rows = []
    for r in out.get("DATA", []):
        vals = [v.strip() for v in r.get("WA", "").split("|")]
        rows.append(dict(zip(fields, vals)))
    return rows


def create_sales_order(conn, material, plant, qty, req_date):
    out = call(
        conn, "BAPI_SALESORDER_CREATEFROMDAT2",
        struct_imports={
            "ORDER_HEADER_IN": {"DOC_TYPE": "TA", "SALES_ORG": SALES_ORG,
                                "DISTR_CHAN": DISTR_CHAN, "DIVISION": DIVISION,
                                "REQ_DATE_H": req_date},
            "ORDER_HEADER_INX": {"DOC_TYPE": "X", "SALES_ORG": "X",
                                 "DISTR_CHAN": "X", "DIVISION": "X", "REQ_DATE_H": "X"},
        },
        table_imports={
            "ORDER_PARTNERS":     [{"PARTN_ROLE": "AG", "PARTN_NUMB": CUSTOMER}],
            "ORDER_ITEMS_IN":     [{"ITM_NUMBER": ITEM, "MATERIAL": material,
                                    "PLANT": plant, "TARGET_QTY": qty}],
            "ORDER_ITEMS_INX":    [{"ITM_NUMBER": ITEM, "MATERIAL": "X",
                                    "PLANT": "X", "TARGET_QTY": "X"}],
            "ORDER_SCHEDULES_IN": [{"ITM_NUMBER": ITEM, "REQ_QTY": qty,
                                    "REQ_DATE": req_date}],
            "ORDER_SCHEDULES_INX": [{"ITM_NUMBER": ITEM, "REQ_QTY": "X",
                                     "REQ_DATE": "X"}],
        },
        read_exports=["SALESDOCUMENT"],
        read_table="RETURN",
    )
    errors = [r for r in out.get("RETURN", []) if r.get("TYPE") in ("E", "A")]
    if errors:
        for e in errors:
            print("  SO ERROR:", e.get("MESSAGE"))
        return None
    commit(conn)
    return out.get("SALESDOCUMENT", "").strip()


def run_mrp(conn, material, plant):
    out = call(
        conn, "BAPI_MATERIAL_PLANNING",
        imports={"MATERIAL_LONG": material, "PLANT": plant},
        struct_imports={"MRP_PLAN_PARAM": {
            "CREATE_PURREQ": "2", "CREATE_SCHED_LINES": "3", "CREATE_MRP_LIST": "1",
            "PLANNING_MODE": "1", "SCHEDULING_PLDORDS": "1", "PLAN_UNCHANGED_COMP": "X",
        }},
        read_structs=["RETURN", "MRP_STATISTIC"],
        read_table="MRP_LISTS",
    )
    if out.get("RETURN", {}).get("TYPE") not in ("E", "A"):
        commit(conn)
    return out


def main():
    material = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SAP_RFC_MATNR", "")).strip()
    plant = (sys.argv[2] if len(sys.argv) > 2 else PLANT).strip()
    qty = (sys.argv[3] if len(sys.argv) > 3 else QTY).strip()
    if not material:
        sys.exit("Usage: python rfc_demand_mrp.py <MATERIAL> [PLANT] [QTY]")

    # SAP stores numeric material numbers zero-padded to 18 (the MATN1 / ALPHA
    # conversion). Your ABAP selection screen applied that for you; from an
    # external RFC call we must do it ourselves, or the BAPI's sales-view lookup
    # fails with "material not defined for sales org" - exactly what you saw.
    matnr = material.zfill(18) if material.isdigit() else material

    req_date = (date.today() + timedelta(days=28)).strftime("%Y%m%d")
    conn, _keep = connect()

    print(f"STEP 1: Creating sales order  {material} (internal {matnr}) x {qty} @ {plant}  (req {req_date})")
    vbeln = create_sales_order(conn, matnr, plant, qty, req_date)
    if not vbeln:
        sys.exit("Sales order failed - see errors above.")
    print(f"  Sales order created: {vbeln}")

    print(f"\nSTEP 2: Running MRP  {material} @ {plant}")
    out = run_mrp(conn, matnr, plant)
    ret = out.get("RETURN", {})
    print("  RETURN:", ret.get("TYPE"), "-", ret.get("MESSAGE"))

    print("\nSTEP 3: MRP statistics (non-empty fields)")
    for k, v in out.get("MRP_STATISTIC", {}).items():
        if str(v).strip():
            print(f"  {k:<26} {v}")
    print(f"  MRP_LISTS: {len(out.get('MRP_LISTS', []))} row(s)")

    print("\n  Planned orders (PLAF):")
    plaf = read_db_table(conn, "PLAF", ["PLNUM", "GSMNG", "MEINS", "PSTTR", "PEDTR"],
                         f"MATNR = '{matnr}' AND PLWRK = '{plant}'")
    if not plaf:
        print("    (none)")
    for r in plaf:
        print("   ", r)

    print("\n  Purchase requisitions (EBAN):")
    eban = read_db_table(conn, "EBAN", ["BANFN", "BNFPO", "MENGE", "MEINS", "LFDAT"],
                         f"MATNR = '{matnr}' AND WERKS = '{plant}'")
    if not eban:
        print("    (none)")
    for r in eban:
        print("   ", r)

    err = RFC_ERROR_INFO()
    sdk.RfcCloseConnection(conn, ctypes.byref(err))
    print(f"\nDone. Check MD04 for {material}.")


if __name__ == "__main__":
    main()
