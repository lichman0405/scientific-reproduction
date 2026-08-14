# Installation and verification guide

This guide is grounded in the real package metadata (`pyproject.toml`), the
repository verification entry point (`scripts/verify.py`), and the frozen
quality gates (`24-DEVELOPMENT-QUALITY-GATES.md`, `26-DEVELOPMENT-CHANGE-CI-RELEASE.md`).
Every command below runs against the actual v0.1 implementation.

## Requirements

- Python **3.11 or newer** (`pyproject.toml`: `requires-python = ">=3.11"`).
  The verification in this repository runs on Python 3.13 on Windows and is
  supported on POSIX as well.
- A working `pip` and the `venv` module (standard library).
- Network access on the first install only (to fetch the dev extras).

The v0.1 runtime is intentionally **stdlib-only**: `pyproject.toml` declares
`dependencies = []` and the package has no runtime dependencies. The `dev`
extra installs the verification toolchain: `pytest`, `pytest-cov`, `ruff`,
`mypy`, `jsonschema` and `PyYAML` (the last two validate the frozen schemas
and benchmark data files).

## Install the package (editable, from the repository root)

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows (PowerShell); use bin/activate on POSIX
python -m pip install -e ".[dev]"
```

The editable install wires `src/scientific_reproduction` into the virtual
environment; the package is the `scientific-reproduction` distribution with
the import name `scientific_reproduction` (`pyproject.toml`,
`[tool.hatch.build.targets.wheel] packages = ["src/scientific_reproduction"]`).

If you only need the runtime (no tooling), `python -m pip install -e .`
suffices — there is nothing extra to pull in.

## Run the verification

### Canonical one-shot entry point

```bash
python scripts/verify.py
```

`scripts/verify.py` is the canonical repository verification command
(DEV-M0-G01/G02). It creates a fresh virtual environment in `.verify-venv`
(reused with `--reuse-venv`), installs the package editable with dev extras,
then runs:

1. `python -m pytest -q` — the unit test suite;
2. `python -m ruff check .` — the lint gate;
3. `python -m mypy src/` — the type/static check.

It prints `[verify] PASS` and exits 0 on success, or `[verify] FAIL` and a
non-zero exit code on any failing step. It works on Windows PowerShell,
Git Bash, and POSIX shells.

### Manual verification commands

The same gates, run individually:

```bash
python -m pytest -q            # full default test suite (tests/)
python -m ruff check .         # lint: E4/E7/E9, F, I (pyproject.toml [tool.ruff])
python -m mypy src             # type check (pyproject.toml [tool.mypy], files = ["src"])
```

Notes on the pytest configuration (`pyproject.toml [tool.pytest.ini_options]`):

- `testpaths = ["tests"]` — the default run is the whole suite under `tests/`;
- `addopts = ["-m", "not slow"]` — long-running tests that create fresh
  virtual environments and hit the network are excluded by default and are
  not part of the deterministic gate. Run them explicitly with
  `python -m pytest -m slow` when needed (a CLI `-m` overrides the default);
- the full default suite on the v0.1 base is
  3063 baseline + 50 benchmark-acceptance + 33 audit/trace + 18 report
  + 17 summary = **3181 tests**. The verified run on the DEV-M13-G04 head
  (Python 3.13, Windows): `3181 passed, 7 skipped, 1 deselected in
  336.23s (0:05:36)`.

### FDM-201 execution-package validator

```bash
python benchmarks/fdm201/execution_packages/validate_packages.py
```

The deterministic, offline FDM-201 execution-package validation gate
(DEV-M13): validates every package under
`benchmarks/fdm201/execution_packages/**` against the frozen benchmark data —
AC-01 schema conformance (lab-execution-package / worker-context schemas),
AC-02 id traceability into the frozen data files, AC-03 no unrelated project
context. Exits 0 when all packages pass, 1 otherwise. It requires `PyYAML`
(the `dev` extra provides it).

## Sanity check after install

Initialize a throwaway one-paper project (see
[`reproduce-and-goals.md`](reproduce-and-goals.md)):

```bash
python -m scientific_reproduction.cli.reproduce init 10.1039/D5TA00771B --root /tmp/fdm201-demo
```

Expected output on success (exit code 0):

```text
initialized project sr_project_<32 hex> at <root>
primary target: doi '10.1039/D5TA00771B'
git checkpoint: <12 hex> (project initialized)
```

## Troubleshooting

- **`pip install` fails on Windows PowerShell**: use the absolute interpreter
  path of your virtual environment, e.g.
  `& .venv\Scripts\python.exe -m pip install -e ".[dev]"`, and run commands
  from the repository root (the editable install resolves `src/` relative to
  `pyproject.toml`).
- **A pytest run appears to launch twice**: a known harness quirk on some
  setups — both processes produce identical results; wait for all processes
  to exit before judging the outcome.
- **A single flaky lease test**: `test_leases.py::test_concurrent_takeover_of_expired_lease_single_winner`
  may flake once in a background run (known accepted defect DEV-DEFECT-002);
  re-run that single test in isolation — a pass there is not a regression.
