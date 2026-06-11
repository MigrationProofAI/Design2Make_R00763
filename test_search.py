"""Exercise the new parametric search engine against live SAP. Safe to delete."""
import importlib

sap = importlib.import_module("mcp_server.sap")

# @mcp.tool() returns the original function, but unwrap defensively just in case.
def call(name, **kw):
    fn = getattr(sap, name)
    fn = getattr(fn, "fn", fn)          # FastMCP FunctionTool fallback
    return fn(**kw)

print("\n########## describe_search_fields ##########")
print(call("describe_search_fields")[:900])

print("\n########## 1) description='Test' ##########")
print(call("search_materials", description="Test", top=5))

print("\n########## 2) product_type='HAWA' AND plant='1710' ##########")
print(call("search_materials", product_type="HAWA", plant="1710", top=5))

print("\n########## 3) country='USA' (alias -> plant 1710) ##########")
print(call("search_materials", country="USA", top=3))

print("\n########## 4) description='Test' AND plant='1710' (intersection) ##########")
print(call("search_materials", description="Test", plant="1710", top=5))

print("\n########## 5) country='Atlantis' (unknown -> warning) ##########")
print(call("search_materials", country="Atlantis", top=2))
