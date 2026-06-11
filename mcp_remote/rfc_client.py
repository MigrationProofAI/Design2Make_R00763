#!/usr/bin/env python3
"""
rfc_client.py - the GENERIC layer (step 2). Build/validate this AFTER rfc_smoke.py
passes, because it leans on the same primitives.

What it does today:
  * describe(func)  -> introspects the function via RfcGetFunctionDesc +
                       RfcGetParameterDescByIndex and returns the FULL typed
                       signature: every parameter's name, RFC type, direction
                       (IMPORT / EXPORT / CHANGING / TABLES) and lengths.
                       THIS is the capability you wanted - the typed interface on
                       first contact, the RFC twin of your $metadata discovery.
  * call(func, imports=..., read_exports=..., read_table=...) -> sets scalar
                       imports, invokes, reads scalar exports and one table param.

Honest scope: scalars are read/written as their STRING form (RfcSetString /
RfcGetString) - the SDK converts most types, which is plenty for discovery and
extraction. For full fidelity later you would add typed handling (RfcSetInt for
INT, typed getters for packed BCD QUAN/CURR to preserve precision, recursion for
nested STRUCTURE/TABLE fields). Those are marked TODO. Table reading here uses the
parameter's line-type handle to discover field names, then reads each field as a
string - good for flat tables; extend for nested ones.

Connection + SAP_UC helpers + RFC_ERROR_INFO are duplicated from rfc_smoke.py so
this file runs standalone. Same env vars, same loader-path setup.
"""

import ctypes
import os
import sys

# Optional: read a .env file if python-dotenv is installed (pip install python-dotenv).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass

# On Windows (3.8+), make the SDK DLLs in ./nwrfcsdk/lib discoverable without PATH.
_SDK_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nwrfcsdk", "lib")
if sys.platform == "win32" and hasattr(os, "add_dll_directory") and os.path.isdir(_SDK_LIB):
    os.add_dll_directory(_SDK_LIB)

# --- load SDK + SAP_UC helpers (same as rfc_smoke.py) ---------------------------
def load_sdk():
    libname = {"linux": "libsapnwrfc.so", "darwin": "libsapnwrfc.dylib",
               "win32": "sapnwrfc.dll"}.get(sys.platform, "libsapnwrfc.so")
    try:
        return ctypes.CDLL(libname)
    except OSError as e:
        sys.exit(f"Could not load {libname}: {e}\nIs the SDK lib folder on your loader path?")


sdk = load_sdk()
SAP_UC = ctypes.c_uint16


def uc(s: str):
    b = s.encode("utf-16-le") + b"\x00\x00"
    return ctypes.create_string_buffer(b, len(b))


def uc_out(n_chars: int):
    return (SAP_UC * (n_chars + 1))()


def from_uc(buf) -> str:
    return bytes(buf).decode("utf-16-le", errors="ignore").split("\x00", 1)[0]


def from_uc_field(arr) -> str:
    # decode a fixed SAP_UC[n] field up to first NUL
    return bytes(arr).decode("utf-16-le", errors="ignore").split("\x00", 1)[0]


def p(buf):
    return ctypes.cast(buf, ctypes.c_void_p)


# --- structs --------------------------------------------------------------------
class RFC_CONNECTION_PARAMETER(ctypes.Structure):
    _fields_ = [("name", ctypes.c_void_p), ("value", ctypes.c_void_p)]


class RFC_ERROR_INFO(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_int), ("group", ctypes.c_int),
        ("key", SAP_UC * 128), ("message", SAP_UC * 512),
        ("abapMsgClass", SAP_UC * 21), ("abapMsgType", SAP_UC * 2),
        ("abapMsgNumber", SAP_UC * 4),
        ("abapMsgV1", SAP_UC * 51), ("abapMsgV2", SAP_UC * 51),
        ("abapMsgV3", SAP_UC * 51), ("abapMsgV4", SAP_UC * 51),
        ("_pad", SAP_UC * 64),
    ]


class RFC_PARAMETER_DESC(ctypes.Structure):
    # Only the LEADING fields need to be exact (that's what we read). The tail
    # (defaultValue/text/optional/extendedDescription) is over-allocated as _pad so
    # RfcGetParameterDescByIndex can't overflow. Verify the head vs your sapnwrfc.h.
    _fields_ = [
        ("name", SAP_UC * 31),
        ("type", ctypes.c_int),        # RFCTYPE
        ("direction", ctypes.c_int),   # RFC_DIRECTION
        ("nucLength", ctypes.c_uint),
        ("ucLength", ctypes.c_uint),
        ("decimals", ctypes.c_uint),
        ("typeDescHandle", ctypes.c_void_p),
        ("_pad", ctypes.c_char * 256),
    ]


