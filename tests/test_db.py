from __future__ import annotations

import os
import stat

import pytest


@pytest.mark.asyncio
async def test_connect_restricts_sqlite_directory_and_file_permissions(tmp_path):
    from tg_max_bridge.db import connect, init_schema

    sqlite_path = tmp_path / "data" / "bridge.sqlite3"

    db = await connect(sqlite_path)
    try:
        await init_schema(db)
        async with db.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "delete"
    finally:
        await db.close()

    dir_mode = stat.S_IMODE(sqlite_path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(sqlite_path.stat().st_mode)
    assert dir_mode & 0o077 == 0
    assert file_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_connect_does_not_chmod_existing_parent_directory(tmp_path):
    from tg_max_bridge.db import connect, init_schema

    existing_parent = tmp_path / "shared"
    existing_parent.mkdir(mode=0o755)
    existing_parent.chmod(0o755)
    sqlite_path = existing_parent / "bridge.sqlite3"

    db = await connect(sqlite_path)
    try:
        await init_schema(db)

        assert stat.S_IMODE(existing_parent.stat().st_mode) == 0o755
        for candidate in (
            sqlite_path,
            sqlite_path.with_name(f"{sqlite_path.name}-wal"),
            sqlite_path.with_name(f"{sqlite_path.name}-shm"),
        ):
            if candidate.exists():
                assert stat.S_IMODE(candidate.stat().st_mode) == 0o600
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_connect_rejects_symlink_database_path(tmp_path):
    from tg_max_bridge.db import connect

    real_database = tmp_path / "real.sqlite3"
    real_database.touch(mode=0o600)
    symlink_database = tmp_path / "link.sqlite3"
    symlink_database.symlink_to(real_database)

    with pytest.raises(ValueError, match="symlink"):
        await connect(symlink_database)


@pytest.mark.asyncio
async def test_connect_rejects_symlink_parent_component(tmp_path):
    from tg_max_bridge.db import connect

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    symlink_parent = tmp_path / "linked"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        await connect(symlink_parent / "bridge.sqlite3")


@pytest.mark.asyncio
async def test_connect_accepts_root_owned_system_symlink_parent(tmp_path, monkeypatch):
    from tg_max_bridge import db as db_module
    from tg_max_bridge.db import connect, init_schema

    sqlite_path = tmp_path / "bridge.sqlite3"
    original_lstat = db_module.Path.lstat

    def fake_lstat(path):
        result = original_lstat(path)
        if path == sqlite_path.parent:
            values = list(result)
            values[0] = stat.S_IFLNK | 0o777
            values[4] = 0
            return os.stat_result(values)
        return result

    monkeypatch.setattr(db_module.Path, "lstat", fake_lstat)

    db = await connect(sqlite_path)
    try:
        await init_schema(db)
    finally:
        await db.close()
