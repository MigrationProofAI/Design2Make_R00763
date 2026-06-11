"""
expand_material.py -- standalone SAP material deep-dump. NOTHING to do with MCP/ADK/A2A.

Give it a material number; it talks straight to API_PRODUCT_SRV over OData V2 and:
  1. reads $metadata and prints the FULL navigation schema tree for A_Product
     (so you can see every entity/nested entity the product exposes -- e.g. where a
      production version would live -- whether or not THIS material has one maintained),
  2. deep-expands the material ONE path at a time (a single combined $expand of all navs
     gets a 400 from this service) and DEEP-MERGES every path into one object,
  3. writes the whole thing to expand_<material>.json and greps it for "version"/"production".

Run:   uv run python expand_material.py            # defaults to 11070, depth 2
       uv run python expand_material.py 11070 --depth 3
Reads the same .env as the app (SAP_HOST / SAP_HTTPS_PORT / SAP_CLIENT / SAP_USER / SAP_PASS).
"""
import os
import ssl
import sys
import json
import argparse
import xml.etree.ElementTree as ET

import urllib3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from dotenv import load_dotenv

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SAP_HOST   = os.getenv("SAP_HOST")
SAP_PORT   = os.getenv("SAP_HTTPS_PORT", "44301")
SAP_CLIENT = os.getenv("SAP_CLIENT", "100")
SAP_USER   = os.getenv("SAP_USER")
SAP_PASS   = os.getenv("SAP_PASS")
BASE = f"https://{SAP_HOST}:{SAP_PORT}"
SRV  = "/sap/opu/odata/sap/API_PRODUCT_SRV"
ROOT_ENTITY = "A_Product"


