#!/usr/bin/env python3
"""Validate and expose the immutable NU dependency release consumed by OpenOPC."""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "config" / "nu_release_manifest.json"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_PACKAGE_KEYS = ("nu-llm-routing-lib", "nu-resource-gen-lib")
_EXACT_DEPENDENCY = re.compile(
    r"^\s*([A-Za-z0-9_.-]+)\s*==\s*([^;\s]+)(?:\s*;.*)?$"
)


class ReleaseManifestError(ValueError):
    """Raised when a release manifest or checked-out dependency is inconsistent."""


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ReleaseManifestError("release manifest must be a JSON object")
    if int(raw.get("schema_version", 0) or 0) != 1:
        raise ReleaseManifestError("release manifest schema_version must be 1")
    if not str(raw.get("release_id", "") or "").strip():
        raise ReleaseManifestError("release manifest release_id is required")
    openopc = raw.get("openopc")
    if not isinstance(openopc, dict):
        raise ReleaseManifestError("release manifest openopc must be an object")
    for field in ("package", "version"):
        if not str(openopc.get(field, "") or "").strip():
            raise ReleaseManifestError(f"openopc.{field} is required")
    packages = raw.get("packages")
    if not isinstance(packages, dict) or set(packages) != set(_PACKAGE_KEYS):
        raise ReleaseManifestError(
            f"release manifest packages must be exactly {list(_PACKAGE_KEYS)}"
        )
    for name in _PACKAGE_KEYS:
        entry = packages[name]
        if not isinstance(entry, dict):
            raise ReleaseManifestError(f"{name} release entry must be an object")
        for field in ("module", "version", "repository", "checkout_path", "commit"):
            if not str(entry.get(field, "") or "").strip():
                raise ReleaseManifestError(f"{name}.{field} is required")
        commit = str(entry["commit"])
        if not _FULL_SHA.fullmatch(commit):
            raise ReleaseManifestError(f"{name}.commit must be a full lowercase SHA-1")
        if Path(str(entry["checkout_path"])).is_absolute() or ".." in Path(
            str(entry["checkout_path"])
        ).parts:
            raise ReleaseManifestError(f"{name}.checkout_path must stay inside the workspace")
    resource_api = str(
        packages["nu-resource-gen-lib"].get("stable_api_version", "") or ""
    )
    if not resource_api:
        raise ReleaseManifestError(
            "nu-resource-gen-lib.stable_api_version is required"
        )
    return raw


def verify_project_metadata(
    manifest: Mapping[str, Any],
    *,
    project_path: Path = ROOT / "pyproject.toml",
) -> dict[str, Any]:
    """Ensure package metadata duplicates only values pinned by the manifest."""

    project_text = project_path.read_text(encoding="utf-8")
    project = _project_metadata(project_text, project_path)

    expected_openopc = dict(manifest["openopc"])
    actual_name = str(project.get("name", "") or "")
    actual_version = str(project.get("version", "") or "")
    expected_name = str(expected_openopc["package"])
    expected_version = str(expected_openopc["version"])
    if actual_name != expected_name:
        raise ReleaseManifestError(
            f"OpenOPC project name {actual_name!r} != manifest {expected_name!r}"
        )
    if actual_version != expected_version:
        raise ReleaseManifestError(
            f"OpenOPC project version {actual_version!r} != manifest {expected_version!r}"
        )

    nu_dependencies = _nu_optional_dependencies(project_text, project_path)

    actual_pins: dict[str, str] = {}
    expected_packages = dict(manifest["packages"])
    for dependency in nu_dependencies:
        match = _EXACT_DEPENDENCY.fullmatch(str(dependency))
        if match is None:
            raise ReleaseManifestError(
                f"NU dependency must use an exact == pin: {dependency!r}"
            )
        name, version = match.groups()
        normalized = name.lower().replace("_", "-")
        if normalized in actual_pins:
            raise ReleaseManifestError(f"duplicate NU dependency pin: {normalized}")
        actual_pins[normalized] = version

    if set(actual_pins) != set(expected_packages):
        raise ReleaseManifestError(
            "pyproject NU dependencies must be exactly "
            f"{sorted(expected_packages)}; got {sorted(actual_pins)}"
        )
    for name, entry in expected_packages.items():
        expected = str(dict(entry)["version"])
        if actual_pins[name] != expected:
            raise ReleaseManifestError(
                f"pyproject pin {name}=={actual_pins[name]} != manifest {expected}"
            )

    return {
        "package": actual_name,
        "version": actual_version,
        "nu_pins": dict(sorted(actual_pins.items())),
        "path": str(project_path.resolve()),
    }


def github_outputs(manifest: Mapping[str, Any]) -> dict[str, str]:
    packages = dict(manifest["packages"])
    llm = dict(packages["nu-llm-routing-lib"])
    resource = dict(packages["nu-resource-gen-lib"])
    return {
        "release_id": str(manifest["release_id"]),
        "llm_ref": str(llm["commit"]),
        "llm_version": str(llm["version"]),
        "resource_ref": str(resource["commit"]),
        "resource_version": str(resource["version"]),
        "resource_stable_api": str(resource["stable_api_version"]),
    }


