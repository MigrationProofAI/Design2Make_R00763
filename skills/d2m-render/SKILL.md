---
name: d2m-render
description: >
  Render ANY D2M structured result on screen - material, BOM, cost, PIR (purchasing info
  record), routing, MRP / planning, or a free-form answer to a user query - from the
  producing tool or agent's structured JSON. Deterministic: no language model runs at render
  time. The model emits structured data; this renders it consistently across every surface.
  Use whenever a result needs to be SHOWN to a user on screen (file exports are a separate
  deterministic path).
---

# D2M render

## The one rule
Every view in D2M = **structured data -> deterministic render.** A tool or the model produces
structured output in a known contract; rendering is pure code, model-free. The model NEVER
hand-draws UI or hand-formats a table in prose - it returns data, and a renderer draws it.
This keeps presentation uniform across sessions, and lets you swap the model (GPT-4, a
reasoning model, none) without changing how anything looks.

`SKILL.md` is the spec. The render components are the code. This file is authoring-time
guidance, not a runtime dependency - nothing here "runs" in production.

## Why one skill for everything
Material, BOM, cost, PIR, routing, MRP, and a free-form answer all reuse the SAME small set of
primitives. Define them once; each object type is then just a data contract + which primitives
it uses + its semantic rules. A new object type later is a new contract section, same
primitives - not a new rendering approach.

## Shared render primitives (the vocabulary)
- **header slab** - object identity: what, where (plant/org), status, timestamp.
- **key-value table** - flat field/value pairs (material attributes, cost lines, PIR terms).
- **stat tiles** - a few headline numbers (run deltas, totals, counts).
- **cascade / tree** - hierarchical rows indented by level (BOM explosion, MRP cascade). One
  primitive, reused by both.
- **badge** - a typed chip (material type, procurement, status, BOM usage).
- **status banner** - success / error / empty. Never render a failed result with success styling.
- **semantic colours** - blue = in-house / planned order; amber = external / purchase req;
  green = success; red = error. Keep these constant across every object type.
- **progressive disclosure** - show a salient field SUBSET by default; expand two ways:
  (a) a deterministic picker populated from the grounding code-book (real fields + labels only,
  never invented), and (b) a natural-language ask ("show MRP fields") mapped and validated
  against that same code-book. Fetch generously; display selectively. The shown-field set is
  session state, mutated by both paths, read by the renderer.

## Per-object contracts and rules
For each: the shape the producing tool returns, the primitives to render it with, and the
semantic rules. MRP's contract below is exact and proven; the others are the SHAPE to fill from
each tool's real output - keep them grounded in what the tool actually returns, never invented.

### Material
data: `{ material, type (FERT/HALB/HAWA/ROH), description, plant, salesOrg, baseUnit, ... }`
render: header slab + key-value table; a type badge. Default subset = identity + type + plant;
the long attribute list expands via progressive disclosure.

### BOM
data: `{ parent, plant, alternative, items: [ {component, type, qty, unit, level}, ... ] }`
render: header slab + cascade/tree (parent at level 0, components indented by level); type badge
per row. Same tree primitive as MRP.

### Cost
data: `{ material, plant, currency, total, components: [ {component, qty, unitCost, rolledCost}, ... ] }`
render: header slab + stat tile (total) + key-value table of component costs; show the roll-up
(component costs summing into the finished price). Money right-aligned.

### PIR (purchasing info record)
data: `{ material, vendor, purchOrg, plant, price, currency, ... }`
render: header slab + key-value table; vendor and price prominent.

### Routing
data: `{ material, plant, group, counter, operations: [ {op, workCentre, description, ... }, ... ] }`
render: header slab + an ordered list/table of operations in sequence; group/counter in the header.

### MRP / planning  (exact, proven contract)
data:
```json
{ "material","plant","status","multiLevel","timestamp","message",
  "run": { "purchaseReqsCreated","plannedOrdersCreated","plannedOrdersDeleted","errors" },
  "materials": [ {"mat","type","label","level","proc","output","exception"}, ... ] }
```
render: header slab + stat tiles (the four `run` deltas) + cascade/tree of `materials` +
exceptions footer. `proc` E/X -> `planned_order` (blue); F -> `purchase_req` (amber); mark a row
when `exception` is true. Reference implementation: `mrp_result_view.html`, `renderMrpResult(data, el)`.

### User query (free-form answer)
Not every response is a typed object. When the agent answers a question, it should still return
`{ answer: "<markdown/text>", refs?: [...] }`, and the renderer shows the answer in a plain prose
block - NOT a fake object card. Render text as text; do not force a query answer into a structured
card. (And per the planning traces: when a tool already returns the data, render THAT - do not
send the agent field-hunting to re-derive what it already has.)

## Empty / failed states (every object)
- `status` E/A, or errors present: surface the message; do not render success styling.
- empty collection (no BOM items, no operations, no `materials` rows): say so explicitly; never
  show an empty card as if it were complete.

## Components
`mrp_result_view.html` is the worked reference - a pure `renderMrpResult(data, el)`. Each other
view is a sibling pure function over its own contract, reusing the primitives above. The only
thing to change for D2M's brand is the font stack; everything else is the contract and rules here.

## Suggested placement
```
<repo>/skills/d2m-render/SKILL.md            <- this file (supersedes the MRP-only draft)
<repo>/skills/d2m-render/mrp_result_view.html <- reference component
```
Under GPT-4 (or any runtime) you ship the components and use this file as the spec; if Claude
Code works the app, it reads the skill directly.
