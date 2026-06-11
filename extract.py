"""
codebook_extract.py — DEPLOYMENT-TIME tool. Run ONCE per SAP system at onboarding.

Builds code_book.json: for the coded material fields you care about, the allowed
codes and their language texts, read straight from SAP's check/text tables.

Clean-core: it ONLY calls the standard RFC_READ_TABLE (no custom ABAP object).
  1. RFC_READ_TABLE on DD03L   -> each base table's fields + their check tables
  2. skip "check tables" that are master/transaction data (DD02L delivery class)
  3. RFC_READ_TABLE on each real check table + its text table -> codes + texts
  4. apply FIELD_BRIDGE (OData field -> DB field) -> an agent-ready section

The running agent never needs RFC: it reads the static code_book.json this produces.

Requirements / caveats
  - pip install pyrfc   (needs the SAP NetWeaver RFC SDK on the machine)
  - RFC creds come from SAP_RFC_* in .env (fall back to the SAP_* HTTP creds)
  - The RFC user must be allowed to call RFC_READ_TABLE (S_RFC) and read these
    tables (S_TABU_*). Some hardened systems restrict RFC_READ_TABLE.
  - SAP language is the 1-char code ('E'), NOT the 2-char ISO ('EN'). Set SAP_SPRAS.
  - RFC_READ_TABLE forbids the client field (MANDT) in the WHERE -- it always
    runs in your logon client -- so we never filter on it.
"""
import datetime
import json
import os
import re

from dotenv import load_dotenv

try:
    from pyrfc import Connection
except ImportError:
    raise SystemExit(
        "pyrfc not installed. Run `pip install pyrfc` "
        "(requires the SAP NetWeaver RFC SDK to be installed and on PATH)."
    )

load_dotenv()

# ----------------------------- CONFIG ---------------------------------------
# Material-master tables to scan for coded fields. Add more as you need them.
BASE_TABLES = ["MARA", "MARC", "MVKE", "MARM", "MEAN", "MBEW"]

# The small, stable bridge: the name the AGENT uses (OData/CDS field)  ->  the
# DB field that backs it. The check table is DISCOVERED from DD03L, not hardcoded.
FIELD_BRIDGE = {
    "ProductType":      "MTART",   # -> T134
    "CrossPlantStatus": "MSTAE",   # -> T141
    "ProductGroup":     "MATKL",   # -> T023
    "IndustrySector":   "MBRSH",   # -> T137
    "BaseUnit":         "MEINS",   # -> T006 (units text table is special -> override below)
}

# Special cases where the "<table>T" text-table convention is WRONG.
# value = (text_table, code_field, text_field)
TEXT_TABLE_OVERRIDES = {
    "T006": ("T006A", "MSEH3", "MSEHT"),   # units: texts live in T006A, not T006T
}

# Delivery classes to SKIP: 'A' = master/transaction data (MARA, LFA1...),
# 'L' = transactional. Real config/check tables are 'C'/'E'/'G'/'S'.
# This is what stops MATNR's "check table" MARA dragging in every material.
SKIP_DELIVERY_CLASS = {"A", "L"}

# Real coded enumerations are small. Anything bigger is a repository
# (e.g. STXFADM smartforms), not a value list -- drop it. 500 keeps countries (255).
MAX_VALUES = 500

OUT_PATH = "code_book.json"
# SAP internal language is 1-char ('E' for English), NOT the 2-char ISO 'EN'.
SPRAS = os.getenv("SAP_SPRAS", "E")

# --------------------------- CONNECTION -------------------------------------
def connect() -> Connection:
    """RFC connection params (NOT the HTTPS ones). sysnr '00' matches this appliance."""
    return Connection(
        ashost=os.getenv("SAP_RFC_HOST") or os.getenv("SAP_HOST"),
        sysnr=os.getenv("SAP_SYSNR", "00"),
        client=os.getenv("SAP_CLIENT", "100"),
        user=os.getenv("SAP_RFC_USER") or os.getenv("SAP_USER"),
        passwd=os.getenv("SAP_RFC_PASSWORD") or os.getenv("SAP_PASS"),
        lang=os.getenv("SAP_LANG", "EN"),
    )

# ----------------------- RFC_READ_TABLE helper ------------------------------
def _where_lines(where):
    """RFC_READ_TABLE OPTIONS: each line must be <= 72 characters."""
    if not where:
        return []
    out, line = [], ""
    for tok in where.split(" "):
        if len(line) + len(tok) + 1 > 72:
            out.append({"TEXT": line})
            line = tok
        else:
            line = f"{line} {tok}".strip()
    if line:
        out.append({"TEXT": line})
    return out


def read_table(conn, table, fields, where=None, rowcount=0):
    """Read `fields` from `table`; return list[dict].

    Parses by OFFSET/LENGTH with DELIMITER='' so a separator inside a text value
    can never corrupt the column split (the classic RFC_READ_TABLE trap).
    Never put MANDT in `where` -- RFC_READ_TABLE rejects the client field.
    """
    res = conn.call(
        "RFC_READ_TABLE",
        QUERY_TABLE=table,
        DELIMITER="",
        FIELDS=[{"FIELDNAME": f} for f in fields],
        OPTIONS=_where_lines(where),
        ROWCOUNT=rowcount,
    )
    cols = res["FIELDS"]
    rows = []
    for d in res["DATA"]:
        wa = d["WA"]
        rows.append({
            c["FIELDNAME"]: wa[int(c["OFFSET"]):int(c["OFFSET"]) + int(c["LENGTH"])].strip()
            for c in cols
        })
    return rows