class RFC_FIELD_DESC(ctypes.Structure):
    _fields_ = [
        ("name", SAP_UC * 31),
        ("type", ctypes.c_int),
        ("nucLength", ctypes.c_uint),
        ("nucOffset", ctypes.c_uint),
        ("ucLength", ctypes.c_uint),
        ("ucOffset", ctypes.c_uint),
        ("decimals", ctypes.c_uint),
        ("typeDescHandle", ctypes.c_void_p),
        ("_pad", ctypes.c_char * 256),
    ]


RFC_OK = 0
P_ERR = ctypes.POINTER(RFC_ERROR_INFO)

RFCTYPE = {0: "CHAR", 1: "DATE", 2: "BCD", 3: "TIME", 4: "BYTE", 5: "TABLE",
           6: "NUM", 7: "FLOAT", 8: "INT", 9: "INT2", 10: "INT1",
           17: "STRUCTURE", 23: "DECF16", 24: "DECF34", 29: "STRING", 30: "XSTRING"}
DIRECTION = {1: "IMPORT", 2: "EXPORT", 3: "CHANGING", 7: "TABLES"}
RFCTYPE_STRUCTURE, RFCTYPE_TABLE = 17, 5

# --- prototypes -----------------------------------------------------------------
for fn, res, args in [
    ("RfcOpenConnection", ctypes.c_void_p, [ctypes.POINTER(RFC_CONNECTION_PARAMETER), ctypes.c_uint, P_ERR]),
    ("RfcCloseConnection", ctypes.c_int, [ctypes.c_void_p, P_ERR]),
    ("RfcGetFunctionDesc", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p, P_ERR]),
    ("RfcCreateFunction", ctypes.c_void_p, [ctypes.c_void_p, P_ERR]),
    ("RfcDestroyFunction", ctypes.c_int, [ctypes.c_void_p, P_ERR]),
    ("RfcInvoke", ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, P_ERR]),
    ("RfcGetParameterCount", ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), P_ERR]),
    ("RfcGetParameterDescByIndex", ctypes.c_int, [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(RFC_PARAMETER_DESC), P_ERR]),
    ("RfcSetString", ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, P_ERR]),
    ("RfcSetInt", ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, P_ERR]),
    ("RfcGetString", ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), P_ERR]),
    ("RfcGetTable", ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), P_ERR]),
    ("RfcGetStructure", ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), P_ERR]),
    ("RfcGetRowCount", ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), P_ERR]),
    ("RfcMoveToFirstRow", ctypes.c_int, [ctypes.c_void_p, P_ERR]),
    ("RfcMoveToNextRow", ctypes.c_int, [ctypes.c_void_p, P_ERR]),
    ("RfcGetCurrentRow", ctypes.c_void_p, [ctypes.c_void_p, P_ERR]),
    ("RfcAppendNewRow", ctypes.c_void_p, [ctypes.c_void_p, P_ERR]),
    ("RfcGetFieldCount", ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), P_ERR]),
    ("RfcGetFieldDescByIndex", ctypes.c_int, [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(RFC_FIELD_DESC), P_ERR]),
]:
    f = getattr(sdk, fn)
    f.restype, f.argtypes = res, args


def check(rc, err, what):
    if rc != RFC_OK:
        raise RuntimeError(f"{what} failed (rc={rc}): [{from_uc(err.key)}] {from_uc(err.message)}")


def need(handle, err, what):
    if not handle:
        raise RuntimeError(f"{what} returned NULL: [{from_uc(err.key)}] {from_uc(err.message)}")
    return handle


# --- connection -----------------------------------------------------------------
def connect():
    missing = [n for n in ("SAP_RFC_HOST", "SAP_RFC_CLIENT", "SAP_RFC_USER", "SAP_RFC_PASSWORD")
               if not os.environ.get(n)]
    if missing:
        sys.exit("Missing required values (add them to your .env): " + ", ".join(missing))
    cfg = {"ashost": os.environ["SAP_RFC_HOST"], "sysnr": os.environ.get("SAP_RFC_SYSNR", "00"),
           "client": os.environ["SAP_RFC_CLIENT"], "user": os.environ["SAP_RFC_USER"],
           "passwd": os.environ["SAP_RFC_PASSWORD"], "lang": os.environ.get("SAP_RFC_LANG", "EN")}
    arr = (RFC_CONNECTION_PARAMETER * len(cfg))()
    keep = []
    for i, (k, v) in enumerate(cfg.items()):
        kb, vb = uc(k), uc(v)
        keep.append((kb, vb))
        arr[i].name = ctypes.cast(kb, ctypes.c_void_p)
        arr[i].value = ctypes.cast(vb, ctypes.c_void_p)
    err = RFC_ERROR_INFO()
    conn = sdk.RfcOpenConnection(arr, len(cfg), ctypes.byref(err))
    return need(conn, err, "RfcOpenConnection"), keep


