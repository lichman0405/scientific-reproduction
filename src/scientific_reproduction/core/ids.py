"""Deterministic identifier helpers (DEV-M1-G01).

IDs are pure functions of their documented inputs: no randomness, no
wall-clock time, no counter state. The same ``kind`` and ``parts`` always
produce the same ID, on every machine and in every process, so committed
objects keep their identity as long as their canonical fields are passed
unchanged.

ID format
---------
Generated IDs have the form::

    sr_<kind>_<32 lowercase hex chars>

Examples: ``sr_project_9f86d081884c7d659a2feaa0c55ad015``,
``sr_goal_a7ffc6f8bf1ed76651c14756a061d662``.

Input contract (documented, stable)
-----------------------------------
``generate_id(kind, *parts)``:

* ``kind`` -- the object category (e.g. ``"project"``, ``"goal"``,
  ``"run"``). Must match ``^[a-z][a-z0-9_-]{0,31}$`` (lowercase letter,
  then lowercase letters/digits/underscore/hyphen, max 32 chars).
* ``parts`` -- the canonical fields of the object, in a **fixed, agreed
  order**, passed as strings. Only these values affect the ID. A rename of
  a non-canonical field (e.g. ``title``) therefore does not change the ID
  of an already-committed object -- callers control which fields are
  canonical by choosing what to pass.

Canonical encoding: the kind and each part are UTF-8 encoded and joined
with length prefixes (``b"<len>:" + payload``), which is unambiguous: no
two distinct ``(kind, parts)`` tuples serialize to the same byte string.

Collision behavior
------------------
* Same ``kind`` + same ``parts`` (same order) -> identical ID.
* Any difference in kind or in any part (or their order) -> different ID
  with overwhelming probability (SHA-256 truncated to 128 bits).
* Distinct objects that happen to share all canonical fields are the same
  object by definition; to give an object its own identity, include an
  identity-bearing field (e.g. an existing project_id) in ``parts``.

The truncation to 32 hex chars (128 bits) keeps IDs compact while the
collision probability for distinct inputs stays negligible for any
realistic object count (birthday bound ~2^64 documents).
"""

from __future__ import annotations

import hashlib
import re
from typing import Sequence

#: Number of hex characters in the digest portion of a generated ID.
ID_HEX_DIGITS = 32

#: Regex a generated ID must match: sr_<kind>_<32 hex chars>.
ID_PATTERN = re.compile(r"^sr_[a-z0-9_-]+_[0-9a-f]{32}$")

#: Regex a kind must match: lowercase letter, then lowercase
#: letters/digits/underscore/hyphen, at most 32 characters total.
KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class InvalidKindError(ValueError):
    """Raised when ``kind`` does not satisfy the documented kind contract."""


class InvalidIdError(ValueError):
    """Raised when a value is not a valid ``sr_<kind>_<hex>`` ID."""


def generate_id(kind: str, *parts: str) -> str:
    """Return a deterministic ID ``sr_<kind>_<32 hex chars>``.

    See the module docstring for the full input contract. In short:

    * ``kind`` must match ``^[a-z][a-z0-9_-]{0,31}$``;
    * ``parts`` are the canonical field values (strings), in a fixed order;
      the ID depends on nothing else.

    Raises:
        InvalidKindError: if ``kind`` violates the documented format.
        TypeError: if any part is not a string.
    """
    if not KIND_PATTERN.fullmatch(kind):
        raise InvalidKindError(
            f"invalid ID kind {kind!r}: expected ^[a-z][a-z0-9_-]{{0,31}}$"
        )
    for i, part in enumerate(parts):
        if not isinstance(part, str):
            raise TypeError(f"part {i} must be a str, got {type(part).__name__}")

    digest = _digest(kind, parts)
    return f"sr_{kind}_{digest}"


def _digest(kind: str, parts: Sequence[str]) -> str:
    """Return the 32-hex-char SHA-256 digest of the canonical encoding.

    The canonical encoding is length-prefixed UTF-8, so it is unambiguous
    for any strings, including empty strings and strings containing the
    separator characters.
    """
    hasher = hashlib.sha256()
    for value in (kind, *parts):
        encoded = value.encode("utf-8")
        hasher.update(f"{len(encoded)}:".encode("ascii"))
        hasher.update(encoded)
    return hasher.hexdigest()[:ID_HEX_DIGITS]


def is_valid_id(value: str, kind: str | None = None) -> bool:
    """Return True if ``value`` is a well-formed generated ID.

    With ``kind`` given, also requires the ID's kind segment to equal it.
    """
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        return False
    if kind is not None:
        return value.startswith(f"sr_{kind}_")
    return True


def parse_id(value: str) -> tuple[str, str]:
    """Split a valid ID into ``(kind, digest)``.

    Raises:
        InvalidIdError: if ``value`` is not a well-formed generated ID.
    """
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise InvalidIdError(
            f"invalid ID {value!r}: expected sr_<kind>_<32 hex chars>"
        )
    kind, digest = value[3:].rsplit("_", 1)
    return kind, digest
