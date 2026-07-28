"""Opt-in side store for shadow decision response artifacts.

The shadow transport ledger is intentionally content-free, which forces
judges to re-capture the served and challenger texts before scoring. This
store keeps those texts locally, keyed by decision id, with SHA-256 digests
that the observation binding later verifies. The ledger itself stays
content-free — the store is a local convenience for the judgment step, and
tampering with a stored artifact is caught because the recomputed digest no
longer matches the index (or the digest bound into the observation).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

INDEX_NAME = "index.json"
SERVED_NAME = "served.md"
CHALLENGER_NAME = "challenger.md"

_DECISION_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


class ShadowArtifactStore:
    """Content-addressed served/challenger artifact pairs per decision."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def add(
        self,
        decision_id: str,
        *,
        served_text: str,
        challenger_text: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        decision = _validate_decision_id(decision_id)
        directory = self.root / decision
        if directory.exists() and not overwrite:
            raise FileExistsError(
                f"shadow artifacts already stored for decision {decision}"
            )
        directory.mkdir(parents=True, exist_ok=True)
        served = str(served_text)
        challenger = str(challenger_text)
        if not served.strip() or not challenger.strip():
            raise ValueError("both served and challenger artifacts must be non-empty")
        (directory / SERVED_NAME).write_text(served, encoding="utf-8")
        (directory / CHALLENGER_NAME).write_text(challenger, encoding="utf-8")
        index = {
            "schema_version": 1,
            "decision_id": decision,
            "served_path": SERVED_NAME,
            "served_sha256": _sha256(served),
            "challenger_path": CHALLENGER_NAME,
            "challenger_sha256": _sha256(challenger),
        }
        (directory / INDEX_NAME).write_text(
            json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return index

    def get(self, decision_id: str) -> dict[str, Any]:
        """Load the index and fail closed if any stored artifact was altered."""

        decision = _validate_decision_id(decision_id)
        directory = self.root / decision
        index_path = directory / INDEX_NAME
        if not index_path.exists():
            raise KeyError(f"no shadow artifacts stored for decision {decision}")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for role in ("served", "challenger"):
            body = (directory / str(index[f"{role}_path"])).read_text(encoding="utf-8")
            if _sha256(body) != index[f"{role}_sha256"]:
                raise ValueError(
                    f"stored {role} artifact for decision {decision} no longer "
                    "matches its recorded digest"
                )
        return dict(index)

    def digests(self, decision_id: str) -> dict[str, str]:
        index = self.get(decision_id)
        return {
            "served_artifact_digest": str(index["served_sha256"]),
            "challenger_artifact_digest": str(index["challenger_sha256"]),
        }


def _validate_decision_id(decision_id: str) -> str:
    decision = str(decision_id or "").strip()
    if not _DECISION_ID.fullmatch(decision):
        raise ValueError(
            "decision_id must be 1-128 safe filename characters"
        )
    return decision


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
