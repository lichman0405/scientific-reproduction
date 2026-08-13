"""Atomic file replacement helper (DEV-M1-G02, acceptance AC-02).

Guarantee
---------
A caller-visible target path is only ever replaced through
``os.replace()``, which is atomic on both POSIX and Windows: readers see
either the complete old content or the complete new content, never a
partial mix. Content is first written to a unique temporary file in the
*same directory* as the target (so the rename can never cross a
filesystem boundary), flushed and fsynced, and only then moved over the
target.

Consequence for crash safety
----------------------------
An interrupted or partial write -- process crash, kill, disk error, or an
exception raised before the rename -- leaves the previous target content
untouched (or no target at all for a first write). The worst case is a
stale, invisible ``.tmp`` file next to the target, which is never read
under the target name and is cleaned up on the next *failed* write attempt.

The parent directory is created on demand (``mkdir(parents=True)``), so
callers do not need to pre-create the layout.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: str | Path, content: str | bytes) -> None:
    """Atomically replace ``path`` with ``content``.

    Args:
        path: destination file. Its parent directory is created if
            missing. May be given as ``str`` or ``pathlib.Path``.
        content: ``str`` is encoded as UTF-8; ``bytes`` is written as is.

    Raises:
        OSError: if the write, fsync, or rename fails. The previous
            target content (if any) is left intact and the temporary
            file is removed.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content

    # A unique temp name in the target directory: the random suffix is a
    # file name, not a content ID -- it only has to guarantee that two
    # concurrent writers never collide on the same staging file.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _widen_permissions(tmp_path)
        os.replace(tmp_path, target)
    except BaseException:
        # Never leave a partially written temp file behind after a failed
        # write; the target itself is untouched at this point.
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _widen_permissions(path: Path) -> None:
    """Give the new file default group/other read access (best effort).

    ``tempfile.mkstemp`` creates files with 0600; the workspace is a
    shared single source of truth, so object files should be readable by
    other processes/users. Non-fatal: on platforms where chmod does not
    apply meaningfully (Windows) or is denied, keep going.
    """
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