# ------------------------------ DDIC ----------------------------------------
_VALID_TABLE = re.compile(r"^[A-Z0-9/_]+$")


def field_catalog(conn, table):
    """All real fields of `table` from DD03L (name, key flag, check table)."""
    rows = read_table(conn, "DD03L", ["FIELDNAME", "KEYFLAG", "CHECKTABLE"],
                      where=f"TABNAME = '{table}'")
    return [r for r in rows if r["FIELDNAME"] and not r["FIELDNAME"].startswith(".")]


def delivery_class(conn, table):
    """DD02L delivery class (CONTFLAG). Used to skip master/transaction tables
    (class 'A'/'L') so MATNR -> MARA etc. don't get treated as coded values."""
    rows = read_table(conn, "DD02L", ["CONTFLAG"], where=f"TABNAME = '{table}'")
    return rows[0]["CONTFLAG"] if rows else ""


def _non_client_key(cat):
    """Key fields of a table excluding MANDT/SPRAS (the code field lives here)."""
    return [r["FIELDNAME"] for r in cat
            if r["KEYFLAG"] == "X" and r["FIELDNAME"] not in ("MANDT", "SPRAS")]


def text_table_of(conn, check_table):
    """Locate a check table's text table. Honors TEXT_TABLE_OVERRIDES first, then
    the convention (CT + 'T'). Returns (table, code_field, text_field) or None.
    """
    if check_table in TEXT_TABLE_OVERRIDES:
        return TEXT_TABLE_OVERRIDES[check_table]
    tt = check_table + "T"
    cat = field_catalog(conn, tt)
    if not cat:
        return None
    if "SPRAS" not in [r["FIELDNAME"] for r in cat]:   # not a language text table
        return None
    code_keys = _non_client_key(cat)
    text_fields = [r["FIELDNAME"] for r in cat if r["KEYFLAG"] != "X"]
    if not code_keys or not text_fields:
        return None
    return tt, code_keys[-1], text_fields[0]


def values_for_check_table(conn, check_table):
    """[{code, text}] for a check table -- via its text table when one exists.
    Filters only on SPRAS (never MANDT -- RFC_READ_TABLE runs in the logon client).
    """
    found = text_table_of(conn, check_table)
    if found:
        table, code_f, text_f = found
        rows = read_table(conn, table, [code_f, text_f], where=f"SPRAS = '{SPRAS}'")
        return sorted(
            ({"code": r[code_f], "text": r[text_f]} for r in rows if r[code_f]),
            key=lambda x: x["code"],
        )
    # no text table -> codes only (augment by hand if needed)
    keys = _non_client_key(field_catalog(conn, check_table))
    if not keys:
        return []
    code_f = keys[-1]
    seen, out = set(), []
    for r in read_table(conn, check_table, [code_f]):
        c = r[code_f]
        if c and c not in seen:
            seen.add(c)
            out.append({"code": c, "text": ""})
    return sorted(out, key=lambda x: x["code"])

# ------------------------------ MAIN ----------------------------------------
def main():
    conn = connect()
    client = os.getenv("SAP_CLIENT", "100")

    # 1) discover  db_field -> check_table  across the base tables,
    #    skipping "check tables" that are really master/transaction data.
    field_to_checktable = {}
    dc_cache = {}
    for tab in BASE_TABLES:
        for r in field_catalog(conn, tab):
            ct = r["CHECKTABLE"]
            if not (ct and ct != "*" and _VALID_TABLE.match(ct)):
                continue
            dc = dc_cache.get(ct)
            if dc is None:
                dc = dc_cache[ct] = delivery_class(conn, ct)
            if dc in SKIP_DELIVERY_CLASS:          # skip MARA, LFA1, STXFADM, ...
                continue
            field_to_checktable.setdefault(r["FIELDNAME"], ct)

    # 2) read values once per unique check table; drop oversized "repositories"
    ct_values = {}
    for ct in sorted(set(field_to_checktable.values())):
        try:
            vals = values_for_check_table(conn, ct)
            ct_values[ct] = vals if len(vals) <= MAX_VALUES else []
        except Exception as e:                     # one bad table shouldn't stop the run
            ct_values[ct] = []
            print(f"  ! {ct}: {e}")

    # 3a) full catalog, keyed by DB field
    by_db_field = {
        f: {"check_table": ct, "values": ct_values.get(ct, [])}
        for f, ct in sorted(field_to_checktable.items())
    }

    # 3b) agent-ready slice, keyed by the OData field name (via the bridge)
    by_odata_field = {}
    for odata_field, db_field in FIELD_BRIDGE.items():
        ct = field_to_checktable.get(db_field)
        by_odata_field[odata_field] = {
            "db_field": db_field,
            "check_table": ct,
            "values": ct_values.get(ct, []) if ct else [],
        }

    out = {
        "_generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "_system": f"{os.getenv('SAP_RFC_HOST') or os.getenv('SAP_HOST')}/{client}",
        "_language": SPRAS,
        "by_odata_field": by_odata_field,   # <- the agent's list_allowed_values reads this
        "by_db_field": by_db_field,         # <- full discovered catalog, for reference
    }
    conn.close()

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Wrote {OUT_PATH}: {len(by_odata_field)} bridged fields, "
          f"{len(by_db_field)} coded DB fields, {len(ct_values)} check tables.")


if __name__ == "__main__":
    main()