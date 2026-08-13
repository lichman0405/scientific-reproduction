"""Deterministic scientific workflow rule modules (DEV-M2 milestone).

Each module in this package encodes one frozen normative rule set as pure
data tables plus pure functions -- no prompts, no LLM, no randomness, no
wall-clock dependence (AC-03 of DEV-M2-G01). The same input always yields
the same output on every platform and interpreter version.

Ownership note: this package init file is maintained by DEV-M2-G01 per the
same-milestone coordination contract; sibling rule modules (lifecycle,
dependencies, evidence, criticality, ...) are implemented by their own
frozen goals.
"""

__all__: list[str] = []
