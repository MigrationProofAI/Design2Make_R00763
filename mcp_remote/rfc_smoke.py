#!/usr/bin/env python3
"""
rfc_smoke.py - minimal proof that we can call an SAP RFC from Python via ctypes,
talking DIRECTLY to the SAP NW RFC SDK (no PyRFC).

It calls STFC_CONNECTION, the canonical RFC "hello world": send a string, get it
echoed back plus the target system's own response text. If this prints your echo
and the system info, the whole chain works:  Python -> ctypes -> NW RFC SDK -> SAP.

It exercises exactly the primitives the generic client will also depend on
(connect, set a string import, invoke, read string exports), so validating THIS
tonight de-risks everything that comes after.

----------------------------------------------------------------------------------
RUN IT (at home):
  1. Unpack the SAP NW RFC SDK somewhere, e.g.  ./nwrfcsdk   (folder with lib/ include/)
  2. Point the dynamic loader at its lib folder:
        Linux:   export LD_LIBRARY_PATH=$PWD/nwrfcsdk/lib:$LD_LIBRARY_PATH
        macOS:   export DYLD_LIBRARY_PATH=$PWD/nwrfcsdk/lib:$DYLD_LIBRARY_PATH
        Windows: handled automatically (the script adds nwrfcsdk/lib to the DLL path)
  3. Logon details for your CAL image (user needs S_RFC authorization):
        export SAP_ASHOST=...     # app server host / ip
        export SAP_SYSNR=00       # system number
        export SAP_CLIENT=100
        export SAP_USER=...
        export SAP_PASSWD=...
        export SAP_LANG=EN
  4. python3 rfc_smoke.py

VERIFY: the RFC_ERROR_INFO field sizes below match YOUR sapnwrfc.h. The _pad makes a
small mismatch safe; a large one would still want fixing.
----------------------------------------------------------------------------------
"""

import ctypes
import os
import sys

# Optional: read a .env file if python-dotenv is installed (pip install python-dotenv).
# Harmless if it isn't - then `source` your .env or `export` the vars instead.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ModuleNotFoundError:
    pass

# On Windows (3.8+), make the SDK DLLs in ./nwrfcsdk/lib discoverable without PATH.
_SDK_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nwrfcsdk", "lib")
if sys.platform == "win32" and hasattr(os, "add_dll_directory") and os.path.isdir(_SDK_LIB):
    os.add_dll_directory(_SDK_LIB)

# --- load the SDK shared library -------------------------------------------------
def load_sdk():
    libname = {
        "linux": "libsapnwrfc.so",
        "darwin": "libsapnwrfc.dylib",
        "win32": "sapnwrfc.dll",
    }.get(sys.platform, "libsapnwrfc.so")
    try:
        return ctypes.CDLL(libname)
    except OSError as e:
        sys.exit(
            f"Could not load {libname}: {e}\n"
            f"Is the SDK lib folder on your loader path "
            f"(LD_LIBRARY_PATH / DYLD_LIBRARY_PATH / PATH)?"
        )


sdk = load_sdk()

# --- SAP_UC (2-byte Unicode) helpers --------------------------------------------
# The SDK uses 2-byte Unicode (SAP_UC). Python's c_wchar_p is 4-byte on Linux, so
# we never use it; we encode/decode UTF-16LE by hand and pass raw buffers.
SAP_UC = ctypes.c_uint16


def uc(s: str):
    """Python str -> null-terminated UTF-16LE buffer, usable as const SAP_UC*."""
    b = s.encode("utf-16-le") + b"\x00\x00"
    return ctypes.create_string_buffer(b, len(b))


def uc_out(n_chars: int):
    """Allocate an output SAP_UC buffer of n_chars characters (+1 for NUL)."""
    return (SAP_UC * (n_chars + 1))()


def from_uc(buf) -> str:
    """Decode a SAP_UC buffer back to a python str (trim at first NUL)."""
    return bytes(buf).decode("utf-16-le", errors="ignore").split("\x00", 1)[0]


def p(buf):
    """Address of a buffer as a void pointer (keeps source alive via cast)."""
    return ctypes.cast(buf, ctypes.c_void_p)


# --- structs --------------------------------------------------------------------
class RFC_CONNECTION_PARAMETER(ctypes.Structure):
    _fields_ = [("name", ctypes.c_void_p), ("value", ctypes.c_void_p)]


class RFC_ERROR_INFO(ctypes.Structure):
    # Verify sizes against your sapnwrfc.h. _pad keeps a small mismatch safe.
    _fields_ = [
        ("code", ctypes.c_int),
        ("group", ctypes.c_int),
        ("key", SAP_UC * 128),
        ("message", SAP_UC * 512),
        ("abapMsgClass", SAP_UC * 21),
        ("abapMsgType", SAP_UC * 2),
        ("abapMsgNumber", SAP_UC * 4),
        ("abapMsgV1", SAP_UC * 51),
        ("abapMsgV2", SAP_UC * 51),
        ("abapMsgV3", SAP_UC * 51),
        ("abapMsgV4", SAP_UC * 51),
        ("_pad", SAP_UC * 64),
    ]


RFC_OK = 0
P_ERR = ctypes.POINTER(RFC_ERROR_INFO)

