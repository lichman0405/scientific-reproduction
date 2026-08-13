"""Atomic file-based locks (DEV-M1-G03, acceptance AC-01).

Contested ownership on the v0.1 filesystem backend is handled with two
complementary primitives (14-STATE-GIT-ARTIFACTS.md SS2/SS3):

* ``atomic_write`` (``core.atomic``) makes **content** replacement atomic:
  a reader sees either the complete old or the complete new content,
  never a partial mix (temp file + fsync + ``os.replace``).
* ``atomic_create`` (this module) makes **first creation** atomic: the
  file is created only if it does not already exist, and the content is
  complete from the moment the file becomes visible.

Why ``os.replace`` alone cannot acquire a lock
----------------------------------------------
``os.replace`` is atomic but **overwrites** an existing target: two
concurrent claimers that both call ``os.replace`` would both succeed
(last writer wins) and both conclude they acquired the object.  A
"check if absent, then write" sequence is a TOCTOU race, not an atomic
protocol.  The filesystem primitive for *create-if-not-exists* is
``os.link`` (POSIX ``link(2)`` / Windows ``CreateHardLinkW``), which
fails with ``FileExistsError`` when the target already exists -- exactly
the semantics 14-STATE-GIT-ARTIFACTS.md SS4 requires ("acquire a lease
using atomic create-if-not-exists").

``atomic_create`` therefore stages the complete content with
``atomic_write`` (same-directory temp file, fsync, atomic rename to a
unique staging name) and then hard-links the staging file onto the final
path: the link either does not happen or the target exists with the
complete, fsynced content.  Readers can never observe a partially
written lock/lease file.  The staging file is removed afterwards; a
crash leaves only an invisible ``.claim-*.tmp`` file, exactly like a
crashed ``atomic_write``.

Requirement: the filesystem must support hard links (POSIX, Windows
NTFS).  Filesystems without hard-link support (e.g. FAT/exFAT) surface
the underlying ``OSError`` instead of silently degrading the protocol.

``FileLockStore`` provides per-key lock files under a ``locks/``
directory (``locks/<key>.lock`` per SS3) for short critical sections:
non-blocking ``try_acquire``, raising ``acquire``, idempotent
``release`` on the handle, context-manager support, and an optional
mtime-based staleness break (``stale_after``) for recovering a lock
left behind by a crashed holder.

Lease-based recovery for long-held reservations is implemented on top
of ``atomic_create`` in ``core.leases``; a lease file is its own lock.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from scientific_reproduction.core.atomic import atomic_write

__all__ = [
    "LockHeldError",
    "FileLock",
    "FileLockStore",
    "atomic_create",
]


class LockHeldError(Exception):
    """Raised when a lock (or lease) is already held by another principal."""


def atomic_create(
    path: str | Path,
    content: str | bytes,
    *,
    file_mode: int | None = None,
) -> None:
    """Atomically create ``path`` with complete ``content``, or fail.

    Atomic create-if-not-exists: the target is created only if it does
    not already exist, and it is created with the complete content
    (never a partial write, never an empty file).  If the target already
    exists, ``FileExistsError`` is raised and the existing file is left
    untouched.

    Args:
        path: destination file.  Its parent directory is created if
            missing.  May be given as ``str`` or ``pathlib.Path``.
        content: ``str`` is encoded as UTF-8; ``bytes`` is written as is.
        file_mode: optional explicit mode, passed through to the staging
            ``atomic_write`` (``None`` keeps the 0o600 default).

    Raises:
        FileExistsError: the target already exists (nothing is changed).
        OSError: if the write, fsync, or link fails; the previous state
            is left untouched and the staging file is removed.
    """
    target = Path(path)
    # A per-claim unique staging name: two concurrent claimers can never
    # collide on the same staging file.
    staging = target.with_name(f".{target.name}.claim-{uuid.uuid4().hex}.tmp")
    # Stage the complete content next to the target (temp + fsync +
    # atomic replace -- content is complete and crash-safe).
    atomic_write(staging, content, file_mode=file_mode)
    try:
        # Atomic create-if-not-exists (14-STATE-GIT-ARTIFACTS.md SS4):
        # link(2)/CreateHardLinkW fail with FileExistsError if the target
        # exists; because the linked inode was fully written and fsynced
        # before the link, the target never exposes partial content.
        os.link(staging, target)
    finally:
        try:
            os.unlink(staging)
        except FileNotFoundError:
            pass


class FileLock:
    """A held lock file handle (``locks/<key>.lock``).

    Obtained from ``FileLockStore.acquire`` / ``try_acquire``.  Only the
    principal whose ``(owner, nonce)`` is recorded in the lock file may
    release it; ``release`` re-verifies the recorded content immediately
    before unlinking so it never deletes a lock re-created by someone
    else in the meantime.

    Supports the context-manager protocol: ``release`` is called on
    exit.
    """

    def __init__(self, path: Path, owner: str, nonce: str) -> None:
        self._path = path
        self._owner = owner
        self._nonce = nonce

    @property
    def path(self) -> Path:
        """The lock file path."""
        return self._path

    def _read(self) -> dict[str, Any] | None:
        """Return the lock file content as a dict, or None if missing.

        Raises:
            LockHeldError: the content cannot be verified (not a JSON
                object) -- treated as held by an unknown principal.
        """
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise LockHeldError(
                f"lock file at {self._path} is unreadable; refusing to"
                f" guess its ownership: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise LockHeldError(
                f"lock file at {self._path} is not a JSON object; refusing"
                " to guess its ownership"
            )
        return data

    def release(self) -> None:
        """Release the lock (idempotent).

        Raises:
            LockHeldError: the lock file is not held by this handle (it
                was already released and re-created by another principal,
                or it changed concurrently); nothing is deleted.
        """
        current = self._read()
        if current is None:
            return  # already released: idempotent
        if current.get("owner") != self._owner or current.get("nonce") != self._nonce:
            raise LockHeldError(
                f"lock file at {self._path} is held by another principal;"
                " refusing to release it"
            )
        # Compare-and-unlink with bounded retry: on Windows a concurrent
        # reader can transiently hold an open handle, so the unlink may
        # raise a sharing violation even though the content is still
        # ours; re-verify and retry instead of failing or guessing.
        for _ in range(3):
            if self._read() != current:
                raise LockHeldError(
                    f"lock file at {self._path} changed concurrently; refusing"
                    " to release it"
                )
            try:
                self._path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                continue  # transient sharing violation: re-verify and retry
        raise LockHeldError(
            f"lock file at {self._path} is in use and could not be removed;"
            " retry the release"
        )

    def __enter__(self) -> FileLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class FileLockStore:
    """Per-key lock files under ``lock_dir`` (``locks/<key>.lock``).

    Non-blocking by design: ``try_acquire`` returns ``None`` instead of
    waiting.  Locks are intended for short critical sections (a read-
    modify-write of a few microseconds); long-lived reservations must
    use ``core.leases`` whose expiry is the recovery mechanism.

    Args:
        lock_dir: root of the lock tree.  May be ``str`` or ``Path``.
        stale_after: optional seconds after which an existing lock file
            is considered abandoned (its mtime is older than the grace
            period) and may be broken by a claimer.  ``None`` (default)
            means locks are never broken automatically -- a lock left by
            a crashed holder then requires operator intervention, which
            is the deterministic default for v0.1.  The stale break is
            compare-and-unlink: the file's mtime (nanosecond precision)
            is verified again immediately before removal.
        file_mode: optional explicit mode for lock files, passed through
            to ``atomic_create``.
    """

    def __init__(
        self,
        lock_dir: str | Path,
        *,
        stale_after: float | None = None,
        file_mode: int | None = None,
    ) -> None:
        self.lock_dir = Path(lock_dir)
        self._stale_after = stale_after
        self._file_mode = file_mode

    #: Characters that can never appear in a lock key (a lock key becomes
    #: a plain file stem).
    _FORBIDDEN_KEY_CHARS = ("/", "\\", "\x00")

    def _check_key(self, key: str) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("lock key must be a non-empty string")
        if key in (".", "..") or any(c in self._FORBIDDEN_KEY_CHARS for c in key):
            raise ValueError(
                f"invalid lock key {key!r}: must be a plain file stem (no"
                " path separators, no '.', no '..')"
            )

    def _lock_path(self, key: str) -> Path:
        self._check_key(key)
        return self.lock_dir / f"{key}.lock"

    def try_acquire(self, key: str, owner: str) -> FileLock | None:
        """Acquire the lock for ``key`` if free; return None otherwise.

        Args:
            key: plain file stem naming the locked object.
            owner: principal/session id that will hold the lock.

        Raises:
            ValueError: invalid ``key`` or ``owner``.
        """
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("lock owner must be a non-empty string")
        path = self._lock_path(key)
        nonce = uuid.uuid4().hex
        content = json.dumps(
            {
                "owner": owner,
                "nonce": nonce,
                "pid": os.getpid(),
                "acquired_at": time.time(),
            },
            indent=2,
            sort_keys=True,
        )
        for attempt in (0, 1):
            try:
                atomic_create(path, content, file_mode=self._file_mode)
                return FileLock(path, owner, nonce)
            except FileExistsError:
                # A stale lock may be broken once (compare-and-unlink),
                # then the create is retried; otherwise the lock is held.
                if attempt == 0 and self._break_stale(path):
                    continue
                return None
        return None

    def acquire(self, key: str, owner: str) -> FileLock:
        """Acquire the lock for ``key`` or raise ``LockHeldError``.

        Non-blocking: if the lock is held, ``LockHeldError`` is raised
        immediately instead of waiting.
        """
        lock = self.try_acquire(key, owner)
        if lock is None:
            raise LockHeldError(
                f"lock for key {key!r} is already held by another principal"
            )
        return lock

    def is_locked(self, key: str) -> bool:
        """Return True if a lock file currently exists for ``key``."""
        return self._lock_path(key).is_file()

    def _break_stale(self, path: Path) -> bool:
        """Break an abandoned lock file (if ``stale_after`` configured).

        Compare-and-unlink: the mtime (nanosecond precision) is verified
        a second time immediately before the unlink so a lock that was
        replaced between the two stats is never removed.  Returns True
        when the file is gone (broken by us, or already gone) and the
        create should be retried.
        """
        if self._stale_after is None:
            return False
        try:
            first = path.stat()
        except FileNotFoundError:
            return True  # already gone: retry the create
        if time.time() - first.st_mtime <= self._stale_after:
            return False  # fresh: held by a live principal
        try:
            second = path.stat()
        except FileNotFoundError:
            return True
        if second.st_mtime_ns != first.st_mtime_ns:
            return False  # changed between the two stats: not stale
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return True
