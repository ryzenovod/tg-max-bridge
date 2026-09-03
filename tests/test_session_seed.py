from __future__ import annotations

import base64
import errno
import io
import os
import tarfile
from pathlib import Path

import pytest


def _seed_archive(files: dict[str, bytes]) -> str:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(content))
    return base64.b64encode(raw.getvalue()).decode("ascii")


def _symlink_archive(name: str, target: str) -> str:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        archive.addfile(info)
    return base64.b64encode(raw.getvalue()).decode("ascii")


def _duplicate_archive(name: str) -> str:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for content in (b"first", b"second"):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(content))
    return base64.b64encode(raw.getvalue()).decode("ascii")


def _session_db(home: Path) -> Path:
    return home / ".max-mcp" / "session.db"


def test_seed_max_session_extracts_expected_files_and_removes_env(tmp_path):
    from tg_max_bridge.session_seed import seed_max_session_from_env

    env = {
        "MAX_MCP_SESSION_TARB64": _seed_archive(
            {
                ".max-mcp/session.db": b"sqlite bytes",
                ".max-mcp/session.json": b'{"token":"synthetic"}',
            }
        )
    }

    result = seed_max_session_from_env(home_dir=tmp_path, env=env)

    assert result.seeded is True
    assert _session_db(tmp_path).read_bytes() == b"sqlite bytes"
    assert (tmp_path / ".max-mcp" / "session.json").read_text() == (
        '{"token":"synthetic"}'
    )
    assert oct(_session_db(tmp_path).stat().st_mode & 0o777) == "0o600"
    assert "MAX_MCP_SESSION_TARB64" not in env


def test_seed_max_session_is_noop_without_secret(tmp_path):
    from tg_max_bridge.session_seed import seed_max_session_from_env

    result = seed_max_session_from_env(home_dir=tmp_path, env={})

    assert result.seeded is False
    assert not _session_db(tmp_path).exists()


def test_seed_max_session_never_overwrites_persistent_session(tmp_path):
    from tg_max_bridge.session_seed import seed_max_session_from_env

    existing = _session_db(tmp_path)
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"already logged in")
    env = {
        "MAX_MCP_SESSION_TARB64": _seed_archive(
            {".max-mcp/session.db": b"new synthetic session"}
        )
    }

    result = seed_max_session_from_env(home_dir=tmp_path, env=env)

    assert result.seeded is False
    assert existing.read_bytes() == b"already logged in"
    assert "MAX_MCP_SESSION_TARB64" not in env


@pytest.mark.parametrize(
    "name",
    [
        "../session.db",
        ".max-mcp/../session.db",
        "/tmp/session.db",
        ".max-mcp/nested/session.db",
    ],
)
def test_seed_max_session_rejects_path_traversal(tmp_path, name):
    from tg_max_bridge.session_seed import SessionSeedError, seed_max_session_from_env

    env = {"MAX_MCP_SESSION_TARB64": _seed_archive({name: b"bad"})}

    with pytest.raises(SessionSeedError, match="session archive"):
        seed_max_session_from_env(home_dir=tmp_path, env=env)

    assert not (tmp_path / "session.db").exists()
    assert "MAX_MCP_SESSION_TARB64" not in env


def test_seed_max_session_rejects_symlink_member(tmp_path):
    from tg_max_bridge.session_seed import SessionSeedError, seed_max_session_from_env

    env = {
        "MAX_MCP_SESSION_TARB64": _symlink_archive(
            ".max-mcp/session.db", "/private/session.db"
        )
    }

    with pytest.raises(SessionSeedError, match="regular file"):
        seed_max_session_from_env(home_dir=tmp_path, env=env)

    assert not _session_db(tmp_path).exists()
    assert "MAX_MCP_SESSION_TARB64" not in env


def test_seed_max_session_rejects_duplicate_members_atomically(tmp_path):
    from tg_max_bridge.session_seed import SessionSeedError, seed_max_session_from_env

    env = {"MAX_MCP_SESSION_TARB64": _duplicate_archive(".max-mcp/session.db")}

    with pytest.raises(SessionSeedError, match="duplicate"):
        seed_max_session_from_env(home_dir=tmp_path, env=env)

    assert not (tmp_path / ".max-mcp").exists()
    assert "MAX_MCP_SESSION_TARB64" not in env


