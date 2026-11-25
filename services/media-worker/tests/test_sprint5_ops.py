"""Sprint 5 DevOps / container / CI hygiene regression tests (fully offline)."""

from __future__ import annotations

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
WORKER_DIR = _HERE.parents[1]
REPO_ROOT = _HERE.parents[3] if len(_HERE.parents) > 3 else None


def test_compose_media_parameterized_and_hardened():
    if REPO_ROOT is None or not (REPO_ROOT / "compose.media.yml").is_file():
        pytest.skip("repository root compose.media.yml not present in container-only layout")
    compose_path = REPO_ROOT / "compose.media.yml"
    text = compose_path.read_text(encoding="utf-8")
    assert "D:/" not in text, "compose.media.yml must not hardcode host drive paths"
    assert "${MEDIA_STORAGE_DIR:-./storage/jobs}" in text
    assert "no-new-privileges:true" in text
    assert "N8N_BLOCK_ENV_ACCESS_IN_NODE=true" in text
    assert "COLLECTION_ENGINE=${COLLECTION_ENGINE:-historical}" in text
    assert "M4_COLLECTION_APPROVAL_ENABLED=${M4_COLLECTION_APPROVAL_ENABLED:-1}" in text
    assert "NAMED_ASSERTION_SECRET=" in text
    assert "AI_MAX_IMAGES_PER_REQUEST=" in text
    assert "AI_VISUAL_DIRECTOR_V2.md:/srv/docs/AI_VISUAL_DIRECTOR_V2.md:ro" in text
    assert 'profiles: ["frontend"]' in text


def test_worker_dockerfile_and_pyproject_reproducible():
    dockerfile_path = WORKER_DIR / "Dockerfile"
    if dockerfile_path.is_file():
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
        assert "libsodium23" in dockerfile
        assert "ffmpeg" in dockerfile
        assert "USER appuser" in dockerfile
        assert "/ready" in dockerfile
        assert "pyproject.toml" in dockerfile

    pyproject = (WORKER_DIR / "pyproject.toml").read_text(encoding="utf-8")
    assert 'cache_dir = "/tmp/pytest-cache"' in pyproject


def test_ci_workflow_covers_all_layers_and_secret_scan():
    if REPO_ROOT is None or not (REPO_ROOT / ".github" / "workflows" / "tests.yml").is_file():
        pytest.skip("repository root CI workflow not present in container-only layout")
    ci_path = REPO_ROOT / ".github" / "workflows" / "tests.yml"
    text = ci_path.read_text(encoding="utf-8")
    assert "n8n-workflows:" in text
    assert "sprint4-workflows.test.mjs" in text
    assert "libsodium23" in text
    assert "compose-validate:" in text
    assert "secret-scan:" in text
    assert "gitleaks" in text
