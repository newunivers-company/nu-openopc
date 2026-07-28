from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify_nu_release_manifest import (
    DEFAULT_MANIFEST,
    ReleaseManifestError,
    github_outputs,
    load_manifest,
    verify_repositories,
)


def test_release_manifest_uses_full_immutable_refs() -> None:
    manifest = load_manifest()
    outputs = github_outputs(manifest)

    assert outputs == {
        "release_id": "openopc-nu-2026-07-23",
        "llm_ref": "0f90652bf9ad07f69592430e0fe1df9f9743e3f6",
        "llm_version": "0.2.2",
        "resource_ref": "760e22acbb2c37e20542bcb4fdf782d58cd256e9",
        "resource_version": "0.2.2",
        "resource_stable_api": "1",
    }


def test_release_manifest_matches_local_dependency_checkouts() -> None:
    manifest = load_manifest()
    results = verify_repositories(
        manifest,
        workspace_root=Path(__file__).resolve().parents[2],
    )

    assert [item["package"] for item in results] == [
        "nu-llm-routing-lib",
        "nu-resource-gen-lib",
    ]
    assert all(item["version"] == "0.2.2" for item in results)


def test_release_manifest_rejects_mutable_or_escaping_ref(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    payload["packages"]["nu-llm-routing-lib"]["commit"] = "main"
    payload["packages"]["nu-resource-gen-lib"]["checkout_path"] = "../escape"
    path = tmp_path / "release.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReleaseManifestError, match="full lowercase SHA-1"):
        load_manifest(path)


def test_release_manifest_rejects_checkout_at_wrong_commit(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    payload["packages"]["nu-llm-routing-lib"]["checkout_path"] = "missing"
    path = tmp_path / "release.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    manifest = load_manifest(path)

    with pytest.raises(ReleaseManifestError, match="checkout is missing"):
        verify_repositories(manifest, workspace_root=tmp_path)
