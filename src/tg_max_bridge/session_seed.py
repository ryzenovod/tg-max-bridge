from __future__ import annotations

import base64
import binascii
import io
import os
import shutil
import tarfile
import tempfile
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

ALLOWED_SESSION_FILES = frozenset(
    {"session.db", "session.kind", "session.phone", "session.json"}
)
MAX_SESSION_FILE_BYTES = 2 * 1024 * 1024
MAX_SESSION_ARCHIVE_BYTES = 12 * 1024 * 1024


class SessionSeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionSeedResult:
    seeded: bool


def seed_max_session_from_env(
    *,
    home_dir: Path | None = None,
    env: MutableMapping[str, str] | None = None,
) -> SessionSeedResult:
    environ = os.environ if env is None else env
    payload = environ.pop("MAX_MCP_SESSION_TARB64", None)
    if not payload:
        return SessionSeedResult(seeded=False)
    session_dir = (Path.home() if home_dir is None else home_dir) / ".max-mcp"
    if (session_dir / "session.db").exists():
        return SessionSeedResult(seeded=False)
    seed_max_session(payload, session_dir)
    return SessionSeedResult(seeded=True)


def seed_max_session(payload: str, session_dir: Path) -> None:
    try:
        payload_bytes = payload.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SessionSeedError("MAX session archive seed must be ASCII") from exc
    if len(payload_bytes) > MAX_SESSION_ARCHIVE_BYTES * 2:
        raise SessionSeedError("MAX session archive is too large")
    try:
        archive = base64.b64decode(payload_bytes, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SessionSeedError("MAX session archive seed is not valid base64") from exc
    if len(archive) > MAX_SESSION_ARCHIVE_BYTES:
        raise SessionSeedError("MAX session archive is too large")

    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = tar.getmembers()
            _validate_members(members)
            files: dict[str, bytes] = {}
            for member in members:
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise SessionSeedError("MAX session archive contains an empty file")
                data = extracted.read(MAX_SESSION_FILE_BYTES + 1)
                if len(data) > MAX_SESSION_FILE_BYTES:
                    raise SessionSeedError("MAX session archive file is too large")
                files[_member_name(member)] = data
    except tarfile.TarError as exc:
        raise SessionSeedError("MAX session archive is not a gzip tar") from exc
    _install_session_files(files, session_dir)


def _validate_members(members: list[tarfile.TarInfo]) -> None:
    names = [_member_name(member) for member in members]
    if len(names) != len(set(names)):
        raise SessionSeedError("MAX session archive contains duplicate files")
    if "session.db" not in names:
        raise SessionSeedError("MAX session archive must contain the session database")
    for member in members:
        name = _member_name(member)
        if name not in ALLOWED_SESSION_FILES:
            raise SessionSeedError("MAX session archive contains an unexpected file")
        if not member.isfile():
            raise SessionSeedError("MAX session archive may contain only regular files")
        if member.size > MAX_SESSION_FILE_BYTES:
            raise SessionSeedError("MAX session archive file is too large")


def _install_session_files(files: dict[str, bytes], session_dir: Path) -> None:
    session_parent = session_dir.parent
    session_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if session_dir.is_symlink():
        raise SessionSeedError("MAX session directory must not be a symlink")
    if session_dir.exists():
        if not session_dir.is_dir():
            raise SessionSeedError("MAX session path must be a directory")
        if any(session_dir.iterdir()):
            raise SessionSeedError("MAX session directory is not empty")
        session_dir.rmdir()

    temporary_dir = Path(tempfile.mkdtemp(prefix=".max-mcp-seed-", dir=session_parent))
    try:
        temporary_dir.chmod(0o700)
        for name, data in files.items():
            _write_session_file(temporary_dir / name, data)
        temporary_dir.rename(session_dir)
    except BaseException:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise


def _member_name(member: tarfile.TarInfo) -> str:
    path = Path(member.name)
    if path.parts[:1] != (".max-mcp",) or len(path.parts) != 2:
        raise SessionSeedError("MAX session archive contains an unsafe path")
    name = path.name
    if not name or name in {".", ".."}:
        raise SessionSeedError("MAX session archive contains an unsafe path")
    return name


def _write_session_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise SessionSeedError(
            "MAX session seed would overwrite an existing file"
        ) from exc
    with os.fdopen(fd, "wb") as file:
        file.write(data)
    path.chmod(0o600)


if __name__ == "__main__":
    seed_max_session_from_env()
