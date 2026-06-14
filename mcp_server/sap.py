"""MCP server exposing SAP S/4HANA OData services as tools.

Reads connection details + credentials from the .env in the project root.
Run standalone for testing:  python ./mcp_server/sap.py
The ADK agent launches it over stdio (see main.py).

Design note — "intelligent" parametric search:
  API_PRODUCT_SRV is OData V2. In V2 you CANNOT filter a parent entity by a
  child entity's field (e.g. you can't filter A_Product by a plant or a
  description in one call). So `search_materials` does a set-intersection JOIN
  in code: every parameter is turned into a filtered query against ITS OWN
  entity returning a set of Product IDs; the sets are intersected; the
  survivors are enriched for display. The LLM only fills parameters — it never
  has to know the OData URL schema. See `describe_search_fields` for grounding.
"""
import os
import re
import ssl
import json
import urllib3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- SAP HTTP/OData connection (names match your .env) ---
SAP_HOST   = os.getenv("SAP_HOST")
SAP_PORT   = os.getenv("SAP_HTTPS_PORT", "44301")
SAP_CLIENT = os.getenv("SAP_CLIENT", "100")
SAP_USER   = os.getenv("SAP_USER")
SAP_PASS   = os.getenv("SAP_PASS")

SAP_BASE_URL = f"https://{SAP_HOST}:{SAP_PORT}"


