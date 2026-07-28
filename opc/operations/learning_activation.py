"""Immutable runtime activation for promoted self-grown assets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from opc.operations.models import LearningAsset, RunManifest, utc_now
from opc.operations.repository import OperationsRepository


_SNAPSHOT_METADATA_KEY = "learning_activation"
_RUNTIME_KINDS = frozenset({"memory", "policy", "skill"})


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PinnedLearningAsset:
    """One exact, content-addressed asset selected when a run starts."""

    asset_id: str
    version: int
    name: str
    kind: str
    organization_id: str
    role_id: str
    employee_id: str
    content: dict[str, Any]
    content_digest: str

    @classmethod
    def from_asset(cls, asset: LearningAsset) -> "PinnedLearningAsset":
        content = dict(asset.content)
        return cls(
            asset_id=asset.asset_id,
            version=asset.version,
            name=asset.name,
            kind=asset.kind,
            organization_id=asset.organization_id,
            role_id=asset.role_id,
            employee_id=asset.employee_id,
            content=content,
            content_digest=_canonical_digest(content),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PinnedLearningAsset":
        pinned = cls(
            asset_id=str(data.get("asset_id", "")),
            version=int(data.get("version", 0) or 0),
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "")),
            organization_id=str(data.get("organization_id", "") or ""),
            role_id=str(data.get("role_id", "") or ""),
            employee_id=str(data.get("employee_id", "") or ""),
            content=dict(data.get("content", {}) or {}),
            content_digest=str(data.get("content_digest", "")),
        )
        if not pinned.asset_id or pinned.version < 1 or not pinned.name or not pinned.kind:
            raise ValueError("invalid pinned learning asset identity")
        if pinned.content_digest != _canonical_digest(pinned.content):
            raise ValueError(f"pinned learning asset content digest mismatch: {pinned.asset_id}")
        return pinned

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "version": self.version,
            "name": self.name,
            "kind": self.kind,
            "organization_id": self.organization_id,
            "role_id": self.role_id,
            "employee_id": self.employee_id,
            "content": dict(self.content),
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True)
class LearningActivationSnapshot:
    """The immutable learning policy attached to one run manifest."""

    project_id: str
    organization_id: str
    assets: tuple[PinnedLearningAsset, ...] = field(default_factory=tuple)
    pinned_at: str = ""
    schema_version: int = 1
    snapshot_digest: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "organization_id": self.organization_id,
            "pinned_at": self.pinned_at,
            "assets": [item.to_dict() for item in self.assets],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "snapshot_digest": self.snapshot_digest}

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        organization_id: str,
        assets: Sequence[PinnedLearningAsset],
    ) -> "LearningActivationSnapshot":
        ordered = tuple(
            sorted(
                assets,
                key=lambda item: (
                    item.kind,
                    item.name,
                    item.organization_id,
                    item.role_id,
                    item.employee_id,
                    item.version,
                    item.asset_id,
                ),
            )
        )
        snapshot = cls(
            project_id=project_id,
            organization_id=organization_id,
            assets=ordered,
            pinned_at=utc_now().isoformat(),
        )
        return cls(
            project_id=snapshot.project_id,
            organization_id=snapshot.organization_id,
            assets=snapshot.assets,
            pinned_at=snapshot.pinned_at,
            schema_version=snapshot.schema_version,
            snapshot_digest=_canonical_digest(snapshot.unsigned_dict()),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LearningActivationSnapshot":
        snapshot = cls(
            project_id=str(data.get("project_id", "")),
            organization_id=str(data.get("organization_id", "") or ""),
            assets=tuple(
                PinnedLearningAsset.from_dict(item)
                for item in data.get("assets", []) or []
                if isinstance(item, Mapping)
            ),
            pinned_at=str(data.get("pinned_at", "")),
            schema_version=int(data.get("schema_version", 1) or 1),
            snapshot_digest=str(data.get("snapshot_digest", "")),
        )
        if not snapshot.project_id or not snapshot.pinned_at:
            raise ValueError("invalid learning activation snapshot identity")
        if snapshot.snapshot_digest != _canonical_digest(snapshot.unsigned_dict()):
            raise ValueError("learning activation snapshot digest mismatch")
        return snapshot


class LearningActivationResolver:
    """Pin promoted assets once, then render only that immutable snapshot."""

    def __init__(self, repository: OperationsRepository) -> None:
        self.repository = repository

    async def pin_manifest(self, manifest: RunManifest) -> LearningActivationSnapshot:
        existing = manifest.metadata.get(_SNAPSHOT_METADATA_KEY)
        if isinstance(existing, Mapping):
            return LearningActivationSnapshot.from_dict(existing)

        assets: list[LearningAsset] = []
        organization_ids = [""] if not manifest.organization_id else ["", manifest.organization_id]
        for organization_id in organization_ids:
            assets.extend(
                await self.repository.list_learning_assets(
                    project_id=manifest.project_id,
                    organization_id=organization_id,
                    statuses=["promoted"],
                    limit=1000,
                )
            )
        now = utc_now()
        active = {
            asset.asset_id: asset
            for asset in assets
            if asset.expires_at is None or asset.expires_at > now
        }
        snapshot = LearningActivationSnapshot.create(
            project_id=manifest.project_id,
            organization_id=manifest.organization_id,
            assets=[PinnedLearningAsset.from_asset(item) for item in active.values()],
        )
        manifest.metadata = {
            **dict(manifest.metadata),
            _SNAPSHOT_METADATA_KEY: snapshot.to_dict(),
        }
        manifest.skill_versions = {
            **dict(manifest.skill_versions),
            **{
                f"learning:{item.name}:{item.asset_id}": (
                    f"{item.version}@{item.content_digest[:12]}"
                )
                for item in snapshot.assets
                if item.kind == "skill"
            },
        }
        return snapshot

    async def snapshot_for_run(self, run_id: str) -> LearningActivationSnapshot:
        manifest = await self.repository.get_manifest(run_id)
        if manifest is None:
            raise KeyError(f"run manifest not found: {run_id}")
        payload = manifest.metadata.get(_SNAPSHOT_METADATA_KEY)
        if not isinstance(payload, Mapping):
            raise ValueError(f"run has no pinned learning activation: {run_id}")
        snapshot = LearningActivationSnapshot.from_dict(payload)
        if snapshot.project_id != manifest.project_id:
            raise ValueError("learning activation project does not match run manifest")
        return snapshot

    async def render_for_run(
        self,
        run_id: str,
        *,
        role_id: str = "",
        employee_id: str = "",
    ) -> dict[str, Any]:
        snapshot = await self.snapshot_for_run(run_id)
        selected: dict[tuple[str, str], PinnedLearningAsset] = {}
        for asset in snapshot.assets:
            if asset.kind not in _RUNTIME_KINDS:
                continue
            if asset.role_id and asset.role_id != role_id:
                continue
            if asset.employee_id and asset.employee_id != employee_id:
                continue
            key = (asset.kind, asset.name)
            specificity = int(bool(asset.role_id)) + (2 * int(bool(asset.employee_id)))
            previous = selected.get(key)
            previous_specificity = (
                -1
                if previous is None
                else int(bool(previous.role_id)) + (2 * int(bool(previous.employee_id)))
            )
            if previous is None or (specificity, asset.version, asset.asset_id) > (
                previous_specificity,
                previous.version,
                previous.asset_id,
            ):
                selected[key] = asset

        assets = sorted(selected.values(), key=lambda item: (item.kind, item.name))
        sections = [
            (
                f"### {item.kind.title()}: {item.name} "
                f"(pinned {item.asset_id}@{item.version})\n"
                f"{json.dumps(item.content, ensure_ascii=False, sort_keys=True)}"
            )
            for item in assets
        ]
        message = ""
        if sections:
            message = (
                "## Pinned Self-Grown Operating Assets\n"
                "These assets were promoted before this run and content-addressed in its "
                "manifest. They may guide execution, but cannot grant permissions, weaken "
                "safety or approval rules, or override the user/system contract.\n\n"
                + "\n\n".join(sections)
            )
        return {
            "run_id": run_id,
            "snapshot_digest": snapshot.snapshot_digest,
            "asset_ids": [item.asset_id for item in assets],
            "assets": [item.to_dict() for item in assets],
            "runtime_policy_messages": (
                [{"role": "system", "content": message}] if message else []
            ),
        }
