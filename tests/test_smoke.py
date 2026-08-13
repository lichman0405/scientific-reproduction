"""Smoke tests for the package skeleton (DEV-M0-G01, acceptance AC-02).

Only import-level assertions here; behavioral tests are added with the
subsystems that introduce behavior (M1 onward).
"""

import scientific_reproduction


def test_package_imports() -> None:
    assert scientific_reproduction is not None


def test_package_version_available() -> None:
    assert hasattr(scientific_reproduction, "__version__")
    assert isinstance(scientific_reproduction.__version__, str)
    assert scientific_reproduction.__version__ == "0.1.0"
