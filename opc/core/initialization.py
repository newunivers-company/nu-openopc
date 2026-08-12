"""Structural initialization diagnostics for an OPC home directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


REQUIRED_CONFIG_FILES: tuple[str, ...] = (
    "system_config.yaml",
    "llm_config.yaml",
    "agent_config.yaml",
    "channel_config.yaml",
)


@dataclass(frozen=True)
class InitializationReport:
    state: str
    opc_home: Path
    config_dir: Path
    present: tuple[str, ...]
    missing: tuple[str, ...]
    invalid: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.state == "initialized"

    def to_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "ready": self.ready,
            "opc_home": str(self.opc_home),
            "config_dir": str(self.config_dir),
            "present": list(self.present),
            "missing": list(self.missing),
            "invalid": list(self.invalid),
        }


def inspect_initialization(opc_home: Path) -> InitializationReport:
    """Classify an OPC home as uninitialized, partial, or initialized.

    Readiness requires all four split runtime configuration files to exist and
    contain YAML mappings. Other artifacts (for example a Corporate org file
    created by an early UI launch) make the state partial, never initialized.
    """
    home = Path(opc_home)
    config_dir = home / "config"
    present: list[str] = []
    missing: list[str] = []
    invalid: list[str] = []

    for name in REQUIRED_CONFIG_FILES:
        path = config_dir / name
        if not path.is_file():
            missing.append(name)
            continue
        present.append(name)
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                invalid.append(name)
        except (OSError, UnicodeError, yaml.YAMLError):
            invalid.append(name)

    has_artifacts = config_dir.is_dir() and any(config_dir.iterdir())
    if not has_artifacts and not present:
        state = "uninitialized"
    elif not missing and not invalid:
        state = "initialized"
    else:
        state = "partial"
    return InitializationReport(
        state=state,
        opc_home=home,
        config_dir=config_dir,
        present=tuple(present),
        missing=tuple(missing),
        invalid=tuple(invalid),
    )
