from __future__ import annotations

import errno
import os
from pathlib import Path

BEST_EFFORT_CHMOD_ENV = "TG_MAX_BRIDGE_BEST_EFFORT_CHMOD"

_UNSUPPORTED_CHMOD_ERRNOS = frozenset(
    code
    for code in (
        errno.EPERM,
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOTSUP", None),
    )
    if code is not None
)


def chmod_fd(descriptor: int, mode: int) -> bool:
    try:
        os.fchmod(descriptor, mode)
    except OSError as exc:
        if _can_ignore_unsupported_chmod(exc):
            return False
        raise
    return True


def chmod_path(path: Path, mode: int, *, follow_symlinks: bool = True) -> bool:
    try:
        os.chmod(path, mode, follow_symlinks=follow_symlinks)
    except OSError as exc:
        if _can_ignore_unsupported_chmod(exc):
            return False
        raise
    return True


def _can_ignore_unsupported_chmod(exc: OSError) -> bool:
    return (
        os.environ.get(BEST_EFFORT_CHMOD_ENV, "").lower() in {"1", "true", "yes", "on"}
        and exc.errno in _UNSUPPORTED_CHMOD_ERRNOS
    )
