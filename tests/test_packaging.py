from __future__ import annotations

import re
from pathlib import Path


def test_dockerfile_installs_project_source_for_console_script():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

    assert "pyproject.toml" in dockerfile
    assert "src" in dockerfile
    assert "uv sync" in dockerfile or "uv pip install" in dockerfile
    assert "tg-max-bridge" in dockerfile


def test_dockerfile_uses_non_root_user_and_frozen_installs():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(r"^USER\s+(?!root\b).+", dockerfile, re.MULTILINE)
    for line in dockerfile.splitlines():
        if "uv sync" in line:
            assert "--frozen" in line

    assert "https://github.com/ryzenovod/max-mcp.git" in dockerfile
    assert "e90d80278f9ae644d22bddc4d34697ecbd508a55" in dockerfile
    assert "git.hubp.de" not in dockerfile


def test_docker_context_uses_an_allowlist_and_excludes_secrets():
    root = Path(__file__).resolve().parents[1]
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")

    assert dockerignore.splitlines()[0] == "*"
    assert "!src/**" in dockerignore
    assert "!.env" not in dockerignore
    assert "!.max-mcp" not in dockerignore


def test_compose_example_runs_with_container_hardening():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.example.yml").read_text(encoding="utf-8")

    assert "read_only: true" in compose
    assert "cap_drop:" in compose
    assert "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "max-session:/home/app/.max-mcp" in compose
    assert "~/.max-mcp" not in compose


def test_systemd_template_sets_identity_umask_and_hardening():
    root = Path(__file__).resolve().parents[1]
    service = (root / "systemd" / "tg-max-bridge.service").read_text(encoding="utf-8")

    assert re.search(r"^User=.+", service, re.MULTILINE)
    assert re.search(r"^Group=.+", service, re.MULTILINE)
    assert "UMask=0077" in service
    assert "NoNewPrivileges=true" in service
    assert "StateDirectory=tg-max-bridge" in service
    assert "/var/lib/tg-max-bridge" in service
    assert "/Users/" not in service


def test_mcp_dependency_is_pinned_to_reviewed_version():
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert '"mcp==1.28.1"' in pyproject
