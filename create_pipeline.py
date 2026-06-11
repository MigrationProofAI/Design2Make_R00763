"""
create_pipeline.py — a Sequential "Design2Make" pipeline that prepares a material
create/update, built from three LlmAgent "Doers" run in a FIXED order:

        Intake  ->  ValidateEnrich  ->  Writer

This is the deterministic, referential-integrity part of the flow -> a SequentialAgent
(an "Arranger" of Doers). State passes between steps via ADK's output_key -> {key}.

Safety: writes are confirm-gated in sap.py (create_material / update_material take
confirm=false|true). The Writer PREVIEWS the change (confirm=false) and only commits
(confirm=true) when the user has explicitly authorised it. The review panel becomes
the standard authoriser at that gate.

Wiring: main.py keeps its websocket flow untouched (build_user_content + the
{message} reply). It just runs THIS pipeline as the agent instead of the single
chat agent:  root_agent = build_create_pipeline()
"""
import os
import sys

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset           # match your main.py's casing
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from knowledge import remember, knowledge_block   # shared memory layer

MODEL = LiteLlm(model=f"openai/{os.getenv('OPENAI_MODEL', 'gpt-4o-mini')}")
# The Writer actually mutates SAP -> stronger model (Intake/ValidateEnrich stay cheap).
WRITE_MODEL = LiteLlm(model=f"openai/{os.getenv('OPENAI_MODEL_WRITE', 'gpt-4o')}")


# Read-only SAP tools = everything EXCEPT the three writes. Search + ValidateEnrich
# get these; only the Writer gets the full set -- so ALL writes funnel through the gate.
READONLY_SAP_TOOLS = ["search_materials", "list_plants", "describe_search_fields",
                      "list_materials", "get_material", "query_sap", "explore_entity",
                      "build_material_payload", "list_allowed_values",
                      "find_field", "list_fields"]


def _sap_toolset(readonly: bool = False) -> MCPToolset:
    """The SAP MCP server over stdio. readonly=True exposes only read tools
    (no create_/update_/change_material) via tool_filter."""
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable, args=["./mcp_server/sap.py"],
            ),
            timeout=30,
        ),
        tool_filter=READONLY_SAP_TOOLS if readonly else None,
    )


def _serper_toolset() -> MCPToolset:
    """Web search (Serper/Google) over stdio -- lets ValidateEnrich look up product
    specs (net/gross weight, dimensions) that aren't on the image or in the request."""
    return MCPToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable, args=["./mcp_server/serper.py"],
            ),
            timeout=30,
        )
    )


