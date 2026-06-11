"""
dump_codebook.py — read code_book.json and print/write the full field listing.

No SAP connection needed; it just reads the JSON that codebook_extract.py produced.

Usage:
    python dump_codebook.py                   # print the full report to the screen
    python dump_codebook.py --index           # just field -> check table -> count
    python dump_codebook.py --out fields.txt  # also write the report to a file
    python dump_codebook.py path\\to\\code_book.json
"""
import argparse
import json
import os
import sys

# Windows consoles default to cp1252 and choke on unit texts like °C / µA / Ω.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def find_codebook(explicit=None):
    """Return the first code_book.json we can find (next to script, app root, mcp_server)."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [explicit] if explicit else [
        "code_book.json",
        os.path.join("mcp_server", "code_book.json"),
        os.path.join(here, "code_book.json"),
        os.path.join(here, "mcp_server", "code_book.json"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise SystemExit("code_book.json not found. Pass its path, e.g.\n"
                     "  python dump_codebook.py path\\to\\code_book.json")


def fmt_values(values, indent="  "):
    """One aligned 'code   text' line per value."""
    width = max((len(v.get("code", "")) for v in values), default=0)
    return [f"{indent}{v.get('code','').ljust(width)}  {v.get('text','')}".rstrip()
            for v in values]


def build_report(book, index_only=False):
    od = book.get("by_odata_field", {})
    db = book.get("by_db_field", {})

    out = [
        f"# Code book — {book.get('_system', '?')}  "
        f"(language {book.get('_language', '?')}, generated {book.get('_generated', '?')})",
        f"# {len(od)} bridged (agent-ready) fields, {len(db)} coded fields in the catalog",
        "",
        "=" * 72,
        "BRIDGED (agent-ready) FIELDS  — what list_allowed_values resolves",
        "=" * 72,
    ]
    for f, e in od.items():
        vals = e.get("values", [])
        out.append(f"\n{f}  [{e.get('db_field')} -> {e.get('check_table')}]  ({len(vals)} values)")
        if not index_only:
            out += fmt_values(vals)

    out += ["", "=" * 72, f"FULL CATALOG  — {len(db)} coded fields", "=" * 72]

    # Don't re-print a value set we've already shown. Seed with the bridged tables
    # so unit fields etc. point back to the bridged section instead of repeating 412 units.
    seen = {}
    for f, e in od.items():
        ct = e.get("check_table")
        if ct and ct not in seen:
            seen[ct] = f + " (bridged, above)"

    for f, e in sorted(db.items()):
        ct, vals = e.get("check_table"), e.get("values", [])
        if index_only:
            out.append(f"{f:<18} {str(ct):<14} {len(vals):>4} values")
        elif ct in seen:
            out.append(f"\n{f}  [-> {ct}]  ({len(vals)} values)  -> same set as {seen[ct]}")
        else:
            seen[ct] = f
            out.append(f"\n{f}  [-> {ct}]  ({len(vals)} values)")
            out += fmt_values(vals)

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Dump code_book.json as a readable field listing.")
    ap.add_argument("input", nargs="?", help="path to code_book.json (optional)")
    ap.add_argument("--index", action="store_true", help="fields -> table -> count only (no values)")
    ap.add_argument("--out", metavar="FILE", help="also write the report to a file")
    args = ap.parse_args()

    with open(find_codebook(args.input), encoding="utf-8") as fh:
        book = json.load(fh)

    report = build_report(book, index_only=args.index)
    print(report)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"\n[written to {args.out}]")


if __name__ == "__main__":
    main()