# --- function prototypes --------------------------------------------------------
sdk.RfcOpenConnection.restype = ctypes.c_void_p
sdk.RfcOpenConnection.argtypes = [ctypes.POINTER(RFC_CONNECTION_PARAMETER), ctypes.c_uint, P_ERR]

sdk.RfcGetFunctionDesc.restype = ctypes.c_void_p
sdk.RfcGetFunctionDesc.argtypes = [ctypes.c_void_p, ctypes.c_void_p, P_ERR]

sdk.RfcCreateFunction.restype = ctypes.c_void_p
sdk.RfcCreateFunction.argtypes = [ctypes.c_void_p, P_ERR]

sdk.RfcSetString.restype = ctypes.c_int
sdk.RfcSetString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, P_ERR]

sdk.RfcGetString.restype = ctypes.c_int
sdk.RfcGetString.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), P_ERR,
]

sdk.RfcInvoke.restype = ctypes.c_int
sdk.RfcInvoke.argtypes = [ctypes.c_void_p, ctypes.c_void_p, P_ERR]

sdk.RfcDestroyFunction.restype = ctypes.c_int
sdk.RfcDestroyFunction.argtypes = [ctypes.c_void_p, P_ERR]

sdk.RfcCloseConnection.restype = ctypes.c_int
sdk.RfcCloseConnection.argtypes = [ctypes.c_void_p, P_ERR]


# --- error helpers --------------------------------------------------------------
def check(rc, err, what):
    if rc != RFC_OK:
        raise RuntimeError(f"{what} failed (rc={rc}): [{from_uc(err.key)}] {from_uc(err.message)}")


def need(handle, err, what):
    if not handle:
        raise RuntimeError(f"{what} returned NULL: [{from_uc(err.key)}] {from_uc(err.message)}")
    return handle


# --- connect --------------------------------------------------------------------
def connect():
    missing = [n for n in ("SAP_RFC_HOST", "SAP_CLIENT", "SAP_RFC_USER", "SAP_RFC_PASSWORD")
               if not os.environ.get(n)]
    if missing:
        sys.exit("Missing required values (add them to your .env): " + ", ".join(missing))
    cfg = {
        "ashost": os.environ["SAP_RFC_HOST"],
        "sysnr": os.environ.get("SAP_RFC_SYSNR", "00"),
        "client": os.environ["SAP_CLIENT"],
        "user": os.environ["SAP_RFC_USER"],
        "passwd": os.environ["SAP_RFC_PASSWORD"],
        "lang": os.environ.get("SAP_RFC_LANG", "EN"),
    }
    arr = (RFC_CONNECTION_PARAMETER * len(cfg))()
    keep = []  # MUST keep buffers alive while RfcOpenConnection reads them
    for i, (k, v) in enumerate(cfg.items()):
        kb, vb = uc(k), uc(v)
        keep.append((kb, vb))
        arr[i].name = ctypes.cast(kb, ctypes.c_void_p)
        arr[i].value = ctypes.cast(vb, ctypes.c_void_p)
    err = RFC_ERROR_INFO()
    conn = sdk.RfcOpenConnection(arr, len(cfg), ctypes.byref(err))
    return need(conn, err, "RfcOpenConnection"), keep


# --- the call: STFC_CONNECTION --------------------------------------------------
def stfc_connection(conn, text="Hello from ctypes"):
    err = RFC_ERROR_INFO()
    desc = need(
        sdk.RfcGetFunctionDesc(conn, p(uc("STFC_CONNECTION")), ctypes.byref(err)),
        err, "RfcGetFunctionDesc(STFC_CONNECTION)",
    )
    fh = need(sdk.RfcCreateFunction(desc, ctypes.byref(err)), err, "RfcCreateFunction")
    try:
        val = uc(text)  # held in a variable so it survives the call
        check(
            sdk.RfcSetString(fh, p(uc("REQUTEXT")), p(val), len(text), ctypes.byref(err)),
            err, "RfcSetString(REQUTEXT)",
        )
        check(sdk.RfcInvoke(conn, fh, ctypes.byref(err)), err, "RfcInvoke(STFC_CONNECTION)")

        def get_str(name, size=512):
            buf = uc_out(size)
            slen = ctypes.c_uint(0)
            check(
                sdk.RfcGetString(fh, p(uc(name)), p(buf), size, ctypes.byref(slen), ctypes.byref(err)),
                err, f"RfcGetString({name})",
            )
            return from_uc(buf)

        return get_str("ECHOTEXT"), get_str("RESPTEXT")
    finally:
        e2 = RFC_ERROR_INFO()
        sdk.RfcDestroyFunction(fh, ctypes.byref(e2))


# --- main -----------------------------------------------------------------------
if __name__ == "__main__":
    conn, _keep = connect()
    print("Connected. Calling STFC_CONNECTION ...")
    echo, resp = stfc_connection(conn)
    print("  ECHOTEXT:", echo)
    print("  RESPTEXT:", resp.strip())
    err = RFC_ERROR_INFO()
    sdk.RfcCloseConnection(conn, ctypes.byref(err))
    print("OK - chain works: Python -> ctypes -> NW RFC SDK -> SAP.")