class SAPHttpAdapter(HTTPAdapter):
    """Allow the older TLS ciphers the SAP appliance offers."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context(ciphers="DEFAULT:@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


# One session, reused by every tool, with the TLS adapter mounted
sap_session = requests.Session()
sap_session.mount("https://", SAPHttpAdapter())

mcp = FastMCP("sap")


def _sap_get(path: str, extra_params: dict | None = None) -> str:
    """Helper: GET an OData path with SAP client + basic auth + JSON."""
    params = {"sap-client": SAP_CLIENT, "$format": "json"}
    if extra_params:
        params.update(extra_params)
    try:
        resp = sap_session.get(
            f"{SAP_BASE_URL}{path}",
            params=params,
            auth=(SAP_USER, SAP_PASS),
            headers={"Accept": "application/json"},
            timeout=120,        # generous, in case the system is warming up
            verify=False,       # appliance cert is self-signed
        )
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.RequestException as e:
        return f"SAP request failed: {e}"


def _cap_result(raw: str, *, max_chars: int, label: str = "") -> str:
    """Bound a tool result so a single fat OData read can't blow the model's context
    window (a few 45k-char query_sap rows piled up to 761k tokens and pinned the trimmer).
    For a v2 collection ({"d":{"results":[...]}}) it keeps as many WHOLE rows as fit the
    budget (valid JSON + an honest count); a single oversized object is hard-truncated with
    a note. The agent can narrow with $select / $filter / a smaller top to see more."""
    if not raw or len(raw) <= max_chars or raw.startswith("SAP request failed"):
        return raw
    try:                                              # keep whole rows for a collection
        obj = json.loads(raw)
        d = obj.get("d", {}) if isinstance(obj, dict) else {}
        rows = d.get("results") if isinstance(d, dict) else None
        if isinstance(rows, list) and rows:
            kept, size = [], 0
            for r in rows:
                rs = len(json.dumps(r))
                if kept and size + rs > max_chars:
                    break
                kept.append(r); size += rs
            if len(kept) < len(rows):
                d["results"] = kept
                d["_truncated"] = (f"showing {len(kept)} of {len(rows)} rows (capped to ~{max_chars} "
                                   f"chars to fit context); add $filter/$select or a smaller top for more")
                return json.dumps(obj)
    except (ValueError, TypeError, AttributeError):
        pass
    return raw[:max_chars] + (f'\n...[{label or "result"} truncated to {max_chars} chars to fit the '
                              f'model context window; use $select to fetch only the fields you need]')


PRODUCT_SRV = "/sap/opu/odata/sap/API_PRODUCT_SRV"

# --- Value-mapping layer -------------------------------------------------
# "US plant" is not a value in SAP -- plants are codes and this service has no
# plant-country field (and a plant-master service is not reachable here). So we
# resolve country -> plant codes from a MAINTAINED config file rather than
# hardcoding guesses. Edit mcp_server/plant_country.json to add real plants;
# `list_plants` shows which plant codes actually exist in the system.
_DEFAULT_PLANTS = {
    "1010": {"country": "DE", "name": "Germany - Werk Hamburg"},
    "1710": {"country": "US", "name": "USA - Plant 1 US"},
}


def _load_plant_info() -> dict:
    """Load plant -> {country, name} from plant_country.json (next to this file).

    Falls back to a small built-in default if the file is missing or malformed,
    so the server always starts.
    """
    path = os.path.join(os.path.dirname(__file__), "plant_country.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        plants = data.get("plants", data)  # accept wrapped {"plants":..} or flat
        return {
            str(p): {"country": str(v.get("country", "")).upper().strip(),
                     "name": v.get("name", "")}
            for p, v in plants.items()
            if isinstance(v, dict) and not str(p).startswith("_")
        }
    except (OSError, json.JSONDecodeError, AttributeError):
        return dict(_DEFAULT_PLANTS)


PLANT_INFO = _load_plant_info()                 # {plant: {country, name}}
PLANT_BY_COUNTRY: dict[str, list[str]] = {}     # {country: [plant, ...]}
for _plant, _info in PLANT_INFO.items():
    if _info["country"]:
        PLANT_BY_COUNTRY.setdefault(_info["country"], []).append(_plant)

# Aliases so the LLM/user can say "USA", "Germany", etc.
COUNTRY_ALIASES = {
    "USA": "US", "UNITED STATES": "US", "AMERICA": "US",
    "GERMANY": "DE", "DEUTSCHLAND": "DE",
    "UK": "GB", "UNITED KINGDOM": "GB", "ENGLAND": "GB",
    "CHINA": "CN", "JAPAN": "JP", "INDIA": "IN",
}


def _esc(value: str) -> str:
    """Escape a literal for an OData string filter (single quote -> doubled)."""
    return str(value).replace("'", "''")


def _resolve_country_to_plants(country: str) -> list[str]:
    key = COUNTRY_ALIASES.get(country.strip().upper(), country.strip().upper())
    return PLANT_BY_COUNTRY.get(key, [])


def _ids_matching(entity: str, filt: str,
                  cap: int = 500) -> tuple[set[str], str | None, bool]:
    """Run a filtered query against `entity`.

    Returns ({Product IDs}, error_or_None, truncated). `truncated` is True when
    the result hit `cap` rows — i.e. there may be more matches than we fetched,
    so any count derived from this set is a lower bound.
    """
    raw = _sap_get(f"{PRODUCT_SRV}/{entity}",
                   {"$filter": filt, "$select": "Product", "$top": cap})
    if raw.startswith("SAP request failed"):
        return set(), raw, False
    try:
        rows = json.loads(raw).get("d", {}).get("results", [])
    except json.JSONDecodeError:
        return set(), f"Could not parse response from {entity}: {raw[:200]}", False
    ids = {r["Product"] for r in rows if "Product" in r}
    return ids, None, len(rows) >= cap


def _enrich(product_ids: list[str], language: str = "EN") -> list[dict]:
    """Fetch display rows (header + descriptions + plants) for given Products."""
    if not product_ids:
        return []
    clause = " or ".join(f"Product eq '{_esc(p)}'" for p in product_ids)
    raw = _sap_get(
        f"{PRODUCT_SRV}/A_Product",
        {"$filter": clause, "$top": len(product_ids),
         "$expand": "to_Description,to_Plant",
         "$select": "Product,ProductType,ProductGroup,Brand,BaseUnit,"
                    "to_Description/Language,to_Description/ProductDescription,"
                    "to_Plant/Plant"},
    )
    out = []
    try:
        rows = json.loads(raw).get("d", {}).get("results", [])
    except json.JSONDecodeError:
        return [{"error": f"enrich parse failed: {raw[:200]}"}]
    for r in rows:
        descs = r.get("to_Description", {}).get("results", [])
        # Prefer the requested language, else the first description available.
        desc = next((d["ProductDescription"] for d in descs
                     if d.get("Language") == language), None)
        if desc is None and descs:
            desc = descs[0].get("ProductDescription")
        plants = [p["Plant"] for p in r.get("to_Plant", {}).get("results", [])]
        out.append({
            "Product": r.get("Product"),
            "Description": desc,
            "ProductType": r.get("ProductType"),
            "ProductGroup": r.get("ProductGroup"),
            "Brand": r.get("Brand") or None,
            "BaseUnit": r.get("BaseUnit"),
            "Plants": plants,
        })
    return out


@mcp.tool()
def search_materials(
    product: str | None = None,
    description: str | None = None,
    brand: str | None = None,
    product_type: str | None = None,
    product_group: str | None = None,
    country_of_origin: str | None = None,
    plant: str | None = None,
    country: str | None = None,
    sales_org: str | None = None,
    distribution_channel: str | None = None,
    language: str = "EN",
    top: int = 20,
) -> str:
    """Search SAP material/product master by ANY combination of attributes.

    This is the smart entry point: supply only the parameters the user mentioned
    and the tool builds the correct OData filters across the right entities and
    intersects the results. Prefer this over `query_sap` for searching.

    Args:
        product: Material/product NUMBER, full or partial (substring match on the
            key, e.g. "TG" matches TG0001/TG0011, "MZ-TG" matches MZ-TG-Y120).
            Use this for code-like tokens ("TG-10", "MZ-TG-Y120"), NOT description.
        description: Free-text in the material description (substring match,
            e.g. "Ryzen", "bicycle", "pump"). Searched in A_ProductDescription.
        brand: Brand name substring (A_Product.Brand).
        product_type: SAP material type code, e.g. "HAWA" (trading good),
            "FERT" (finished), "ROH" (raw). Exact match.
        product_group: Material/product group code, e.g. "E002". Exact match.
        country_of_origin: 2-letter origin country of the material, e.g. "US".
        plant: Exact plant code, e.g. "1710". Restricts to materials extended
            to that plant (A_ProductPlant).
        country: Plant location country/name, e.g. "US" or "Germany". Resolved
            to plant code(s) via the built-in map (see describe_search_fields).
        sales_org: Sales organization, e.g. "1710" (A_ProductSalesDelivery).
        distribution_channel: Distribution channel, e.g. "10".
        language: Language key for the returned description (default "EN").
        top: Max materials to return (default 20).

    Returns:
        JSON with the applied filters, total match count, and enriched rows.
    """
    candidate: set[str] | None = None  # None == no constraint applied yet
    applied: dict = {}
    errors: list[str] = []
    truncated: list[str] = []
    MAX_INLINE_IDS = 80  # how many surviving IDs we'll fold into the next filter

    def run(entity: str, base_filt: str) -> None:
        """Filter one entity and intersect its Product IDs into `candidate`.

        Optimisation/correctness: once `candidate` is small, we scope the next
        query to exactly those IDs (`... and (Product eq 'a' or ...)`). The query
        then returns the precise intersection — no 500-row cap to truncate it,
        so we can't silently drop a match that lived past the cap.
        """
        nonlocal candidate
        if candidate is not None and not candidate:
            return  # already empty — intersection can only stay empty
        filt, scoped = base_filt, False
        if candidate is not None and len(candidate) <= MAX_INLINE_IDS:
            or_ids = " or ".join(f"Product eq '{_esc(p)}'" for p in sorted(candidate))
            filt = f"({base_filt}) and ({or_ids})"
            scoped = True
        ids, err, trunc = _ids_matching(entity, filt)
        if err:
            errors.append(err)
        if trunc and not scoped:  # a scoped query (<=80 IDs) can't meaningfully truncate
            truncated.append(entity)
        candidate = ids if candidate is None else (candidate & ids)

    any_param = any([product, description, brand, product_type, product_group,
                     country_of_origin, plant, country, sales_org,
                     distribution_channel])

    # Order matters: run the typically-selective filters first so `candidate`
    # is small before we touch the high-cardinality plant/sales entities, which
    # then get scoped to the surviving IDs (exact + cheap).

    # 1) Description -> A_ProductDescription. Match ALL whitespace-separated
    #    tokens (AND), not the whole phrase as one substring -- so
    #    "AMD Ryzen 9800X3D" still finds the stored "AMD Ryzen 7 9800X3D"
    #    (extra/missing words and word order no longer break the match).
    if description:
        applied["description"] = description
        tokens = [t for t in description.split() if t]
        clause = " and ".join(
            f"substringof('{_esc(t)}',ProductDescription)" for t in tokens
        ) or f"substringof('{_esc(description)}',ProductDescription)"
        run("A_ProductDescription", clause)

    # 2) Header attributes all live on A_Product -> one combined AND filter
    header = []
    if product:
        header.append(f"substringof('{_esc(product)}',Product)"); applied["product"] = product
    if brand:
        header.append(f"substringof('{_esc(brand)}',Brand)"); applied["brand"] = brand
    if product_type:
        header.append(f"ProductType eq '{_esc(product_type)}'"); applied["product_type"] = product_type
    if product_group:
        header.append(f"ProductGroup eq '{_esc(product_group)}'"); applied["product_group"] = product_group
    if country_of_origin:
        header.append(f"CountryOfOrigin eq '{_esc(country_of_origin)}'"); applied["country_of_origin"] = country_of_origin
    if header:
        run("A_Product", " and ".join(header))

    # 3) Sales attributes -> A_ProductSalesDelivery
    sales = []
    if sales_org:
        sales.append(f"ProductSalesOrg eq '{_esc(sales_org)}'"); applied["sales_org"] = sales_org
    if distribution_channel:
        sales.append(f"ProductDistributionChnl eq '{_esc(distribution_channel)}'"); applied["distribution_channel"] = distribution_channel
    if sales:
        run("A_ProductSalesDelivery", " and ".join(sales))

    # 4) Plant / country -> A_ProductPlant (OR-chain of plant codes). Last,
    #    because "all products in a plant" is the least selective filter.
    plant_codes: list[str] = []
    if plant:
        plant_codes.append(plant); applied["plant"] = plant
    if country:
        resolved = _resolve_country_to_plants(country)
        applied["country"] = {"input": country, "resolved_plants": resolved}
        if not resolved:
            errors.append(f"No plant mapping for country '{country}'. "
                          f"Known: {sorted(PLANT_BY_COUNTRY)}. "
                          f"Pass an explicit plant code instead.")
        plant_codes.extend(resolved)
    if plant_codes:
        clause = " or ".join(f"Plant eq '{_esc(p)}'"
                             for p in dict.fromkeys(plant_codes))
        run("A_ProductPlant", clause)
    elif plant or country:
        # A location was requested but resolves to no real plant code -> the
        # constraint is unsatisfiable. Return nothing rather than silently
        # dropping it and showing unrelated rows. (The reason is already in
        # `errors`, e.g. "No plant mapping for country 'GB'".)
        candidate = set()

    # Nothing actually constrained the result.
    if candidate is None:
        if any_param:
            # Params were given but none could be applied (e.g. unknown country)
            return json.dumps({"applied_filters": applied, "total_matches": 0,
                               "returned": 0, "materials": [],
                               "warnings": errors or ["No usable filter."]},
                              indent=2)
        # Truly no input -> behave like a plain list
        raw = _sap_get(f"{PRODUCT_SRV}/A_Product", {"$top": top})
        return json.dumps({"note": "No search parameters supplied; returning "
                                   "first materials. Pass filters to narrow.",
                           "raw_top": json.loads(raw).get("d", {}).get("results", [])
                           if not raw.startswith("SAP request failed") else raw},
                          indent=2)

    chosen = sorted(candidate)[:top]
    result = {
        "applied_filters": applied,
        "total_matches": len(candidate),
        "returned": len(chosen),
        "materials": _enrich(chosen, language=language),
    }
    if truncated:
        errors.append(
            f"Pre-intersection result hit the 500-row cap for: "
            f"{sorted(set(truncated))}. total_matches is a lower bound; add "
            f"more (or more selective) filters for an exact count.")
    if errors:
        result["warnings"] = errors
    return json.dumps(result, indent=2)


@mcp.tool()
def list_plants() -> str:
    """List the plant codes that ACTUALLY exist in this system, each with its
    mapped country/name (or "(unmapped)").

    Use this to ground plant/country searches and to discover which plant codes
    you can still add to mcp_server/plant_country.json. (The product service has
    no plant-country field, so the mapping is maintained config, not live data.)
    """
    raw = _sap_get(f"{PRODUCT_SRV}/A_ProductPlant",
                   {"$select": "Plant", "$top": 5000})
    try:
        real = sorted({r["Plant"] for r in json.loads(raw)["d"]["results"]})
    except (json.JSONDecodeError, KeyError):
        return raw  # surface the underlying error text
    rows = []
    for p in real:
        info = PLANT_INFO.get(p)
        rows.append({"plant": p,
                     "country": info["country"] if info else None,
                     "name": info["name"] if info else "(unmapped)"})
    return json.dumps({
        "plants_in_system": rows,
        "mapped_countries": PLANT_BY_COUNTRY,
        "note": "Add unmapped plants in mcp_server/plant_country.json "
                "(country = ISO 2-letter code).",
    }, indent=2)


@mcp.tool()
def describe_search_fields() -> str:
    """Describe what `search_materials` can filter on (grounding for the agent).

    Call this when unsure which parameters or codes to use. Returns the valid
    search parameters, the SAP entity each maps to, example values, and the
    country->plant mapping used to resolve queries like "US plant".
    """
    return json.dumps({
        "tool": "search_materials",
        "how_it_works": "Each parameter is filtered against its own OData "
                        "entity; the resulting Product-ID sets are intersected "
                        "(logical AND across parameters).",
        "parameters": {
            "product": {"entity": "A_Product.Product", "match": "substring",
                        "note": "material NUMBER, not its text. Use for code-like "
                                "tokens.",
                        "examples": ["TG", "TG0011", "MZ-TG-Y120"]},
            "description": {"entity": "A_ProductDescription",
                            "match": "all words (AND of substrings, any order)",
                            "examples": ["Ryzen", "AMD Ryzen 9800X3D", "bicycle"]},
            "brand": {"entity": "A_Product.Brand", "match": "substring"},
            "product_type": {"entity": "A_Product.ProductType", "match": "exact",
                             "examples": {"HAWA": "trading good", "FERT": "finished",
                                          "ROH": "raw material", "HALB": "semi-finished"}},
            "product_group": {"entity": "A_Product.ProductGroup", "match": "exact",
                              "examples": ["E002", "ZMCATRET"]},
            "country_of_origin": {"entity": "A_Product.CountryOfOrigin",
                                  "match": "exact", "examples": ["US", "DE"]},
            "plant": {"entity": "A_ProductPlant.Plant", "match": "exact",
                      "examples": ["1710", "1010"]},
            "country": {"resolves_to": "plant codes via map below",
                        "examples": ["US", "Germany", "Japan"]},
            "sales_org": {"entity": "A_ProductSalesDelivery.ProductSalesOrg",
                          "examples": ["1710"]},
            "distribution_channel": {"entity": "A_ProductSalesDelivery.ProductDistributionChnl",
                                     "examples": ["10"]},
        },
        "country_to_plant_map": PLANT_BY_COUNTRY,
        "country_aliases": COUNTRY_ALIASES,
        "known_plants": PLANT_INFO,
        "tip": "Call list_plants to see which plant codes actually exist in "
               "this system before filtering by plant/country.",
    }, indent=2)


# ---- $metadata-driven discovery (ANY service: fields + keys + navigations) --------
PRODUCT_SRV_NAME = "API_PRODUCT_SRV"

# Friendly service names the agent can pass -> OData base path. Covers the material +
# the four make-object services, so the agent can discover ANY of them (not just products).
_KNOWN_SERVICES = {
    "API_PRODUCT_SRV": "/sap/opu/odata/sap/API_PRODUCT_SRV",
    "API_INFORECORD_PROCESS_SRV": "/sap/opu/odata/sap/API_INFORECORD_PROCESS_SRV",
    "API_PURGPRCGCONDITIONRECORD_SRV": "/sap/opu/odata/sap/API_PURGPRCGCONDITIONRECORD_SRV",
    "API_BILL_OF_MATERIAL_SRV": "/sap/opu/odata/sap/API_BILL_OF_MATERIAL_SRV;v=2",
    "API_PRODUCTION_ROUTING": "/sap/opu/odata/sap/API_PRODUCTION_ROUTING",
}

_FIELDS_CACHE: dict = {}   # service_path -> [field dicts]
_META_CACHE: dict = {}     # service_path -> {entity: {"keys": [...], "navs": [...]}}
_NAVTGT_CACHE: dict = {}   # service_path -> {entity: {nav_name: target_entity}}


def _service_path(service: str | None) -> str:
    """Resolve a service name/path to its OData base path (default API_PRODUCT_SRV)."""
    if not service:
        return PRODUCT_SRV
    s = service.strip()
    return _KNOWN_SERVICES.get(s, s if s.startswith("/") else f"/sap/opu/odata/sap/{s}")


def _load_metadata(service: str | None = None) -> tuple[list, dict]:
    """Fetch + parse a service's $metadata into (fields, entity_meta), cached per service.
    fields: [{entity, field, label, info, type}].  entity_meta: {entity: {keys, navs}}.
    Also builds nav->target-entity (in _NAVTGT_CACHE) by resolving Associations, so callers
    can expand nested segments. $metadata must be fetched WITHOUT $format/$top (those 400 it)."""
    path = _service_path(service)
    if path in _FIELDS_CACHE:
        return _FIELDS_CACHE[path], _META_CACHE[path]
    try:
        resp = sap_session.get(
            f"{SAP_BASE_URL}{path}/$metadata",
            params={"sap-client": SAP_CLIENT}, auth=(SAP_USER, SAP_PASS),
            headers={"Accept": "application/xml"}, timeout=120, verify=False)
        resp.raise_for_status()
        xml = resp.text
    except requests.exceptions.RequestException:
        _FIELDS_CACHE[path], _META_CACHE[path], _NAVTGT_CACHE[path] = [], {}, {}
        return [], {}
    fields, meta, nav_attrs = [], {}, {}
    for et in re.finditer(r'<EntityType Name="([^"]+)".*?</EntityType>', xml, re.S):
        ename = et.group(1)
        entity = ename[:-4] if ename.endswith("Type") else ename     # set name, not type
        block = et.group(0)
        meta[entity] = {
            "keys": re.findall(r'<PropertyRef Name="([^"]+)"', block),
            "navs": re.findall(r'<NavigationProperty Name="([^"]+)"', block),
        }
        na = {}                                          # nav -> (assoc_localname, toRole)
        for nm in re.finditer(r'<NavigationProperty\b([^>]*?)/?>', block):
            a = nm.group(1)
            nme, rel, to = (re.search(p, a) for p in
                            (r'Name="([^"]+)"', r'Relationship="([^"]+)"', r'ToRole="([^"]+)"'))
            if nme:
                na[nme.group(1)] = (rel.group(1).split('.')[-1] if rel else None,
                                    to.group(1) if to else None)
        nav_attrs[entity] = na
        for pm in re.finditer(r'<Property Name="([^"]+)"([^>]*?)/?>', block):
            attrs = pm.group(2)

            def _a(name, _attrs=attrs):
                m = re.search(name + r'="([^"]*)"', _attrs)
                return m.group(1) if m else ""
            fields.append({"entity": entity, "field": pm.group(1),
                           "label": _a("sap:label"), "info": _a("sap:quickinfo"),
                           "type": _a("Type")})
    # Associations -> {assoc_localname: {role: target_entity}}
    assoc: dict = {}
    for a in re.finditer(r'<Association Name="([^"]+)"[^>]*>(.*?)</Association>', xml, re.S):
        roles = {}
        for e in re.finditer(r'<End\b([^>]*?)/?>', a.group(2)):
            role, typ = re.search(r'Role="([^"]+)"', e.group(1)), re.search(r'Type="([^"]+)"', e.group(1))
            if role and typ:
                t = typ.group(1).split('.')[-1]
                roles[role.group(1)] = t[:-4] if t.endswith("Type") else t
        assoc[a.group(1)] = roles
    nav_targets = {e: {nv: assoc.get(rel, {}).get(to)
                       for nv, (rel, to) in na.items() if rel and to}
                   for e, na in nav_attrs.items()}
    _FIELDS_CACHE[path], _META_CACHE[path], _NAVTGT_CACHE[path] = fields, meta, nav_targets
    return fields, meta


def _normalize_keys(provided: dict, real_keys: list) -> dict:
    """Map an agent's (often mis-named) key dict onto the entity's REAL key fields from
    $metadata. A cheap LLM calls explore_entity with keys={'Material':'11070'} but A_Product's
    key is 'Product' -> A_Product(Material='11070') -> 400. Match real keys by name
    (case-insensitive) first, then fill any leftover real keys positionally from the remaining
    values. So {'Material':'11070'} -> {'Product':'11070'}; a correct dict passes through."""
    if not provided or not real_keys:
        return provided
    lower = {k.lower(): k for k in provided}
    out, used = {}, set()
    for rk in real_keys:                                  # 1) match by (case-insensitive) name
        if rk.lower() in lower:
            ok = lower[rk.lower()]
            out[rk] = provided[ok]
            used.add(ok)
    leftover = [v for k, v in provided.items() if k not in used]
    for rk, v in zip([rk for rk in real_keys if rk not in out], leftover):   # 2) fill by position
        out[rk] = v
    return out or provided


@mcp.tool()
def find_field(term: str, entity: str | None = None, service: str | None = None) -> str:
    """Find the EXACT OData field name(s) for a business term, from the live $metadata.

    Searches every field's name, SAP label and description. Resolve fields here instead of
    guessing. e.g. find_field("minimum order quantity") -> MinimumOrderQuantity.

    Args:
        term: the business words, e.g. "net price", "lead time", "minimum order quantity".
        entity: optional entity set to restrict to, e.g. "A_ProductSalesDelivery".
        service: which OData service -- DEFAULT API_PRODUCT_SRV (materials). For the make
            objects pass one of: API_INFORECORD_PROCESS_SRV (PIR),
            API_PURGPRCGCONDITIONRECORD_SRV (cost), API_BILL_OF_MATERIAL_SRV (BOM),
            API_PRODUCTION_ROUTING (routing).
    """
    fields, _ = _load_metadata(service)
    if not fields:
        return f"Could not load $metadata for {service or PRODUCT_SRV_NAME}."
    tokens = [t.lower() for t in term.split() if t]
    out = []
    for f in fields:
        if entity and entity.lower() not in f["entity"].lower():
            continue
        hay = f"{f['field']} {f['label']} {f['info']}".lower()
        if all(t in hay for t in tokens):
            out.append(f)
    if not out:
        return json.dumps({"term": term, "service": service or PRODUCT_SRV_NAME, "matches": [],
                           "hint": "No field matched -- try fewer words, or set `service`."})
    return json.dumps({"term": term, "service": service or PRODUCT_SRV_NAME,
                       "count": len(out), "matches": out[:25]}, indent=2)


@mcp.tool()
def list_fields(entity: str, service: str | None = None) -> str:
    """List an entity's fields, KEYS and NAVIGATION properties (its child entities), from
    the live $metadata. Use this to learn an entity's exact KEY (for change_material_view /
    change_make_object) and its NAV names (for $expand) -- never guess them. e.g.
    list_fields("A_PurchasingInfoRecord", service="API_INFORECORD_PROCESS_SRV")
    -> key [PurchasingInfoRecord], nav [to_PurgInfoRecdOrgPlantData].

    Args:
        entity: the entity set, e.g. "A_Product", "A_PurchasingInfoRecord".
        service: OData service (default API_PRODUCT_SRV). See find_field for the make services.
    """
    allf, meta = _load_metadata(service)
    key = next((e for e in meta if e.lower() == entity.strip().lower()), None)
    if not key:
        return json.dumps({"entity": entity, "service": service or PRODUCT_SRV_NAME,
                           "fields": [], "known_entities": sorted(meta)[:60]})
    em = meta.get(key, {})
    fields = [f for f in allf if f["entity"] == key]
    return json.dumps({"entity": key, "service": service or PRODUCT_SRV_NAME,
                       "keys": em.get("keys", []), "navigations": em.get("navs", []),
                       "field_count": len(fields),
                       "fields": [{"field": f["field"], "label": f["label"],
                                   "type": f["type"]} for f in fields]}, indent=2)


@mcp.tool()
def explore_entity(entity: str, filter: str = "", keys: dict | None = None,
                   service: str | None = None, depth: int = 2) -> str:
    """Read an object WITH its nested segments expanded -- the reliable way to answer
    "show the full X" / "all segments of this X". It DISCOVERS the entity's navigation
    properties from $metadata and builds the $expand for you (up to `depth` levels), so you
    never guess nav names or which key is which.

    Identify the row with `filter` (use this when the user gives BUSINESS keys like
    material+supplier, which are NOT the object's own number) OR `keys` (the exact key).
    e.g. all segments of a PIR the user described by material+supplier:
        explore_entity("A_PurchasingInfoRecord",
            filter="Material eq '11066' and Supplier eq '17300001'",
            service="API_INFORECORD_PROCESS_SRV")

    Args:
        entity: root entity set, e.g. "A_PurchasingInfoRecord", "ProductionRoutingHeader".
        filter: an OData $filter to locate the row(s) by business keys (NOT the entity's own id).
        keys: alternatively the exact key {field: value} to address exactly one row.
        service: OData service (default API_PRODUCT_SRV); pass the service name for make objects
                 (API_INFORECORD_PROCESS_SRV / API_PURGPRCGCONDITIONRECORD_SRV /
                 API_BILL_OF_MATERIAL_SRV / API_PRODUCTION_ROUTING).
        depth: how many nav levels to expand (1 or 2; default 2).
    """
    _, meta = _load_metadata(service)
    nav_targets = _NAVTGT_CACHE.get(_service_path(service), {})
    ent = next((e for e in meta if e.lower() == entity.strip().lower()), None)
    if not ent:
        return json.dumps({"entity": entity, "error": "unknown entity for this service",
                           "known_entities": sorted(meta)[:60]})
    navs = meta[ent]["navs"]
    expand = list(navs)
    if depth >= 2:
        for nv in navs:                                  # add child navs (nav/childnav)
            child = nav_targets.get(ent, {}).get(nv)
            expand += [f"{nv}/{cnv}" for cnv in meta.get(child, {}).get("navs", [])]
    path = _service_path(service)
    if keys:
        keys = _normalize_keys(keys, meta[ent].get("keys", []))   # fix mis-named keys (Material->Product)
        url = f"{path}/{ent}(" + ",".join(f"{k}='{_esc(v)}'" for k, v in keys.items()) + ")"
        base: dict = {}
    else:
        url, base = f"{path}/{ent}", {"$top": 5}
        if filter:
            base["$filter"] = filter

    if not expand:
        return _cap_result(_sap_get(url, base), max_chars=14000, label="explore_entity")

    # 1) try the whole $expand in one call (works for entities with a few navs).
    one = _sap_get(url, {**base, "$expand": ",".join(expand)})
    if one[:1] == "{" and '"error"' not in one[:400] and not one.startswith("SAP request failed"):
        return _cap_result(one, max_chars=14000, label="explore_entity")

    # 2) Rejected -- entities with MANY navs (e.g. A_Product's 13) overflow SAP's $expand limit,
    #    especially as a collection ($top + filter). Fall back to base + ONE expand per nav,
    #    merged. Robust "all segments" at the cost of N+1 small reads.
    raw = _sap_get(url, base)
    try:
        d = json.loads(raw)["d"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return raw                                       # even the base read failed -> surface it
    targets = d.get("results", [d])
    for nv in navs:
        try:
            sd = json.loads(_sap_get(url, {**base, "$expand": nv}))["d"]
            for i, srow in enumerate(sd.get("results", [sd])):
                if i < len(targets) and nv in srow:
                    targets[i][nv] = srow[nv]
        except (json.JSONDecodeError, KeyError, TypeError):
            pass                                         # skip a nav that won't expand
    return _cap_result(json.dumps({"d": d}, indent=2), max_chars=14000, label="explore_entity")


@mcp.tool()
def list_materials(top: int = 5) -> str:
    """List material/product master records from SAP S/4HANA.

    Args:
        top: Maximum number of materials to return (default 5).
    """
    return _sap_get("/sap/opu/odata/sap/API_PRODUCT_SRV/A_Product", {"$top": top})


# Lean header fields for get_material -- the ones agents actually use to confirm a
# material exists and plan a change. Returning only these keeps a single read small,
# so a turn that reads MANY materials (e.g. extending a BOM's components to a plant,
# which read 28 materials in one loop and pressured the context window) stays lean.
_GET_MATERIAL_LEAN_FIELDS = (
    "Product", "ProductType", "BaseUnit", "ProductGroup", "IndustrySector",
    "CrossPlantStatus", "Division", "GrossWeight", "NetWeight", "WeightUnit",
    "ProductStandardID", "ItemCategoryGroup", "PurchaseOrderQuantityUnit",
    "CountryOfOrigin",        # assurance.py reads this to judge CoO; omitting it made every
                             # Compliance check false-flag "CountryOfOrigin not specified".
    "CreationDate", "LastChangeDate",
)


@mcp.tool()
def get_material(material_id: str, full: bool = False) -> str:
    """Fetch a single material/product master record by ID from SAP S/4HANA.

    Returns a LEAN set of the most-used header fields by default, so reading many
    materials in one turn (e.g. extending a BOM's components to a plant) stays well
    within the model's context window. Pass full=True only when you need a header
    field outside the lean set (use find_field/list_fields to discover field names).

    Args:
        material_id: The exact material/product number, e.g. "21".
        full: True -> the complete entity (every header field). Default False (lean).
    """
    # Single-entity read by key — no $top (invalid on a single entity)
    raw = _sap_get(f"/sap/opu/odata/sap/API_PRODUCT_SRV/A_Product('{material_id}')")
    if full or raw.startswith("SAP request failed"):
        return raw
    try:                                          # prune to the lean set; never break a read
        d = json.loads(raw).get("d", {}) or {}
        lean = {k: d[k] for k in _GET_MATERIAL_LEAN_FIELDS if k in d}
        if not lean:                              # unexpected shape -> return raw untouched
            return raw
        lean["_note"] = "lean view -- call get_material(id, full=true) for all header fields"
        return json.dumps({"d": lean})
    except (ValueError, TypeError, AttributeError):
        return raw


@mcp.tool()
def query_sap(path: str, top: int = 5) -> str:
    """Read data from ANY SAP S/4HANA OData service (GET). Use for collections/lists.

    Args:
        path: OData path after the host, e.g.
              "/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_BusinessPartner"
        top:  Maximum number of records to return (default 5).

    Tip: rows are FULL entities and can be large. If you only need a few fields, add
    "$select=Field1,Field2" to the path -- the result is capped to keep context lean.
    """
    return _cap_result(_sap_get(path, {"$top": top}), max_chars=12000, label="query_sap")

# ---- writes: CSRF + confirm-gated create / update ---------------------------
_PRODUCT_SRV = f"{SAP_BASE_URL}/sap/opu/odata/sap/API_PRODUCT_SRV"


def _csrf_for(service_root: str) -> str:
    """Fetch a CSRF token (+ session cookies) from a service root on the shared session.
    OData v2 rejects any write without it; the SAME sap_session is reused for the write."""
    resp = sap_session.get(
        f"{service_root}/",
        params={"sap-client": SAP_CLIENT},
        auth=(SAP_USER, SAP_PASS),
        headers={"X-CSRF-Token": "Fetch", "Accept": "application/json"},
        timeout=120, verify=False,
    )
    return resp.headers.get("x-csrf-token", "")


def _sap_csrf_token() -> str:
    """CSRF token from API_PRODUCT_SRV (used by create_material / update_material)."""
    return _csrf_for(_PRODUCT_SRV)


def _key_predicate(entity: str, keys: dict, service: str | None = None) -> str:
    """Build an OData v2 key predicate, formatting each literal by its $metadata type:
    Edm.DateTime -> datetime'..', numeric -> bare, else -> '..'. So a datetime key (e.g. a
    condition's ConditionValidityEndDate) addresses correctly instead of 404-ing as a string."""
    fields, _ = _load_metadata(service)
    types = {f["field"]: f["type"] for f in fields if f["entity"].lower() == entity.lower()}
    parts = []
    for k, v in keys.items():
        t = types.get(k, "Edm.String")
        sv = str(v).strip()
        if "DateTime" in t:
            sv = sv.replace("datetime", "").strip().strip("'")
            parts.append(f"{k}=datetime'{sv}'")
        elif any(n in t for n in ("Decimal", "Int", "Double", "Single", "Byte")):
            parts.append(f"{k}={sv}")
        else:
            parts.append(f"{k}='{_esc(sv)}'")
    return ",".join(parts)


# ---- deep-insert payload builder --------------------------------------------
# Header + to_Description + plant view (to_Plant w/ nested supply planning +
# storage) + to_Valuation are LIFTED verbatim from Design2Make's
# AgentsSAPCreateMat._build_sap_payload (lines 868-957). The sales view
# (to_SalesDelivery -> to_SalesTax{Country,TaxCategory,TaxClassification}) is
# authored from the API_PRODUCT_SRV $metadata (it does not exist in Design2Make).
_SALES_RELEVANT_TYPES = {"FERT", "HAWA", "HALB"}  # get ItemCategoryGroup NORM

# Valuation class depends on material type, else SAP raises M3/180 on create.
_VALUATION_CLASS_BY_TYPE = {"ROH": "3000", "HAWA": "3100", "HALB": "7900",
                            "FERT": "7920", "VERP": "3000"}

# In-house produced types (mirrors the KG: FERT/HALB = in-house "E", has_view
# WorkScheduling). They default to procurement "E" and get a WORK-SCHEDULING view on
# the plant -- without it SAP rejects routing creation (CZCL/002). So a FERT built here
# is born routable, honoring the ontology rather than hardcoding business rules.
_MADE_TYPES = {"FERT", "HALB"}

# A sales view (to_SalesDelivery) needs the COMPLETE tax-classification set for
# the sales area or SAP raises MG/172. Verified set for vhcals4hci; edit for
# other systems / tax setups. TaxClassification "1" = Full tax (NOT blank --
# blank is rejected on create even though the API READ returns it blank).
_DEFAULT_SALES_TAX = [
    {"Country": "DE", "TaxCategory": "TTX1", "TaxClassification": "1"},
    {"Country": "US", "TaxCategory": "UTXJ", "TaxClassification": "1"},
]


def _build_material_payload(
    description: str,
    product_type: str = "ROH",
    industry_sector: str = "M",
    base_unit: str = "EA",
    product_group: str = "50101001",
    division: str = "00",
    long_text: str | None = None,
    # plant view (to_Plant) + valuation -- included only when `plant` is given
    plant: str | None = None,
    purchasing_group: str = "002",
    profit_center: str = "YB700",
    mrp_type: str = "PD",
    mrp_responsible: str = "001",
    procurement_type: str | None = None,  # default derived from product_type (E for made)
    storage_location: str = "171A",
    valuation_class: str | None = None,   # default derived from product_type
    standard_price: float = 100.0,
    currency: str = "USD",
    # sales view (to_SalesDelivery) + tax (to_SalesTax) -- included via sales_org
    sales_org: str | None = None,
    distribution_channel: str = "10",
    tax_classifications: list | None = None,  # default: complete set for the system
) -> dict:
    """Assemble the A_Product deep-insert `fields` dict. Sections are optional:
    pass `plant` to add the plant+valuation views, `sales_org` to add the sales
    view, `tax_country` to nest the tax classification under it."""
    if procurement_type is None:               # in-house for made types, else external
        procurement_type = "E" if product_type in _MADE_TYPES else "F"
    payload: dict = {
        "ProductType": product_type,
        "IndustrySector": industry_sector,
        "BaseUnit": base_unit,
        "ProductGroup": product_group,
        "Division": division,
        # MAKTX is 40 chars max in SAP (legacy truncation).
        "to_Description": {"results": [
            {"Language": "EN", "ProductDescription": description[:40]}]},
    }
    if product_type in _SALES_RELEVANT_TYPES:
        payload["ItemCategoryGroup"] = "NORM"
    if long_text:
        payload["to_ProductBasicText"] = {"results": [
            {"Language": "EN", "LongText": long_text}]}

    # --- Plant view (to_Plant) + nested supply planning + storage, + valuation
    if plant:
        plant_view = {
            "Plant": plant,
            "PurchasingGroup": purchasing_group,
            "ProfitCenter": profit_center,
            "MRPType": mrp_type,
            "MRPResponsible": mrp_responsible,
            "ProcurementType": procurement_type,
            "AvailabilityCheckType": "02",
            "to_ProductSupplyPlanning": {        # NOTE: bare object, NOT results[]
                "Plant": plant,
                "LotSizingProcedure": "EX",
                "MRPType": mrp_type,
                "MRPResponsible": mrp_responsible,
                "ProcurementType": procurement_type,
                "AvailabilityCheckType": "02",
                "PlannedDeliveryDurationInDays": "10",
                "TotalReplenishmentLeadTime": "4",
            },
            "to_StorageLocation": {"results": [
                {"Plant": plant, "StorageLocation": storage_location}]},
        }
        # Made types (FERT/HALB) get the WORK-SCHEDULING view (nested on the plant, 1:1
        # bare object like supply planning) so routing creation is allowed (no CZCL/002).
        if product_type in _MADE_TYPES:
            plant_view["to_ProductWorkScheduling"] = {"Plant": plant}
        payload["to_Plant"] = {"results": [plant_view]}
        payload["to_Valuation"] = {"results": [{
            "ValuationArea": plant,
            "ValuationType": "",
            "ValuationClass": valuation_class or _VALUATION_CLASS_BY_TYPE.get(
                product_type, "3000"),
            "InventoryValuationProcedure": "V",
            "PriceDeterminationControl": "2",
            "StandardPrice": str(standard_price),
            "PriceUnitQty": "1",
            "Currency": currency,
        }]}

    # --- Sales view (to_SalesDelivery) + tax classification (to_SalesTax) -----
    if sales_org:
        sales = {
            "ProductSalesOrg": sales_org,
            "ProductDistributionChnl": distribution_channel,
            "ItemCategoryGroup": "NORM",
        }
        # A_ProductSalesTax keys per $metadata: Country, TaxCategory, TaxClassification.
        # Default to the complete set the system requires (avoids MG/172).
        tax_rows = _DEFAULT_SALES_TAX if tax_classifications is None else tax_classifications
        if tax_rows:
            sales["to_SalesTax"] = {"results": [dict(t) for t in tax_rows]}
        payload["to_SalesDelivery"] = {"results": [sales]}

    return payload


@mcp.tool()
def build_material_payload(
    description: str,
    product_type: str = "ROH",
    base_unit: str = "EA",
    product_group: str = "50101001",
    long_text: str | None = None,
    plant: str | None = None,
    mrp_type: str = "PD",
    procurement_type: str | None = None,
    valuation_class: str | None = None,
    standard_price: float = 100.0,
    sales_org: str | None = None,
    distribution_channel: str = "10",
) -> str:
    """Assemble a ready-to-POST deep-insert payload, then hand it to create_material.

    Returns {"fields": {...}} to pass straight to create_material(fields=..., confirm=false).
    Builds header + to_Description, plus the plant view (to_Plant w/ supply planning +
    storage + to_Valuation) when `plant` is given, plus the sales view
    (to_SalesDelivery + tax) when `sales_org` is given.

    Smart defaults verified against the system, so the agent need not know them:
      * valuation_class defaults by material type (ROH 3000, HAWA 3100, FERT 7920,
        HALB 7900) -- avoids SAP error M3/180.
      * a sales view auto-includes the COMPLETE tax-classification set (DE/TTX1 +
        US/UTXJ, Full tax) -- avoids SAP error MG/172.
    Resolve other coded values with list_allowed_values when unsure. Does NOT write.
    """
    fields = _build_material_payload(
        description=description, product_type=product_type, base_unit=base_unit,
        product_group=product_group, long_text=long_text, plant=plant,
        mrp_type=mrp_type, procurement_type=procurement_type,
        valuation_class=valuation_class, standard_price=standard_price,
        sales_org=sales_org, distribution_channel=distribution_channel,
    )
    return json.dumps({
        "fields": fields,
        "next": "Pass `fields` to create_material(confirm=false) to preview, "
                "then confirm=true to write.",
    }, indent=2)


@mcp.tool()
def create_material(fields: dict, confirm: bool = False) -> str:
    """Create a material (A_Product) in SAP S/4HANA via OData deep insert.

    SAFETY GATE: with confirm=false (default) this only PREVIEWS the exact request —
    it does NOT write. Call once with confirm=false, show the preview to the user,
    and only call again with confirm=true after they explicitly approve.

    Easiest path: call build_material_payload(...) to assemble `fields`, then pass
    its "fields" here. Or build it yourself. Deep-insert nested views use
    {"results":[ {...} ]} (OData v2) -- NOT a bare list -- EXCEPT
    to_ProductSupplyPlanning, which is a bare object. Canonical shape:
        {
          "ProductType":"ROH","IndustrySector":"M","BaseUnit":"EA",
          "ProductGroup":"50101001","Division":"00",
          "to_Description":{"results":[{"Language":"EN","ProductDescription":"..."}]},
          "to_Plant":{"results":[{
            "Plant":"1710","PurchasingGroup":"002","ProfitCenter":"YB700",
            "MRPType":"PD","MRPResponsible":"001","ProcurementType":"F",
            "AvailabilityCheckType":"02",
            "to_ProductSupplyPlanning":{"Plant":"1710","LotSizingProcedure":"EX", ...},
            "to_StorageLocation":{"results":[{"Plant":"1710","StorageLocation":"171A"}]}}]},
          "to_Valuation":{"results":[{"ValuationArea":"1710","ValuationClass":"3000",
            "InventoryValuationProcedure":"V","PriceDeterminationControl":"2",
            "StandardPrice":"100.0","PriceUnitQty":"1","Currency":"USD"}]},
          "to_SalesDelivery":{"results":[{"ProductSalesOrg":"1710",
            "ProductDistributionChnl":"10","ItemCategoryGroup":"NORM",
            "to_SalesTax":{"results":[{"Country":"US","TaxCategory":"UTXJ",
              "TaxClassification":"1"}]}}]}
        }

    Args:
        fields: the deep-insert dict above (or the "fields" from build_material_payload).
                Omit "Product" if the material type uses internal numbering. Resolve
                coded values (ProductType, ValuationClass, TaxCategory,
                TaxClassification...) with list_allowed_values first; decimals as strings.
        confirm: must be true to actually create.
    """
    url = f"{_PRODUCT_SRV}/A_Product"
    if not confirm:
        return ("PREVIEW — nothing written. Confirm with the user, then call again "
                f"with confirm=true.\nPOST {url}\n"
                f"{json.dumps(fields, indent=2, ensure_ascii=False)}")
    token = _sap_csrf_token()
    if not token:
        return "Could not obtain a CSRF token from SAP (write blocked)."
    try:
        resp = sap_session.post(
            url, params={"sap-client": SAP_CLIENT}, auth=(SAP_USER, SAP_PASS),
            headers={"X-CSRF-Token": token, "Content-Type": "application/json",
                     "Accept": "application/json"},
            json=fields, timeout=120, verify=False,
        )
        resp.raise_for_status()
        return f"Created material (HTTP {resp.status_code}): {resp.text[:600]}"
    except requests.exceptions.RequestException as e:
        detail = e.response.text[:600] if getattr(e, "response", None) is not None else ""
        return f"Create failed: {e}\n{detail}"


@mcp.tool()
def update_material(material_id: str, fields: dict, confirm: bool = False) -> str:
    """Change one or more fields on an existing material (A_Product).

    SAFETY GATE: confirm=false (default) only PREVIEWS — no write. Preview, get the
    user's approval, then call again with confirm=true. Never confirm=true without it.

    Args:
        material_id: the Product key, e.g. "TG10".
        fields: {ExactODataFieldName: new_value}. Read the material first
                (get_material) so names are exact; resolve coded values with
                list_allowed_values; decimals as strings.
        confirm: must be true to actually write.
    """
    url = f"{_PRODUCT_SRV}/A_Product('{material_id}')"
    if not confirm:
        return ("PREVIEW — nothing written. Confirm with the user, then call again "
                f"with confirm=true.\nPATCH {url}\n"
                f"{json.dumps(fields, indent=2, ensure_ascii=False)}")
    token = _sap_csrf_token()
    if not token:
        return "Could not obtain a CSRF token from SAP (write blocked)."
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json",
               "Accept": "application/json"}
    try:
        resp = sap_session.patch(
            url, params={"sap-client": SAP_CLIENT}, auth=(SAP_USER, SAP_PASS),
            headers=headers, json=fields, timeout=120, verify=False,
        )
        if resp.status_code == 405:           # some releases want the MERGE tunnel
            headers["X-HTTP-Method"] = "MERGE"
            resp = sap_session.post(
                url, params={"sap-client": SAP_CLIENT}, auth=(SAP_USER, SAP_PASS),
                headers=headers, json=fields, timeout=120, verify=False,
            )
        resp.raise_for_status()
        return f"Updated {material_id} (HTTP {resp.status_code})."   # success is 204
    except requests.exceptions.RequestException as e:
        detail = e.response.text[:600] if getattr(e, "response", None) is not None else ""
        return f"Update failed: {e}\n{detail}"


@mcp.tool()
def change_material_view(entity: str, keys: dict, fields: dict | None = None,
                         operation: str = "update", service: str | None = None,
                         confirm: bool = False) -> str:
    """Change or add a keyed CHILD entity -- a material sub-view OR any object's child
    (PIR / cost condition / BOM / routing) on its own service.

    OData v2 has no deep update, so each child row is addressed by its OWN key:
        operation="update" -> PATCH the keyed row.
        operation="add"    -> POST a new child row (extend a plant / sales org / language).
        operation="delete" -> DELETE the keyed row.
    Key literals are formatted by their $metadata TYPE, so an Edm.DateTime key (e.g. a
    condition's ConditionValidityEndDate) is sent as datetime'..' -- not a string.

    NOTE: some child entities store the VALUE in the key (e.g. A_ProductSalesTax) -- change
    those by delete + add. ALWAYS discover the exact key + service first with
    explore_entity / list_fields; never guess them.

    SAFETY GATE: confirm=false (default) only PREVIEWS the exact request -- no write.

    Args:
        entity: child entity set, e.g. "A_ProductPlant", "A_PurInfoRecdPrcgCndnValidity".
        keys: the FULL key {field: value} of the row (from explore_entity / list_fields).
        fields: {ExactODataField: new_value} ("update": changed fields; "add": new-row fields).
        operation: "update" (default), "add", or "delete".
        service: OData service of the entity. DEFAULT API_PRODUCT_SRV (material views). For a
            NON-product object pass its service: API_INFORECORD_PROCESS_SRV (PIR),
            API_PURGPRCGCONDITIONRECORD_SRV (cost), API_BILL_OF_MATERIAL_SRV (BOM),
            API_PRODUCTION_ROUTING (routing).
        confirm: must be true to actually write.
    """
    entity = entity.strip().lstrip("/")
    op = operation.strip().lower()
    if op not in ("update", "add", "delete"):
        return "operation must be 'update', 'add', or 'delete'."
    root = f"{SAP_BASE_URL}{_service_path(service)}"

    if op == "add":
        url = f"{root}/{entity}"
        body = {**keys, **(fields or {})}    # new row carries its own keys
        verb = "POST"
    else:                                # update or delete -> address the row by key
        url = f"{root}/{entity}({_key_predicate(entity, keys, service)})"
        # Echo the key fields in the PATCH body too: some SAP services (e.g. the PIR
        # org-data) reject an update whose payload omits the keys (CM_MGW_RT/022 "check
        # key fields of URI and payload"). Matching keys are harmless for the rest.
        body = {**keys, **(fields or {})} if op == "update" else None
        verb = "PATCH" if op == "update" else "DELETE"

    if not confirm:
        body_str = "" if body is None else "\n" + json.dumps(body, indent=2, ensure_ascii=False)
        return ("PREVIEW -- nothing written. Confirm with the user, then call again "
                f"with confirm=true.\n{verb} {url}{body_str}")

    token = _csrf_for(root)
    if not token:
        return "Could not obtain a CSRF token from SAP (write blocked)."
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json",
               "Accept": "application/json"}
    if verb in ("PATCH", "DELETE"):      # etag-protected rows (condition records) need the
        try:                             # CURRENT etag in If-Match (SAP rejects "*").
            g = sap_session.get(url, params={"sap-client": SAP_CLIENT},
                                auth=(SAP_USER, SAP_PASS), headers={"Accept": "application/json"},
                                timeout=120, verify=False)
            etag = g.headers.get("ETag") or \
                (g.json().get("d", {}).get("__metadata", {}) or {}).get("etag")
        except Exception:                # noqa: BLE001
            etag = None
        headers["If-Match"] = etag or "*"
    try:
        if verb == "POST":
            resp = sap_session.post(url, params={"sap-client": SAP_CLIENT},
                                    auth=(SAP_USER, SAP_PASS), headers=headers,
                                    json=body, timeout=120, verify=False)
        elif verb == "DELETE":
            resp = sap_session.delete(url, params={"sap-client": SAP_CLIENT},
                                      auth=(SAP_USER, SAP_PASS), headers=headers,
                                      timeout=120, verify=False)
        else:
            resp = sap_session.patch(url, params={"sap-client": SAP_CLIENT},
                                     auth=(SAP_USER, SAP_PASS), headers=headers,
                                     json=body, timeout=120, verify=False)
            if resp.status_code == 405:        # some releases want the MERGE tunnel
                headers["X-HTTP-Method"] = "MERGE"
                resp = sap_session.post(url, params={"sap-client": SAP_CLIENT},
                                        auth=(SAP_USER, SAP_PASS), headers=headers,
                                        json=body, timeout=120, verify=False)
        resp.raise_for_status()
        tail = f" {resp.text[:300]}" if resp.text.strip() else ""   # POST echoes the row; PATCH is 204
        return f"{verb} {entity} OK (HTTP {resp.status_code})." + tail
    except requests.exceptions.RequestException as e:
        detail = e.response.text[:600] if getattr(e, "response", None) is not None else ""
        return f"{op} on {entity} failed: {e}\n{detail}"

# ---- coded-value lookup (reads code_book.json from codebook_extract.py) ------
_CODE_BOOK = None


def _load_code_book() -> dict:
    """Load code_book.json (next to this file, or the project root). Cached; {} if absent."""
    global _CODE_BOOK
    if _CODE_BOOK is None:
        here = os.path.dirname(__file__)
        for path in (os.path.join(here, "code_book.json"),
                     os.path.join(os.path.dirname(here), "code_book.json")):
            try:
                with open(path, encoding="utf-8") as f:
                    _CODE_BOOK = json.load(f)
                break
            except (FileNotFoundError, ValueError):
                continue
        if _CODE_BOOK is None:
            _CODE_BOOK = {}
    return _CODE_BOOK


@mcp.tool()
def list_allowed_values(field: str) -> str:
    """Allowed codes + texts for a coded material field, so you can map a user's
    word (e.g. 'phase-out') to a valid SAP code, and VALIDATE a code the user
    gives (e.g. reject '07' if it isn't in the list) BEFORE any write.

    Always call this for any coded field (status, type, group, industry, unit...).
    Never invent a code, and never write a code that isn't in the returned list.

    When presenting the list always provide in a tabular format with clear headings 
    with borders between rows and columns.
    Args:
        field: e.g. 'CrossPlantStatus', 'ProductType', 'ProductGroup',
               'IndustrySector', 'BaseUnit'. The DB field name (e.g. 'MSTAE') also works.
    """
    book = _load_code_book()
    if not book:
        return ("No code book found. Run codebook_extract.py once and place "
                "code_book.json at the project root or in mcp_server/.")
    by_odata = book.get("by_odata_field", {})
    by_db = book.get("by_db_field", {})
    key = next((k for k in by_odata if k.lower() == field.lower()), None)
    entry = by_odata.get(key) if key else None
    if entry is None:
        key = next((k for k in by_db if k.lower() == field.lower()), None)
        entry = by_db.get(key) if key else None
    if entry is None:
        avail = ", ".join(sorted(by_odata)) or "(none)"
        return f"'{field}' is not in the code book. Fields with value lists: {avail}."
    values = entry.get("values", [])
    if not values:
        return f"'{key}' (check table {entry.get('check_table')}) has no values in the code book."
    lines = [f"Allowed values for {key} (check table {entry.get('check_table')}):"]
    lines += [f"  {v['code']} = {v['text']}" if v.get("text") else f"  {v['code']}"
              for v in values]
    lines.append("Map the user's intent to one of these codes; reject anything not listed.")
    return "\n".join(lines)



if __name__ == "__main__":
    mcp.run(transport="stdio")