# --- introspection: the headline ------------------------------------------------
def describe(conn, func_name):
    """Return the full typed signature of a function module."""
    err = RFC_ERROR_INFO()
    desc = need(sdk.RfcGetFunctionDesc(conn, p(uc(func_name)), ctypes.byref(err)),
                err, f"RfcGetFunctionDesc({func_name})")
    n = ctypes.c_uint(0)
    check(sdk.RfcGetParameterCount(desc, ctypes.byref(n), ctypes.byref(err)), err, "RfcGetParameterCount")
    params = []
    for i in range(n.value):
        pd = RFC_PARAMETER_DESC()
        check(sdk.RfcGetParameterDescByIndex(desc, i, ctypes.byref(pd), ctypes.byref(err)),
              err, f"RfcGetParameterDescByIndex({i})")
        params.append({
            "name": from_uc_field(pd.name),
            "type": RFCTYPE.get(pd.type, pd.type),
            "direction": DIRECTION.get(pd.direction, pd.direction),
            "ucLength": pd.ucLength,
            "decimals": pd.decimals,
            "type_handle": pd.typeDescHandle,
        })
    return desc, params


def _table_fields(type_handle):
    """Field names of a table/structure line type via field introspection."""
    err = RFC_ERROR_INFO()
    n = ctypes.c_uint(0)
    check(sdk.RfcGetFieldCount(type_handle, ctypes.byref(n), ctypes.byref(err)), err, "RfcGetFieldCount")
    out = []
    for i in range(n.value):
        fd = RFC_FIELD_DESC()
        check(sdk.RfcGetFieldDescByIndex(type_handle, i, ctypes.byref(fd), ctypes.byref(err)),
              err, f"RfcGetFieldDescByIndex({i})")
        out.append(from_uc_field(fd.name))
    return out


def _read_struct(struct_handle, field_names, err):
    """Read every field of a structure handle into a dict (as strings)."""
    rec = {}
    for fname in field_names:
        buf = uc_out(2048)
        slen = ctypes.c_uint(0)
        check(sdk.RfcGetString(struct_handle, p(uc(fname)), p(buf), 2048,
                               ctypes.byref(slen), ctypes.byref(err)),
              err, f"RfcGetString(struct.{fname})")
        rec[fname] = from_uc(buf)
    return rec