def build_create_pipeline(before_model_callback=None) -> SequentialAgent:
    """Build the Intake -> ValidateEnrich -> Writer pipeline.

    NOTE: each tool-using agent gets its OWN MCPToolset instance. Sharing one
    instance across agents trips ADK's MCP session ContextVar ("created in a
    different Context"), so ValidateEnrich and Writer each build their own.
    """

    intake = LlmAgent(
        name="Intake",
        model=MODEL,
        instruction="""
You are the INTAKE step of a material create/change pipeline. The request may arrive as
text, an IMAGE (e.g. a product box / label / spec sheet), or transcribed audio. Do NOT
call any tool and do NOT write anything — only read the request and produce a brief.

If an IMAGE is present, READ it and extract what you can SEE: product name/description,
brand, any visible material number or codes, and type hints (e.g. "trading good",
"processor" -> raw/finished). State the attributes you extracted.

Decide the OPERATION:
  - "change" if they refer to an existing material (an ID is given, or words like
    change / set / update / extend / mark).
  - "create" if they want a new material.

Capture the fields stated/seen using plain labels (type, base unit, industry,
description, group, plant, sales org, ...). For a change, capture the material ID. Note
whether the user explicitly authorised writing (e.g. "create it", "change it", "go ahead").

Output exactly this and nothing else:
operation: create | change
material_id: <id or blank>
authorised_to_write: yes | no
fields:
  <label>: <value>
""",
        output_key="intake",
    )

    validate = LlmAgent(
        name="ValidateEnrich",
        model=MODEL,
        tools=[_sap_toolset(readonly=True), _serper_toolset(), remember],
        instruction="""
You are the VALIDATE/ENRICH step. READ-ONLY — never write. The intake brief is:
{intake}

1. If operation is "change", call get_material(material_id) to confirm it exists and to
   learn the EXACT OData field names + current values.
2. Resolve EVERY coded value to a valid CODE via list_allowed_values(field) — e.g.
   ProductType, ProductGroup, IndustrySector, BaseUnit, CrossPlantStatus. NEVER invent a
   code; if nothing matches, record it under "issues".
3. Decide whether a plant view and/or sales view are wanted (the user said it is sold /
   stocked in a plant / in a country).
4. WEB ENRICH (only if asked): if the user wants a physical/spec attribute that is NOT
   given or visible — net weight, gross weight, dimensions — call google_search for THIS
   exact product (e.g. "AMD Ryzen 7 9800X3D net weight kg") and use the value found. Put
   it in material/changes with its unit (e.g. NetWeight + WeightUnit "KG"). A successful
   web lookup is NOT an "issue" — keep issues "none" and just put the value in changes.
   NEVER fabricate a spec.
5. FIELD NAMES: when mapping a user's words to an OData field in "changes", FIRST consult
   the LEARNED KNOWLEDGE below for the right field name (e.g. size/dimensions ->
   SizeOrDimensionText). If it's NOT there, call find_field(term) -- it searches the live
   $metadata labels (e.g. find_field("minimum order quantity") -> MinimumOrderQuantity).
   Use the resolved field; NEVER guess a field name.

Output ONLY this JSON and nothing else (no prose, no code fences):
{
  "operation": "create" | "change",
  "material_id": "<id or empty>",
  "authorised_to_write": "yes" | "no",
  "material": {
     "Description": "...", "ProductType": "<code>", "BaseUnit": "<code>",
     "ProductGroup": "<code>", "Plant": "<code or empty>", "SalesOrg": "<code or empty>"
  },
  "changes": [ {"field": "<ODataField or sub-view>", "value": "<new value>"} ],
  "issues": "<missing/ambiguous items, or none>"
}
For a CREATE fill "material" (changes empty); for a CHANGE fill "changes" + material_id
(material may be partial). Decimals as strings.
""" + knowledge_block(),
        output_key="validated",
    )

    writer = LlmAgent(
        name="Writer",
        model=WRITE_MODEL,
        tools=[_sap_toolset(), remember],
        instruction="""
You are the WRITER step. The validated plan is:
{validated}

In order:
- If "issues" is anything other than "none": STOP. Report the issues and ask the user to
  resolve them. Do NOT call any write tool.

CREATE (operation == "create"):
  1. Call build_material_payload with the material fields — do NOT hand-build the payload:
       build_material_payload(description=<Description>, product_type=<ProductType>,
         base_unit=<BaseUnit>, product_group=<ProductGroup>,
         plant=<Plant if set>, sales_org=<SalesOrg if set>)
     It returns {"fields": {...}} with the verified defaults baked in (valuation class by
     material type; a sales view auto-carries the complete tax set).
  2. create_material(fields=<the returned fields>, confirm=false) — show the preview.
  3. If authorised_to_write == "yes": create_material(fields=..., confirm=true) and report
     the NEW material number. Else stop after the preview and ask the user to confirm.

CHANGE (operation == "change"): for each entry in "changes", pick the right tool:
  * a HEADER field (GrossWeight, ProductStandardID/GTIN, ProductGroup, CrossPlantStatus…):
      update_material(material_id, fields={<ODataField>: <value>}, confirm=…)
  * a SUB-VIEW (description text, plant field, sales, tax):
      change_material_view(entity, keys, fields, operation, confirm=…)
      e.g. change a description -> entity "A_ProductDescription",
           keys {"Product": <id>, "Language": "EN"}, fields {"ProductDescription": <value>}.
  Preview with confirm=false first; show it; then confirm=true ONLY if authorised.

NEVER call a write tool with confirm=true unless authorised_to_write == "yes".

LEARNING: if a write fails with an invalid-field / invalid-key / unknown-code error and a
corrected field name or code THEN succeeds (or the user corrected you), call
remember("<the correct mapping>") so it persists across sessions -- e.g.
remember("The size/dimensions field is SizeOrDimensionText, not Dimensions.").
""" + knowledge_block(),
        output_key="result",
    )

    if before_model_callback is not None:                # keep long create/change sessions
        for a in (intake, validate, writer):             # under the model context window
            a.before_model_callback = before_model_callback
    return SequentialAgent(name="CreatePipeline", sub_agents=[intake, validate, writer])
