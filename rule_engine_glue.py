"""
rule_engine_glue.py — assembles ONE structured result from the pipeline, computes
the verdict (verdict.py), and builds a COMPLETE rule-execution audit log.

The audit is driven by rules.md (read in code), not by the model, so every rule
is accounted for. A rule the model silently skipped shows up as "not reported".

assemble_result(...) returns the object a review panel consumes:
    { verdict, material, derivations, errors, warnings, audit }
render_markdown(...) is the chat rendering for now.
"""
import json
import re
from pathlib import Path

from verdict import Finding, compute_verdict

RULES_FILE = Path(__file__).parent / "rules.md"


def _strip_fences(text: str) -> str:
    return text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()


def _extract_json_array(text) -> list:
    if isinstance(text, list):
        return text
    if not isinstance(text, str) or not text.strip():
        return []
    s = _strip_fences(text)
    i, j = s.find("["), s.rfind("]")
    if i == -1 or j == -1 or j <= i:
        return []
    try:
        data = json.loads(s[i:j + 1])
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def _extract_json_object(text) -> dict:
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        return {}
    s = _strip_fences(text)
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1 or j <= i:
        return {}
    try:
        data = json.loads(s[i:j + 1])
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def parse_findings(*blobs) -> list:
    findings = []
    for blob in blobs:
        for item in _extract_json_array(blob):
            if isinstance(item, dict):
                findings.append(Finding(
                    rule_id=str(item.get("rule_id", "")),
                    status=str(item.get("status", "")),
                    message=str(item.get("message", "")),
                ))
    return findings


def load_rules_index(path: Path = RULES_FILE) -> list:
    """Every rule from rules.md, in order: {id, type, severity}. The model never
    sees this list -- it's the code's source of truth for 'did each rule run'."""
    rules, cur = [], None
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        m = re.match(r"-\s*id:\s*(\S+)", s)
        if m:
            cur = {"id": m.group(1), "type": "", "severity": ""}
            rules.append(cur)
            continue
        if cur:
            mt = re.match(r"type:\s*(\w+)", s)
            if mt:
                cur["type"] = mt.group(1).lower()
            ms = re.match(r"severity:\s*(\w+)", s)
            if ms:
                cur["severity"] = ms.group(1).lower()
    return rules


def build_audit(rules_index: list, derivations: list, findings: list) -> list:
    """For EVERY rule in rules.md, what did the model do with it -- and WHY?"""
    applied = {d.get("rule_id"): d for d in derivations if isinstance(d, dict)}
    finding_by_id = {f.rule_id: f for f in findings}
    audit = []
    for r in rules_index:
        rid, rtype = r["id"], r["type"]
        if rtype == "derivation":
            d = applied.get(rid)
            audit.append({"rule_id": rid, "type": "derivation",
                          "status": "applied" if d else "not reported",
                          "detail": (d.get("added", "") if d else "")})
        else:  # validation (or unspecified)
            f = finding_by_id.get(rid)
            audit.append({"rule_id": rid, "type": rtype or "validation",
                          "status": f.status if f else "not reported",
                          "detail": (f.message if f else "")})
    return audit


def assemble_result(derived_blob, code_blob, policy_blob) -> dict:
    derived = _extract_json_object(derived_blob)
    material = derived.get("material", {}) if isinstance(derived.get("material"), dict) else {}
    derivations = derived.get("derivations", []) if isinstance(derived.get("derivations"), list) else []
    findings = parse_findings(code_blob, policy_blob)
    verdict = compute_verdict(findings)
    return {
        "verdict": verdict.verdict,
        "material": material,
        "derivations": derivations,
        "errors": [{"rule_id": r, "message": m} for r, m in verdict.errors],
        "warnings": [{"rule_id": r, "message": m} for r, m in verdict.warnings],
        "audit": build_audit(load_rules_index(), derivations, findings),
    }


def render_markdown(result: dict) -> str:
    lines = [f"**Verdict: {result['verdict']}**"]
    if result["errors"]:
        lines.append("\nErrors (block the write):")
        lines += [f"- {e['rule_id']}: {e['message']}" for e in result["errors"]]
    if result["warnings"]:
        lines.append("\nWarnings (confirm to proceed):")
        lines += [f"- {w['rule_id']}: {w['message']}" for w in result["warnings"]]
    if not result["errors"] and not result["warnings"]:
        lines.append("\nAll checks passed.")
    if result["material"]:
        lines.append("\n**Material**")
        lines += [f"- {k}: {v}" for k, v in result["material"].items()]
    if result["derivations"]:
        lines.append("\n**Auto-derived**")
        lines += [f"- {d.get('rule_id', '')}: {d.get('added', '')}" for d in result["derivations"]]
    if result.get("audit"):
        lines.append("\n**Rule execution log**")
        for a in result["audit"]:
            why = f" \u2014 {a['detail']}" if a.get("detail") else ""
            lines.append(f"- {a['rule_id']} ({a['type']}): {a['status']}{why}")
    return "\n".join(lines)


async def run_rule_engine(runner, session_service, app_name: str,
                          session_id: str, content) -> str:
    """Capture each Doer's text from the EVENT STREAM (by author), fall back to
    session state, assemble + render, and append a one-line in-CHAT diagnostic so
    we can see exactly what was captured. Delete the diag block once it's clean."""
    captured: dict = {}
    counts: dict = {}          # how many times each agent emitted text
    async for event in runner.run_async(
        session_id=session_id, user_id=session_id, new_message=content
    ):
        author = getattr(event, "author", "") or ""
        ec = getattr(event, "content", None)
        parts = getattr(ec, "parts", None) if ec else None
        if parts:
            text = "".join((getattr(p, "text", "") or "") for p in parts)
            if text.strip():
                captured[author] = text  # last non-empty text per author wins
                counts[author] = counts.get(author, 0) + 1

    state = {}
    try:
        session = await session_service.get_session(
            app_name=app_name, user_id=session_id, session_id=session_id
        )
        state = session.state if session else {}
    except Exception as e:  # noqa: BLE001
        print("[rule_engine] get_session failed:", e)

    derived = captured.get("Derive") or state.get("derived")
    code = captured.get("CodeCheck") or state.get("code_check")
    policy = captured.get("PolicyCheck") or state.get("policy_check")

    result = assemble_result(derived, code, policy)
    rendered = render_markdown(result)

    # ---- TEMPORARY in-chat diagnostic (remove once Material + why are flowing) ----
    sample = parse_findings(code, policy)
    sample = sample[0] if sample else None
    diag = ["", "---", "_diag (temporary):_"]
    diag.append(f"- authors captured: {list(captured.keys())}")
    if result["material"]:
        diag.append(f"- derived parsed: yes ({len(result['material'])} fields, "
                    f"{len(result['derivations'])} derivations)")
    else:
        diag.append(f"- derived parsed: NO -> raw: `{str(derived or '<empty>')[:120]}`")
    if sample:
        diag.append(f"- sample finding: {sample.rule_id} = {sample.status} "
                    f"| message=`{(sample.message or '<blank>')[:60]}`")
    else:
        diag.append("- sample finding: none parsed")
    critic_note = captured.get("Critic")
    diag.append(f"- critic: {('note -> ' + critic_note.strip()[:80]) if (critic_note and critic_note.strip()) else 'approved on first pass (exit_loop)'}")
    diag.append(f"- loop: ran {counts.get('PolicyCheck', 0)} iteration(s); "
                f"critic sent {counts.get('Critic', 0)} note(s) before approving")
    return rendered + "\n".join(diag)