from __future__ import annotations

import errno
import os
from pathlib import Path

BEST_EFFORT_CHMOD_ENV = "TG_MAX_BRIDGE_BEST_EFFORT_CHMOD"
ALLOW_SYNTHETIC_UID_ENV = "TG_MAX_BRIDGE_ALLOW_SYNTHETIC_UID"

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


def owner_matches_current_user(owner_uid: int) -> bool:
    if not hasattr(os, "geteuid") or owner_uid == os.geteuid():
        return True
    return _env_flag_enabled(ALLOW_SYNTHETIC_UID_ENV)


def _can_ignore_unsupported_chmod(exc: OSError) -> bool:
    return _env_flag_enabled(BEST_EFFORT_CHMOD_ENV) and (
        exc.errno in _UNSUPPORTED_CHMOD_ERRNOS
    )


def _env_flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}
