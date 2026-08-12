from __future__ import annotations

import json
import subprocess
from copy import deepcopy
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
        "release_id": "openopc-nu-2026-08-12",
        "llm_ref": "f0f27187f8e88f0977449be63b670b5c0070f5cc",
        "llm_version": "0.4.0",
        "resource_ref": "1f2b0025c3314e1410fca3db4628a881ab67750b",
        "resource_version": "0.2.4",
        "resource_stable_api": "1",
    }


def test_release_manifest_matches_pinned_dependency_checkouts(tmp_path: Path) -> None:
    manifest = deepcopy(load_manifest())
    for name, entry in manifest["packages"].items():
        repository = tmp_path / entry["checkout_path"]
        repository.mkdir()
        (repository / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "{entry["version"]}"\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.email", "ci@example.test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", "CI"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "add", "pyproject.toml"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "commit", "-qm", "fixture"],
            check=True,
        )
        entry["commit"] = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    results = verify_repositories(
        manifest,
        workspace_root=tmp_path,
    )

    assert [item["package"] for item in results] == [
        "nu-llm-routing-lib",
        "nu-resource-gen-lib",
    ]
    assert [item["version"] for item in results] == ["0.4.0", "0.2.4"]


def test_release_manifest_matches_openopc_project_metadata() -> None:
    manifest = load_manifest()
    project = verify_project_metadata(manifest)

    assert project["package"] == "opc"
    assert project["version"] == "0.1.0"
    assert project["nu_pins"] == {
        "nu-llm-routing-lib": "0.4.0",
        "nu-resource-gen-lib": "0.2.4",
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
  "nu-resource-gen-lib==0.2.4",
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
  "nu-resource-gen-lib==0.2.4",
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


def test_ci_gates_byte_reproducible_release_wheels() -> None:
    ci_text = (
        Path(__file__).resolve().parents[1] / ".github" / ("work" + "flows") / "ci.yml"
    ).read_text(encoding="utf-8")

    assert (
        'LLM_SOURCE_DATE_EPOCH="$(git -C ../nu-llm-routing-lib show -s --format=%ct HEAD)"'
        in ci_text
    )
    assert (
        'RESOURCE_SOURCE_DATE_EPOCH="$(git -C ../nu-resource-gen-lib show -s --format=%ct HEAD)"'
        in ci_text
    )
    assert 'OPC_SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"' in ci_text
    assert (
        'cmp "$NU_WHEEL_ROOT"/llm/*.whl "$NU_WHEEL_ROOT"/llm-rebuild/*.whl' in ci_text
    )
    assert (
        'cmp "$NU_WHEEL_ROOT"/resource/*.whl "$NU_WHEEL_ROOT"/resource-rebuild/*.whl'
    ) in ci_text
    assert 'cmp dist/*.whl "$RUNNER_TEMP"/openopc-wheel-rebuild/*.whl' in ci_text