def test_seed_max_session_rejects_oversized_member(tmp_path):
    from tg_max_bridge.session_seed import SessionSeedError, seed_max_session_from_env

    env = {
        "MAX_MCP_SESSION_TARB64": _seed_archive(
            {".max-mcp/session.db": b"x" * (2 * 1024 * 1024 + 1)}
        )
    }

    with pytest.raises(SessionSeedError, match="too large"):
        seed_max_session_from_env(home_dir=tmp_path, env=env)

    assert not _session_db(tmp_path).exists()
    assert "MAX_MCP_SESSION_TARB64" not in env


def test_seed_max_session_defaults_to_process_environment(monkeypatch, tmp_path):
    from tg_max_bridge.session_seed import seed_max_session_from_env

    monkeypatch.setenv(
        "MAX_MCP_SESSION_TARB64",
        _seed_archive({".max-mcp/session.db": b"process env session"}),
    )

    result = seed_max_session_from_env(home_dir=tmp_path)

    assert result.seeded is True
    assert _session_db(tmp_path).read_bytes() == b"process env session"
    assert "MAX_MCP_SESSION_TARB64" not in os.environ


def test_seed_max_session_ignores_chmod_eperm_when_best_effort_enabled(
    monkeypatch, tmp_path
):
    from tg_max_bridge import permissions
    from tg_max_bridge.session_seed import seed_max_session_from_env

    def object_store_chmod(path, mode, *, follow_symlinks=True):
        raise PermissionError(errno.EPERM, "operation not supported", os.fspath(path))

    monkeypatch.setattr(permissions.os, "chmod", object_store_chmod)
    monkeypatch.setenv("TG_MAX_BRIDGE_BEST_EFFORT_CHMOD", "1")
    env = {
        "MAX_MCP_SESSION_TARB64": _seed_archive(
            {".max-mcp/session.db": b"object storage session"}
        ),
    }

    result = seed_max_session_from_env(home_dir=tmp_path, env=env)

    assert result.seeded is True
    assert _session_db(tmp_path).read_bytes() == b"object storage session"
    assert "MAX_MCP_SESSION_TARB64" not in env


def test_seed_max_session_propagates_chmod_eperm_by_default(monkeypatch, tmp_path):
    from tg_max_bridge import permissions
    from tg_max_bridge.session_seed import seed_max_session_from_env

    def object_store_chmod(path, mode, *, follow_symlinks=True):
        raise PermissionError(errno.EPERM, "operation not supported", os.fspath(path))

    monkeypatch.setattr(permissions.os, "chmod", object_store_chmod)
    env = {
        "MAX_MCP_SESSION_TARB64": _seed_archive(
            {".max-mcp/session.db": b"strict session"}
        )
    }

    with pytest.raises(PermissionError):
        seed_max_session_from_env(home_dir=tmp_path, env=env)

    assert not _session_db(tmp_path).exists()
    assert "MAX_MCP_SESSION_TARB64" not in env


def test_seed_max_session_propagates_unexpected_chmod_error_in_best_effort(
    monkeypatch, tmp_path
):
    from tg_max_bridge import permissions
    from tg_max_bridge.session_seed import seed_max_session_from_env

    def broken_chmod(path, mode, *, follow_symlinks=True):
        raise OSError(errno.EIO, "backend I/O failure", os.fspath(path))

    monkeypatch.setattr(permissions.os, "chmod", broken_chmod)
    monkeypatch.setenv("TG_MAX_BRIDGE_BEST_EFFORT_CHMOD", "1")
    env = {
        "MAX_MCP_SESSION_TARB64": _seed_archive(
            {".max-mcp/session.db": b"strict session"}
        ),
    }

    with pytest.raises(OSError, match="backend I/O failure"):
        seed_max_session_from_env(home_dir=tmp_path, env=env)

    assert not _session_db(tmp_path).exists()
    assert "MAX_MCP_SESSION_TARB64" not in env
