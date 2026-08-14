# FDM-201 Plan v1 resources (DEV-M12-G03)

Resource records for the FDM-201 reference reproduction benchmark
(`17-FDM201-REFERENCE-CASE.md`, DOI `10.1039/D5TA00771B`), frozen with
Plan v1.

## Layout

- `resources.yaml` — 17 resource records `RES-001..017` with
  `resource_type` and `availability_state` per `schemas/resource.schema.yaml`
  (`AVAILABLE` / `PROCURE` / `OUTSOURCE` / `CAPABILITY_GAP`), the G01
  acquisition status where applicable (`acquisition_status`,
  `g01_acquisition_item`, `source_ids`), the goal that resolves a resource
  gap (`resolution_goal_id`, or the token `PROCURE` for plan-side
  procurement), the goals that use the resource (`used_by_goal_ids`), the
  goals blocked by unavailability (`blocks_goal_ids`), and a `human_gate_required`
  flag.
- `README.md` — this file.

## Coverage

| Resources | Count | Notes |
|---|---|---|
| `RES-001..009` | 9 | G01 acquisition-log items 1-9 with their REGISTERED / PARTIAL / UNAVAILABLE statuses (2 / 4 / 3) |
| `RES-010..017` | 8 | execution resources (software, compute, equipment, instruments, reagents, standards) — not G01 registration items; `acquisition_status: not_registered`, procured at execution |

The three UNAVAILABLE sources (CIF `RES-003`, related structures `RES-004`,
data availability `RES-005`) carry explicit resolution goals
(`GOAL-RES-003` / `GOAL-RES-004`) and `blocks_goal_ids` for the goals whose
execution depends on them — the formal unavailability handling is mirrored
in `../goals/mapping_audit.yaml` and `../plans/plan_v1.yaml`.

## Integrity

`../goals/audit_check.py` asserts: 17 records with unique ids, every
`resource_type` / `availability_state` in the schema enums, every G01-linked
resource maps to an existing acquisition-log item with the matching status,
every `used_by_goal_ids` / `blocks_goal_ids` goal exists in
`../goals/goals.yaml`, every `resolution_goal_id` is a defined goal or the
documented token `PROCURE`, and `source_ids` exist in the G01 records.
