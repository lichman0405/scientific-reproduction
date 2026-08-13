# Known Spec-Package Defects & Dispositions

| ID | File | Defect | Disposition |
|---|---|---|---|
| SPEC-DEFECT-001 | `development/validate_development_spec.py` (lines ~40–41) | Stale assertion: "Expected 79 development goals, found 80". The spec package was upgraded 79→80 goals in v0.1.2 (see `SPEC-CHANGELOG.md`); `validate_spec_package.py`, `PACKAGE-MANIFEST.json` and the goal catalog all agree on 80. The standalone validator was not updated. | **FIX IN DEV-M0-G00 (IN_CURRENT_GOAL)** — DEV-M0-G00 AC-05 requires `scripts/validate_development_contracts.py` to pass on the frozen 80-goal contract set; a validator that asserts the wrong count cannot pass. The fix (79→80) is strictly a defect repair, does not broaden M0-G00 scope, and does not modify any frozen acceptance criterion. Recorded here as the mandated decision trail. |

Neither defect alters any frozen acceptance criterion or DEV-GOAL contract.
