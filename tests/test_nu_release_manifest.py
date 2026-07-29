from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify_nu_release_manifest import (
    DEFAULT_MANIFEST,
    ReleaseManifestError,
    github_outputs,
    load_manifest,
    verify_project_metadata,
    verify_repositories,
)


def test_release_manifest_uses_full_immutable_refs() -> None:
    manifest = load_manifest()
    outputs = github_outputs(manifest)

    assert outputs == {
        "release_id": "openopc-nu-2026-07-29",
        "llm_ref": "513b277e5edc59de084204743bcfa61b25e32bea",
        "llm_version": "0.4.0",
        "resource_ref": "e29fa40afeb707881c97142daf03749ef6d9c80f",
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
    assert [item["version"] for item in results] == ["0.4.0", "0.2.2"]


def test_release_manifest_matches_openopc_project_metadata() -> None:
    manifest = load_manifest()
    project = verify_project_metadata(manifest)

    assert project["package"] == "opc"
    assert project["version"] == "0.1.0"
    assert project["nu_pins"] == {
        "nu-llm-routing-lib": "0.4.0",
        "nu-resource-gen-lib": "0.2.2",
    }


def test_release_manifest_rejects_project_pin_drift(tmp_path: Path) -> None:
    project_path = tmp_path / "pyproject.toml"
    project_path.write_text(
        """
[project]
name = "opc"
version = "0.1.0"

[project.optional-dependencies]
nu = [
  "nu-llm-routing-lib==0.3.9",
  "nu-resource-gen-lib==0.2.2",
]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseManifestError, match="pyproject pin"):
        verify_project_metadata(load_manifest(), project_path=project_path)


def test_release_manifest_requires_exact_project_pins(tmp_path: Path) -> None:
    project_path = tmp_path / "pyproject.toml"
    project_path.write_text(
        """
[project]
name = "opc"
version = "0.1.0"

[project.optional-dependencies]
nu = [
  "nu-llm-routing-lib>=0.4.0",
  "nu-resource-gen-lib==0.2.2",
]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseManifestError, match="exact == pin"):
        verify_project_metadata(load_manifest(), project_path=project_path)


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
