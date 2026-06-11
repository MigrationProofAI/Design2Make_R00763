"""
verdict.py — the DETERMINISTIC half of the rule engine.

The LLM checks each rule and reports a finding (rule_id + pass/violated). This
module does the parts the LLM must NOT do, because they are facts and logic, not
judgement:
  1. read each rule's declared SEVERITY from rules.md (the source of truth), and
  2. aggregate the findings into one verdict by severity.

This is what fixes the "BLOCK when it should be CONFIRM" bug: the LLM no longer
gets to decide (or mislabel) severity, and the verdict is a pure function.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

RULES_FILE = Path(__file__).parent / "rules.md"


@dataclass
class Finding:
    rule_id: str
    status: str            # "pass" | "violated"
    message: str = ""


def load_rule_severities(path: Path = RULES_FILE) -> dict[str, str]:
    """rule_id -> 'error' | 'warning', read from rules.md. The LLM is never the
    authority on severity; this file is."""
    sev: dict[str, str] = {}
    rid = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        m = re.match(r"-\s*id:\s*(\S+)", s)
        if m:
            rid = m.group(1)
            continue
        m = re.match(r"severity:\s*(\w+)", s)
        if m and rid:
            sev[rid] = m.group(1).lower()
    return sev


@dataclass
class Verdict:
    verdict: str                         # PASS | CONFIRM | BLOCK
    errors: list = field(default_factory=list)     # [(rule_id, message)]
    warnings: list = field(default_factory=list)


def compute_verdict(findings: list[Finding],
                    severities: dict[str, str] | None = None) -> Verdict:
    """error blocks; warning needs confirmation; otherwise pass. Pure function."""
    if severities is None:
        severities = load_rule_severities()
    errors, warnings = [], []
    for f in findings:
        if f.status.lower() != "violated":
            continue
        sev = severities.get(f.rule_id, "error")  # unknown rule -> fail safe (block)
        (errors if sev == "error" else warnings).append((f.rule_id, f.message))
    verdict = "BLOCK" if errors else "CONFIRM" if warnings else "PASS"
    return Verdict(verdict=verdict, errors=errors, warnings=warnings)


if __name__ == "__main__":
    # Reproduce EXACTLY the case that wrongly returned BLOCK:
    findings = [
        Finding("R001", "violated", "Net weight (400) is greater than gross weight (350)."),
        Finding("R002", "pass"),
        Finding("R003", "pass"),
        Finding("R004", "violated", "Description 'Brk' is under 5 characters."),
        Finding("R005", "pass"),
        Finding("R006", "pass"),
    ]
    sev = load_rule_severities()
    print("severities from rules.md:", sev)
    v = compute_verdict(findings, sev)
    print("\nVERDICT:", v.verdict)
    print("errors:  ", v.errors or "none")
    print("warnings:", v.warnings or "none")