def verify_repositories(
    manifest: Mapping[str, Any],
    *,
    workspace_root: Path,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name, raw_entry in dict(manifest["packages"]).items():
        entry = dict(raw_entry)
        repository_path = workspace_root / str(entry["checkout_path"])
        if not repository_path.is_dir():
            raise ReleaseManifestError(
                f"{name} checkout is missing: {repository_path}"
            )
        head = _git(repository_path, "rev-parse", "HEAD")
        expected_commit = str(entry["commit"])
        if head != expected_commit:
            raise ReleaseManifestError(
                f"{name} HEAD {head} != pinned commit {expected_commit}"
            )
        package_version = _project_version(repository_path / "pyproject.toml")
        expected_version = str(entry["version"])
        if package_version != expected_version:
            raise ReleaseManifestError(
                f"{name} project version {package_version} != {expected_version}"
            )
        results.append(
            {
                "package": name,
                "path": str(repository_path.resolve()),
                "commit": head,
                "version": package_version,
            }
        )
    return results


def verify_installed(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for name, raw_entry in dict(manifest["packages"]).items():
        entry = dict(raw_entry)
        api = importlib.import_module(f"{entry['module']}.api")
        installed = str(getattr(api, "__version__", "") or "")
        expected = str(entry["version"])
        if installed != expected:
            raise ReleaseManifestError(
                f"installed {name} version {installed} != {expected}"
            )
        result = {"package": name, "version": installed}
        stable_api = entry.get("stable_api_version")
        if stable_api is not None:
            installed_api = str(getattr(api, "STABLE_API_VERSION", "") or "")
            if installed_api != str(stable_api):
                raise ReleaseManifestError(
                    f"installed {name} stable API {installed_api} != {stable_api}"
                )
            result["stable_api_version"] = installed_api
        results.append(result)
    return results


def build_report(
    manifest: Mapping[str, Any],
    *,
    project_path: Path = ROOT / "pyproject.toml",
    workspace_root: Path | None = None,
    check_installed: bool = False,
) -> dict[str, Any]:
    project = verify_project_metadata(manifest, project_path=project_path)
    repositories = (
        verify_repositories(manifest, workspace_root=workspace_root)
        if workspace_root is not None
        else []
    )
    installed = verify_installed(manifest) if check_installed else []
    return {
        "ok": True,
        "release_id": str(manifest["release_id"]),
        "project": project,
        "repositories": repositories,
        "installed": installed,
        "pins": github_outputs(manifest),
    }


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _project_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    project_match = re.search(r"(?ms)^\[project\]\s*$.*?^version\s*=\s*['\"]([^'\"]+)['\"]", text)
    if project_match is None:
        raise ReleaseManifestError(f"project version is missing from {path}")
    return project_match.group(1).strip()


def _project_metadata(text: str, path: Path) -> dict[str, str]:
    section = re.search(
        r"(?ms)^\[project\]\s*$"
        r"(?P<body>.*?)(?=^\[[^\n]+\]\s*$|\Z)",
        text,
    )
    if section is None:
        raise ReleaseManifestError(f"[project] is missing from {path}")
    body = section.group("body")
    values: dict[str, str] = {}
    for field in ("name", "version"):
        match = re.search(
            rf"(?m)^\s*{field}\s*=\s*['\"]([^'\"]+)['\"]\s*$",
            body,
        )
        if match is not None:
            values[field] = match.group(1).strip()
    return values


def _nu_optional_dependencies(text: str, path: Path) -> list[str]:
    section = re.search(
        r"(?ms)^\[project\.optional-dependencies\]\s*$"
        r"(?P<body>.*?)(?=^\[[^\n]+\]\s*$|\Z)",
        text,
    )
    if section is None:
        raise ReleaseManifestError(
            f"[project.optional-dependencies].nu is missing from {path}"
        )
    match = re.search(
        r"(?ms)^\s*nu\s*=\s*(?P<value>\[.*?^\s*\])",
        section.group("body"),
    )
    if match is None:
        raise ReleaseManifestError(
            f"[project.optional-dependencies].nu is missing from {path}"
        )
    try:
        value = ast.literal_eval(match.group("value"))
    except (SyntaxError, ValueError) as exc:
        raise ReleaseManifestError(
            f"[project.optional-dependencies].nu is invalid in {path}"
        ) from exc
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ReleaseManifestError(
            f"[project.optional-dependencies].nu must be a string array in {path}"
        )
    return value


def _write_github_output(path: Path, values: Mapping[str, str]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ReleaseManifestError(f"GitHub output {key} contains a newline")
            output.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="Workspace containing the dependency checkout_path directories",
    )
    parser.add_argument("--check-installed", action="store_true")
    parser.add_argument("--emit-github-output", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.manifest)
        if args.emit_github_output is not None:
            _write_github_output(
                args.emit_github_output,
                github_outputs(manifest),
            )
        report = build_report(
            manifest,
            workspace_root=args.workspace_root,
            check_installed=args.check_installed,
        )
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
    except (OSError, ReleaseManifestError, subprocess.CalledProcessError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
