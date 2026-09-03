from __future__ import annotations

import os
import stat
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import aiosqlite

from tg_max_bridge.permissions import chmod_fd, chmod_path, owner_matches_current_user

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_chat_id INTEGER NOT NULL,
  tg_message_id INTEGER NOT NULL,
  tg_trigger_message_id INTEGER NOT NULL,
  tg_from_user_id INTEGER NOT NULL,
  tg_from_display_name TEXT NOT NULL,
  source_text TEXT NOT NULL,
  max_chat_id INTEGER NOT NULL,
  max_text TEXT NOT NULL,
  marker TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER NOT NULL,
  locked_at INTEGER,
  last_error TEXT,
  max_message_id INTEGER,
  max_response_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  sent_at INTEGER,
  UNIQUE (tg_chat_id, tg_message_id, max_chat_id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(status, next_attempt_at, id);
"""


async def connect(path: Path) -> aiosqlite.Connection:
    path = _prepare_sqlite_path(path)

    db = await aiosqlite.connect(path)
    try:
        db.row_factory = aiosqlite.Row
        # WAL is not safe on network filesystems. DELETE keeps the tiny, serialized
        # bridge database compatible with Cloud.ru's Object Storage volume.
        await db.execute("PRAGMA journal_mode=DELETE")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        _chmod_sqlite_files(path)
    except BaseException:
        await db.close()
        raise
    return db


async def init_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(SCHEMA_SQL)
    await db.commit()


@asynccontextmanager
async def transaction(db: aiosqlite.Connection) -> AsyncIterator[None]:
    await db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        await db.rollback()
        raise
    else:
        await db.commit()


def _chmod_sqlite_files(path: Path) -> None:
    for candidate in (
        path,
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    ):
        try:
            file_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(file_stat.st_mode):
            raise ValueError(f"SQLite file must not be a symlink: {candidate}")
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"SQLite path must be a regular file: {candidate}")
        if not owner_matches_current_user(file_stat.st_uid):
            raise PermissionError(f"SQLite file is not owned by this user: {candidate}")
        chmod_path(candidate, 0o600, follow_symlinks=False)


def _prepare_sqlite_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    _reject_symlink_components(path)
    _create_private_parent_directories(path.parent)
    _reject_symlink_components(path)

    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        with _private_umask():
            descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        _validate_existing_sqlite_file(path)
    else:
        try:
            chmod_fd(descriptor, 0o600)
        finally:
            os.close(descriptor)
    return path


def _create_private_parent_directories(parent: Path) -> None:
    missing: list[Path] = []
    candidate = parent
    while not candidate.exists():
        if candidate.is_symlink():
            raise ValueError(f"SQLite path must not contain symlinks: {candidate}")
        missing.append(candidate)
        next_candidate = candidate.parent
        if next_candidate == candidate:
            break
        candidate = next_candidate

    for directory in reversed(missing):
        created = False
        try:
            with _private_umask():
                directory.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass

        directory_stat = directory.lstat()
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
            directory_stat.st_mode
        ):
            raise ValueError(f"SQLite parent must be a real directory: {directory}")
        if created:
            chmod_path(directory, 0o700, follow_symlinks=False)


def _reject_symlink_components(path: Path) -> None:
    candidate = Path(path.anchor)
    for component in path.parts[1:]:
        candidate /= component
        try:
            component_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(component_stat.st_mode):
            # macOS exposes trusted system paths such as /var and /tmp through
            # root-owned compatibility symlinks. Allow those parent components,
            # while still rejecting the database itself and user-controlled links.
            if candidate != path and component_stat.st_uid == 0:
                continue
            raise ValueError(f"SQLite path must not contain symlinks: {candidate}")


def _validate_existing_sqlite_file(path: Path) -> None:
    file_stat = path.lstat()
    if stat.S_ISLNK(file_stat.st_mode):
        raise ValueError(f"SQLite file must not be a symlink: {path}")
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(f"SQLite path must be a regular file: {path}")
    if not owner_matches_current_user(file_stat.st_uid):
        raise PermissionError(f"SQLite file is not owned by this user: {path}")
    chmod_path(path, 0o600, follow_symlinks=False)


@contextmanager
def _private_umask() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)
