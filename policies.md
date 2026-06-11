# Design2Make — Assurance Policies

Customer **policy** artifact: the organisation's standard for what is *acceptable*, which the
review board judges objects **against**. Distinct from `rules.md` (deterministic *constraints* of
the data model). Like rules.md it lives outside the SAP core (clean core).

**Facts by code, judgment by the board.** The machine-readable values live in `policies.json`;
`mcp_server/assurance.py` runs them as **deterministic checks** over the *real* created objects
(material, BOM, PIRs/vendors, cost) and emits **findings**. The board does not invent a standard
and does not re-derive facts — it interprets the findings and decides: accept (with rationale),
find an alternate, or escalate. Edit `policies.json` to change the standard; no code change.

## Policies (mirrors policies.json)

- **P-CoO — Country of origin.** Every material must have a country of origin. A CoO in a
  **restricted region** (`RU, BY, IR, KP, SY, CU`) is an **error** (blocks). An **elevated-review**
  region (`CN, HK`) is a **warning** (needs review). Missing CoO is an **error** — a sellable/
  procured object whose origin is unknown cannot pass trade compliance.
  _The deterministic check reads each component's `CountryOfOrigin` (and, where present, the
  sourcing vendor's country via its PIR) — it is never left to a model to "remember to look"._

- **P-SRC — Sourcing concentration.** No single vendor should supply more than **60%** of the
  bought components (warning), and a vendor on **5+** components is surfaced for review. A bought
  component with **no PIR / no source of supply** is an **error**.

- **P-MD — Master-data completeness.** Every material needs `ProductType`, `BaseUnit`,
  `ProductGroup`; a FERT additionally needs `CountryOfOrigin`. Missing → **error**.

- **P-WT — Weights.** Net ≤ gross (warning); both ≥ 0 (error); matching weight units (error).
  _(Mirrors rules.md R001/R002/R006 — applied here as code over the real object.)_

- **P-ST — Status.** A FERT left in CrossPlantStatus `01` (Design) is a **warning** (a sellable
  good in Design is usually a setup mistake — rules.md R005).

- **P-CST — Cost.** A bought component with a zero cost condition is a **warning** (price likely
  missing). An optional `max_unit_cost` ceiling can elevate outliers.

## How the board uses a finding
A finding is `{check, object, fact, against (policy/rule), severity, verdict}`. The verdict is the
deterministic disposition (pass / fail / review). The board adds the *judgment*: an `error` finding
on a restricted-region CoO is an **escalate**; an elevated-region warning may be **accept with
rationale** or **find an alternate vendor**; a sourcing-concentration warning is a re-genesis
candidate (swap supplier). Severity comes from policy, not from the model.
