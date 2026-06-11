"""knowledge.py — the shared, cross-cutting memory layer.

A curated, always-loaded knowledge base (knowledge.md) that ANY pipeline can:
  - READ  via knowledge_block()  -> inject confirmed facts into an agent's instruction
  - WRITE via remember(lesson)    -> append a newly-confirmed lesson (the learning loop)

Deliberately simple + file-based so the knowledge is transparent and editable (same
spirit as rules.md / code_book.json). Graduate to a vector store + recall() tool when
this file grows large.
"""
from pathlib import Path

KNOWLEDGE_FILE = Path(__file__).parent / "knowledge.md"


def load_knowledge() -> str:
    """Return the current knowledge base text (or a placeholder if absent)."""
    try:
        return KNOWLEDGE_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "(no learned knowledge yet)"


def knowledge_block() -> str:
    """A block to append to an agent's instruction: the learned facts + the rule to
    write new ones back. Concatenated (not f-string) so ADK {placeholders} survive."""
    return (
        "\n\nLEARNED KNOWLEDGE — apply these confirmed facts. If you CONFIRM a new "
        "non-obvious fact (a correct OData field name, a code, or a business rule) — "
        "especially right after fixing an error — call remember(lesson) so future "
        "sessions reuse it.\n---\n" + load_knowledge() + "\n---\n"
    )


def remember(lesson: str) -> dict:
    """Append a durable lesson to the knowledge base for future sessions to reuse.

    Use for a confirmed, non-obvious fact worth keeping: a correct field name, a code,
    a business rule, an enrichment trick. Keep it ONE crisp line. Do NOT record secrets,
    one-off values, or anything already in the knowledge base.

    Args:
        lesson: the single-line lesson, e.g.
                "Size/dimensions field is SizeOrDimensionText (not SizeDimension)."
    """
    line = " ".join(lesson.split()).strip()
    if not line:
        return {"status": "skipped", "reason": "empty lesson"}
    existing = load_knowledge().lower()
    if line.lower() in existing:
        return {"status": "already-known", "lesson": line}
    with KNOWLEDGE_FILE.open("a", encoding="utf-8") as f:
        f.write(f"- {line}\n")
    return {"status": "remembered", "lesson": line}