# --- a pragmatic generic call ---------------------------------------------------
def call(conn, func_name, imports=None, int_imports=None, struct_imports=None,
         table_imports=None, read_exports=None, read_structs=None, read_table=None):
    """
    imports:        {PARAM: str}            scalar char/string imports
    int_imports:    {PARAM: int}            scalar INT imports (e.g. ROWCOUNT)
    struct_imports: {PARAM: {field: val}}   structure imports (one level)
    table_imports:  {PARAM: [ {field: val}, ... ]}  table imports (append rows)
    read_exports:   [PARAM, ...]            scalar exports to read back (as strings)
    read_structs:   [PARAM, ...]            structure exports to read -> dict
    read_table:     PARAM                   one table param to read -> list[dict]
    """
    imports, int_imports = imports or {}, int_imports or {}
    struct_imports, table_imports = struct_imports or {}, table_imports or {}
    read_exports, read_structs = read_exports or [], read_structs or []
    err = RFC_ERROR_INFO()
    desc, params = describe(conn, func_name)
    type_handle_map = {pm["name"]: pm["type_handle"] for pm in params}

    fh = need(sdk.RfcCreateFunction(desc, ctypes.byref(err)), err, "RfcCreateFunction")
    try:
        held = []
        for name, val in imports.items():
            vb = uc(val); held.append(vb)
            check(sdk.RfcSetString(fh, p(uc(name)), p(vb), len(val), ctypes.byref(err)),
                  err, f"RfcSetString({name})")
        for name, val in int_imports.items():
            check(sdk.RfcSetInt(fh, p(uc(name)), int(val), ctypes.byref(err)),
                  err, f"RfcSetInt({name})")
        for sname, fields in struct_imports.items():
            sh = ctypes.c_void_p()
            check(sdk.RfcGetStructure(fh, p(uc(sname)), ctypes.byref(sh), ctypes.byref(err)),
                  err, f"RfcGetStructure({sname})")
            for fname, fval in fields.items():
                s = str(fval)
                fv = uc(s); held.append(fv)
                check(sdk.RfcSetString(sh, p(uc(fname)), p(fv), len(s), ctypes.byref(err)),
                      err, f"RfcSetString({sname}.{fname})")
        for tname, rows_in in table_imports.items():
            tbl = ctypes.c_void_p()
            check(sdk.RfcGetTable(fh, p(uc(tname)), ctypes.byref(tbl), ctypes.byref(err)),
                  err, f"RfcGetTable({tname})")
            for row_dict in rows_in:
                rowh = need(sdk.RfcAppendNewRow(tbl, ctypes.byref(err)), err, f"RfcAppendNewRow({tname})")
                for fname, fval in row_dict.items():
                    s = str(fval)
                    fv = uc(s); held.append(fv)
                    check(sdk.RfcSetString(rowh, p(uc(fname)), p(fv), len(s), ctypes.byref(err)),
                          err, f"RfcSetString({tname}.{fname})")

        check(sdk.RfcInvoke(conn, fh, ctypes.byref(err)), err, f"RfcInvoke({func_name})")

        result = {}
        for name in read_exports:
            buf = uc_out(2048); slen = ctypes.c_uint(0)
            check(sdk.RfcGetString(fh, p(uc(name)), p(buf), 2048, ctypes.byref(slen), ctypes.byref(err)),
                  err, f"RfcGetString({name})")
            result[name] = from_uc(buf)

        for sname in read_structs:
            sh = ctypes.c_void_p()
            check(sdk.RfcGetStructure(fh, p(uc(sname)), ctypes.byref(sh), ctypes.byref(err)),
                  err, f"RfcGetStructure({sname})")
            result[sname] = _read_struct(sh, _table_fields(type_handle_map[sname]), err)

        if read_table:
            tbl = ctypes.c_void_p()
            check(sdk.RfcGetTable(fh, p(uc(read_table)), ctypes.byref(tbl), ctypes.byref(err)),
                  err, f"RfcGetTable({read_table})")
            rows = ctypes.c_uint(0)
            check(sdk.RfcGetRowCount(tbl, ctypes.byref(rows), ctypes.byref(err)), err, "RfcGetRowCount")
            fields = _table_fields(type_handle_map[read_table])
            data = []
            if rows.value:
                check(sdk.RfcMoveToFirstRow(tbl, ctypes.byref(err)), err, "RfcMoveToFirstRow")
                for _ in range(rows.value):
                    row = need(sdk.RfcGetCurrentRow(tbl, ctypes.byref(err)), err, "RfcGetCurrentRow")
                    rec = {}
                    for fname in fields:
                        buf = uc_out(2048); slen = ctypes.c_uint(0)
                        check(sdk.RfcGetString(row, p(uc(fname)), p(buf), 2048, ctypes.byref(slen), ctypes.byref(err)),
                              err, f"RfcGetString(row.{fname})")
                        rec[fname] = from_uc(buf)
                    data.append(rec)
                    sdk.RfcMoveToNextRow(tbl, ctypes.byref(err))  # last call returns EOF; ignore
            result[read_table] = data
        return result
    finally:
        e2 = RFC_ERROR_INFO()
        sdk.RfcDestroyFunction(fh, ctypes.byref(e2))


# --- demo -----------------------------------------------------------------------
if __name__ == "__main__":
    conn, _keep = connect()

    print("=== Signature of RFC_READ_TABLE (discovered, typed) ===")
    _desc, params = describe(conn, "RFC_READ_TABLE")
    for pm in params:
        print(f"  {pm['direction']:<8} {pm['name']:<16} {pm['type']:<10} len={pm['ucLength']}")

    print("\n=== Calling RFC_READ_TABLE on T000 (max 10 rows) ===")
    out = call(
        conn, "RFC_READ_TABLE",
        imports={"QUERY_TABLE": "T000", "DELIMITER": "|"},
        int_imports={"ROWCOUNT": 10},
        read_table="DATA",
    )
    for row in out["DATA"]:
        print("  ", row.get("WA", row))

    err = RFC_ERROR_INFO()
    sdk.RfcCloseConnection(conn, ctypes.byref(err))
    print("\nDone.")
