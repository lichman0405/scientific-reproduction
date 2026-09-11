#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Language-consistency lint for a reproduction workspace (P22-C).

A zh deliverable should not silently carry English display prose in the
state records that the human summary renders (assumption rationales,
decision rationales, requirement statements).  This tool scans those
display fields and reports entries whose prose is not in the expected
language, so the supervisor can localize them before finalization.

Usage:
    python scripts/check_state_language.py <workspace_root> [--expect zh|en]

Exit codes: 0 = all display fields in the expected language (or none);
1 = violations found (list printed).  Machine ids (ASM-*/DEC-*/REQ-*/GOAL-*)
are never flagged; only prose fields are inspected.
"""
import json
import re
import sys
from pathlib import Path

CJK = re.compile(r"[一-鿿]")


def _prose(s):
    return (s or "").strip()


def _ratio_cjk(s):
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 1.0 if not s else 0.5
    return sum(1 for c in letters if CJK.match(c)) / len(letters)


def _scan(path: Path, expect: str):
    """Return (path_label, field, ratio, snippet) violations."""
    out = []
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(rec, dict):
        return out
    fields = ("rationale", "statement")
    for f in fields:
        val = _prose(rec.get(f))
        if not val:
            continue
        ratio = _ratio_cjk(val)
        if expect == "zh" and ratio < 0.30 and len(val) > 40:
            out.append((str(path), f, ratio, val[:90]))
        elif expect == "en" and ratio > 0.30 and len(val) > 40:
            out.append((str(path), f, ratio, val[:90]))
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    expect = sys.argv[2].split("=", 1)[1] if len(sys.argv) > 2 and sys.argv[2].startswith("--expect") else "zh"
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 2
    violations = []
    for sub in ("assumptions", "decisions", "requirements"):
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            violations.extend(_scan(p, expect))
    if not violations:
        print(f"[check_state_language] OK: display fields of {root} are "
              f"consistent with expected language '{expect}' (or absent)")
        return 0
    print(f"[check_state_language] {len(violations)} violation(s) for "
          f"expected language '{expect}':")
    for path, field, ratio, snippet in violations:
        print(f"- {path} [{field}] cjk-ratio={ratio:.2f}: {snippet}…")
    print("Localize the prose fields above (ids stay unchanged), or rerun "
          "with a different --expect language.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
