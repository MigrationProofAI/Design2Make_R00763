# Design2Make — Business Rules
#
# Customer policy artifact. NOT in the SAP backend (clean core). The rule-engine
# pipeline reads this file: it APPLIES derivation rules (to enrich the payload)
# and CHECKS validation rules (to grade it). check_rules.py reads it to ensure
# the rules don't conflict with each other.
#
# One rule per block. Fields:
#   id        stable identifier (so the agent can report which rule it applied)
#   type      validation  -> checks the data; produces a finding
#             derivation  -> expands the data; adds/extends fields
#   scope     what the rule is about (weights, status, distribution, defaults...)
#   when      (optional) condition under which the rule applies
#   severity  (validation only) error -> blocks; warning -> needs confirmation
#   rule      (validation) the check, in plain language
#   action    (derivation) what to add/extend when the condition holds
#   why       (optional) rationale / where it comes from
#
# The content is also valid YAML, so you can parse it later — but the agent just
# reads it as text and reasons over it.

# ---------- VALIDATION rules (check the data) ----------

- id: R001
  type: validation
  scope: weights
  severity: warning
  rule: Net weight must be less than or equal to gross weight.
  why: SAP treats this as a warning, not an error, so it commits — this surfaces it before the write and lets a stricter deployment elevate it.

- id: R002
  type: validation
  scope: weights
  severity: error
  rule: Gross weight and net weight must each be greater than or equal to zero.

- id: R003
  type: derivation
  scope: defaults
  when: operation = create AND IndustrySector is empty
  action: Set IndustrySector to M (Mechanical engineering).

- id: R004
  type: validation
  scope: naming
  when: ProductType = FERT
  severity: warning
  rule: A finished product should have a description of at least 5 characters.

- id: R005
  type: validation
  scope: status
  when: ProductType = FERT
  severity: warning
  rule: A finished product should not be left in CrossPlantStatus 01 (Design).
  why: A sellable finished good in Design status is usually a setup mistake.

- id: R006
  type: validation
  scope: weights
  severity: error
  rule: If both gross and net weight are given, their weight units must be the same.

# ---------- DERIVATION rules (expand the data) ----------

- id: R007
  type: derivation
  scope: distribution
  when: ProductType = FERT
  action: Extend the material to sales organisation 1710, distribution channel 10.
  why: All finished goods in this deployment are sold through the 1710 / 10 channel.

- id: R008
  type: derivation
  scope: plant
  when: ProductType = FERT
  action: Extend the material to plant 1710.



- id: R009
  type: validation
  scope: mandatory
  when: operation = create
  severity: error
  rule: A new material must have ProductType, IndustrySector, BaseUnit, and a description.
  why: SAP enforces this on create anyway; checking here gives feedback before the round-trip.

- id: R010
  type: derivation
  scope: defaults
  when: operation = create AND BaseUnit is empty
  action: Set BaseUnit to EA (Each).

- id: R011
  type: derivation
  scope: procurement
  when: ProductType = ROH
  action: Set procurement type to F (external procurement).
  why: Raw materials in this deployment are bought, not made.