class _Adapter(HTTPAdapter):
    """The SAP appliance offers older TLS ciphers; allow them (matches the app)."""
    def init_poolmanager(self, *a, **k):
        ctx = create_urllib3_context(ciphers="DEFAULT:@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        k["ssl_context"] = ctx
        return super().init_poolmanager(*a, **k)


S = requests.Session()
S.mount("https://", _Adapter())


def _get(path, params=None, accept="application/json"):
    p = {"sap-client": SAP_CLIENT}
    if params:
        p.update(params)
    r = S.get(f"{BASE}{path}", params=p, auth=(SAP_USER, SAP_PASS),
              headers={"Accept": accept}, timeout=120, verify=False)
    return r


# ---------------------------------------------------------------- metadata
def _ln(tag):
    return tag.split("}")[-1]


def parse_metadata():
    """-> (nav_map, props_map, set_map). nav_map[EntityType]=[(navName,targetType),..];
    set_map[EntitySet]=EntityType (the SET name used in URLs differs from the TYPE name --
    e.g. set 'A_Product' -> type 'A_ProductType')."""
    r = _get(f"{SRV}/$metadata", accept="application/xml")
    r.raise_for_status()
    root = ET.fromstring(r.content)
    etypes, assocs, set_map = {}, {}, {}
    for el in root.iter():
        ln = _ln(el.tag)
        if ln == "EntityType":
            navs, props = [], []
            for sub in el:
                s = _ln(sub.tag)
                if s == "NavigationProperty":
                    navs.append((sub.get("Name"), sub.get("Relationship"), sub.get("ToRole")))
                elif s == "Property":
                    props.append(sub.get("Name"))
            etypes[el.get("Name")] = {"navs": navs, "props": props}
        elif ln == "Association":
            ends = {}
            for sub in el:
                if _ln(sub.tag) == "End":
                    ends[sub.get("Role")] = (sub.get("Type") or "").split(".")[-1]
            assocs[el.get("Name")] = ends
        elif ln == "EntitySet":
            set_map[el.get("Name")] = (el.get("EntityType") or "").split(".")[-1]

    nav_map, props_map = {}, {}
    for name, info in etypes.items():
        props_map[name] = info["props"]
        resolved = []
        for nav, rel, to_role in info["navs"]:
            aname = (rel or "").split(".")[-1]
            tgt = assocs.get(aname, {}).get(to_role)
            resolved.append((nav, tgt))
        nav_map[name] = resolved
    return nav_map, props_map, set_map


def print_schema_tree(nav_map, props_map, entity=ROOT_ENTITY, depth=4):
    """Print the navigation schema tree from $metadata (no data fetched)."""
    print(f"\n=== SCHEMA: navigations of {entity} (from $metadata, depth {depth}) ===")
    hits = []

    def walk(ent, prefix, d, seen):
        for nav, tgt in nav_map.get(ent, []):
            line = f"{prefix}-> {nav}  : {tgt}"
            print(line)
            if "version" in nav.lower() or (tgt and "version" in tgt.lower()):
                hits.append(f"{prefix}{nav} -> {tgt}")
            if tgt and d > 1 and tgt not in seen:
                walk(tgt, prefix + "    ", d - 1, seen | {tgt})

    walk(entity, "  ", depth, {entity})

    # also scan plain (non-nav) property names for "version"/"production"
    prop_hits = []
    for ent, props in props_map.items():
        for p in props:
            if "version" in p.lower() or "production" in p.lower():
                prop_hits.append(f"{ent}.{p}")
    if hits:
        print("\n  ** version-related NAVIGATIONS in schema:")
        for h in hits:
            print(f"     {h}")
    if prop_hits:
        print("\n  ** version/production PROPERTIES in schema:")
        for h in sorted(set(prop_hits)):
            print(f"     {h}")
    if not hits and not prop_hits:
        print("\n  (no 'version'/'production' nav or property found in API_PRODUCT_SRV schema)")


# ---------------------------------------------------------------- expand paths
def all_paths(nav_map, entity=ROOT_ENTITY, depth=2):
    """Every root-rooted nav path up to `depth`, with a per-path cycle guard."""
    out = []

    def walk(ent, prefix, d, seen):
        for nav, tgt in nav_map.get(ent, []):
            path = f"{prefix}{nav}" if prefix == "" else f"{prefix}/{nav}"
            out.append(path)
            if tgt and d > 1 and tgt not in seen:
                walk(tgt, path, d - 1, seen | {tgt})

    walk(entity, "", depth, {entity})
    return out


def deep_merge(a, b):
    """Merge two OData JSON fragments: prefer expanded over __deferred; merge
    {'results':[...]} element-wise (same query => same order)."""
    if isinstance(a, dict) and isinstance(b, dict):
        a_def = set(a.keys()) <= {"__deferred"}
        b_def = set(b.keys()) <= {"__deferred"}
        if a_def and not b_def:
            return b
        if b_def and not a_def:
            return a
        out = dict(a)
        for k, v in b.items():
            out[k] = deep_merge(a[k], v) if k in a else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        n = max(len(a), len(b))
        return [deep_merge(a[i] if i < len(a) else None,
                           b[i] if i < len(b) else None) for i in range(n)]
    return b if a is None else a


def _key_variants(material):
    v = [material]
    if material.isdigit():
        v += [material.zfill(18), material.zfill(40)]
    seen, out = set(), []
    for x in v:
        if x not in seen:
            seen.add(x); out.append(x)
    return out


def fetch_material(material, nav_map, depth, root_type):
    # resolve the real stored key first (try as-typed, then zero-padded)
    key = None
    for cand in _key_variants(material):
        r = _get(f"{SRV}/{ROOT_ENTITY}('{cand}')", {"$format": "json"})
        if r.status_code == 200:
            key = cand
            doc = r.json()["d"]
            break
    if key is None:
        print(f"!! material {material} not found (tried {_key_variants(material)}) "
              f"-- last status {r.status_code}")
        sys.exit(2)
    print(f"\n=== DATA: {ROOT_ENTITY}('{key}') -- deep expand, depth {depth} ===")

    paths = all_paths(nav_map, entity=root_type, depth=depth)
    print(f"expanding {len(paths)} navigation path(s) one at a time...")
    ok = empty = err = 0
    for i, path in enumerate(paths, 1):
        r = _get(f"{SRV}/{ROOT_ENTITY}('{key}')", {"$format": "json", "$expand": path})
        tag = ""
        if r.status_code == 200:
            try:
                frag = r.json()["d"]
                doc = deep_merge(doc, frag)
                ok += 1
            except Exception as e:                       # noqa
                err += 1; tag = f"  [parse err {e}]"
        elif r.status_code == 404:
            empty += 1; tag = "  [404]"
        else:
            err += 1; tag = f"  [{r.status_code}]"
        print(f"  [{i:>3}/{len(paths)}] $expand={path}{tag}")
    print(f"\npaths: {ok} merged, {empty} 404, {err} error")
    return key, doc


# ---------------------------------------------------------------- value scan
def scan_values(doc):
    """Find every key (anywhere in the tree) containing 'version' or 'production'."""
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                kp = f"{path}.{k}" if path else k
                if ("version" in k.lower() or "production" in k.lower()) and \
                   not isinstance(v, (dict, list)):
                    hits.append((kp, v))
                walk(v, kp)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(doc, "")
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("material", nargs="?", default="11070")
    ap.add_argument("--depth", type=int, default=2, help="nav nesting depth to expand (default 2)")
    args = ap.parse_args()

    if not (SAP_HOST and SAP_USER and SAP_PASS):
        print("!! missing SAP_HOST / SAP_USER / SAP_PASS in .env"); sys.exit(1)

    nav_map, props_map, set_map = parse_metadata()
    root_type = set_map.get(ROOT_ENTITY, "A_ProductType")   # SET 'A_Product' -> TYPE 'A_ProductType'
    print_schema_tree(nav_map, props_map, entity=root_type, depth=max(args.depth, 4))

    key, doc = fetch_material(args.material, nav_map, args.depth, root_type)

    out_path = f"expand_{key}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"d": doc}, f, indent=2, ensure_ascii=False)
    print(f"\nfull nested object -> {out_path}")

    hits = scan_values(doc)
    print(f"\n=== values whose key contains 'version'/'production' ({len(hits)}) ===")
    for kp, v in hits:
        print(f"  {kp} = {v!r}")
    if not hits:
        print("  (none found in the data -- check the SCHEMA tree above for where it would attach)")


if __name__ == "__main__":
    main()
