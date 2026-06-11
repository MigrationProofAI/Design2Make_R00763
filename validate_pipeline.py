"""
validate_pipeline.py — Design2Make material RULE ENGINE + Stage 4 critique loop.

  Derive                         {derived}      JSON {material, derivations}
    |
  Parallel(
    CodeCheck                    {code_check}   JSON findings
    Loop(                                          <-- STAGE 4: critique & refine
      PolicyCheck                {policy_check} JSON findings (the Doer)
      Critic   -> exit_loop  (clean)   |  {critic_feedback}  (redo with a note)
    )  max_iterations = 3
  )

The Critic forces PolicyCheck to cover EVERY applicable validation rule (with a
reason) before the loop is allowed to exit. rule_engine_glue captures
PolicyCheck's FINAL (loop-refined) findings; verdict.py reads severity from
rules.md and computes the verdict. No LLM decides severity or the verdict.
"""
import os
import sys
from pathlib import Path

from google.adk.agents import LlmAgent, LoopAgent, ParallelAgent, SequentialAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.tool_context import ToolContext
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset   # match your main.py's casing
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

MODEL = LiteLlm(model=f"openai/{os.getenv('OPENAI_MODEL', 'gpt-4o-mini')}")
RULES_FILE = Path(__file__).parent / "rules.md"


def _load_rules() -> str:
    try:
        return RULES_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "# (rules.md not found next to validate_pipeline.py -- no customer rules loaded)"


def _sap_toolset() -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable, args=["./mcp_server/sap.py"],
            ),
            timeout=30,
        )
    )


def exit_loop(tool_context: ToolContext) -> dict:
    """Stop the critique loop. Call this ONLY when the findings are complete --
    every applicable validation rule is covered and every finding has a reason."""
    tool_context.actions.escalate = True
    return {"status": "approved", "loop": "exited"}


def build_validation_pipeline(sap_tools: McpToolset | None = None) -> SequentialAgent:
    if sap_tools is None:
        sap_tools = _sap_toolset()
    rules = _load_rules()

    # ---- 1. DERIVE: enrich the material; emit STRUCTURED JSON ----
    derive = LlmAgent(
        name="Derive",
        model=MODEL,
        instruction=f"""
You apply the DERIVATION rules (type: derivation) from the rule set below to the
proposed material in the user's message. A derivation ADDS or EXTENDS fields when
its 'when' condition holds. Apply ONLY matching derivation rules; never add a
field no rule asks for; never overwrite the user's own values.

Output ONLY a JSON object and nothing else -- no prose, no code fences. Exactly
two keys:
  material     -> an object of the material's fields: the user's fields PLUS what
                  the derivations added. Use clear names such as ProductType,
                  BaseUnit, GrossWeight, NetWeight, GrossWeightUnit, NetWeightUnit,
                  IndustrySector, CrossPlantStatus, ProductGroup, Description,
                  SalesOrganization, DistributionChannel, Plant. Omit empty fields.
  derivations  -> a list of objects, each with keys rule_id and added (what that
                  rule contributed). Empty list if none fired.

RULES:
---
{rules}
---
""",
        output_key="derived",
    )

    # ---- 2a. CODECHECK: domain validity on the material (skip absent fields) ----
    code_check = LlmAgent(
        name="CodeCheck",
        model=MODEL,
        tools=[sap_tools],
        instruction="""
You validate the CODED fields of the proposed material. The material is the
'material' object of:
{derived}

Only consider coded fields that have an ACTUAL VALUE. If a field is absent or
empty, SKIP it -- do NOT emit a finding (a missing field is the mandatory check's
concern). For each coded field that HAS a value (ProductType, ProductGroup,
IndustrySector, CrossPlantStatus, BaseUnit), call list_allowed_values(field) and
check the value is in the returned list. NEVER guess. An invalid value is a
violation; an absent field is NOT.

Report ONLY a JSON array and nothing else -- no prose, no code fences. Each
element has keys: rule_id (the field name), status (pass or violated), message.
The message is REQUIRED for BOTH pass and violated and must name the field and
its actual value, e.g. "EA is a valid base unit" or "status 07 is not in the
allowed list". One element per coded field you actually checked.
""",
        output_key="code_check",
    )

    # ---- 2b. POLICYCHECK: the DOER inside the critique loop ----
    policy_check = LlmAgent(
        name="PolicyCheck",
        model=MODEL,
        instruction=f"""
You apply the VALIDATION rules (type: validation) from the rule set below to the
proposed material. The material is the 'material' object of:
{{derived}}

If a reviewer left feedback on your PREVIOUS attempt, fix exactly what it lists
(add the missing rule findings, add the missing reasons). Feedback (may be empty
on the first attempt):
{{critic_feedback?}}

For each validation rule whose condition applies, decide pass or violated. Do NOT
judge coded-value validity (CodeCheck's job). Do NOT decide severity -- that is
read from the rule file later; you only report status.

Report ONLY a JSON array and nothing else -- no prose, no code fences. Each
element has keys: rule_id (e.g. R001), status (pass or violated), message. The
message is REQUIRED for BOTH pass and violated and must state what was checked
with the ACTUAL values, e.g. "net 400 <= gross 450" or "description 'Brk' is 3
chars (< 5)". One element per applicable validation rule.

RULES:
---
{rules}
---
""",
        output_key="policy_check",
    )

    # ---- 2b-critic: judge completeness; approve (exit_loop) or send a redo note ----
    critic = LlmAgent(
        name="Critic",
        model=MODEL,
        tools=[exit_loop],
        instruction=f"""
You REVIEW the findings produced by PolicyCheck:
{{policy_check}}

against the validation rules below and the material (the 'material' object of):
{{derived}}

A finding is REQUIRED for EVERY validation rule whose condition applies to this
material, and every finding must carry a reason (message) with the actual values.

- If the findings are COMPLETE (no applicable rule is missing) and every finding
  has a reason: call the exit_loop tool and output nothing else.
- Otherwise: do NOT call the tool. Output ONE or TWO lines naming exactly which
  rule_ids are missing, or which findings lack a reason. This note is fed back to
  PolicyCheck to fix on the next attempt.

RULES:
---
{rules}
---
""",
        output_key="critic_feedback",
    )

    # STAGE 4: PolicyCheck + Critic run in a cycle until clean or max_iterations.
    policy_loop = LoopAgent(
        name="PolicyCritiqueLoop",
        sub_agents=[policy_check, critic],
        max_iterations=3,
    )

    # CodeCheck runs in parallel with the whole critique loop.
    checks = ParallelAgent(name="Validate", sub_agents=[code_check, policy_loop])
    return SequentialAgent(name="MaterialRuleEngine", sub_agents=[derive, checks])