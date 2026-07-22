"""Stable, policy-preserving bridge to ``nu-resource-gen-lib``."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import re
from typing import Any, Mapping

from loguru import logger

from opc.core.config import NUResourceGenConfig


_SCOPE_TOKEN_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def _scope_token(value: Any, fallback: str) -> str:
    normalized = _SCOPE_TOKEN_PATTERN.sub("-", str(value or "").strip()).strip("-._")
    return normalized[:120] or fallback


class NUResourceGenBridge:
    """Own one ResourceGenerator and keep all live calls behind local policy."""

    def __init__(
        self,
        config: NUResourceGenConfig,
        *,
        opc_home: Path,
        project_id: str | None = None,
    ) -> None:
        self.config = config
        self.opc_home = Path(opc_home)
        self.project_id = _scope_token(project_id, "default")
        self._generator: Any | None = None
        self._load_attempted = False
        self._load_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def available(self) -> bool:
        return self._load_generator() is not None

    @property
    def load_error(self) -> str:
        self._load_generator()
        return self._load_error

    def _runtime_path(self, configured: str, default: Path) -> Path:
        raw = str(configured or "").strip()
        path = Path(raw).expanduser() if raw else default
        if not path.is_absolute():
            path = self.opc_home / path
        return path.resolve(strict=False)

    def _load_generator(self) -> Any | None:
        if not self.enabled:
            return None
        if self._load_attempted:
            return self._generator
        self._load_attempted = True
        try:
            from nu_resource_gen_lib.api import ResourceGenerator
        except (ImportError, ModuleNotFoundError) as exc:
            self._load_error = f"nu-resource-gen-lib unavailable: {type(exc).__name__}"
            logger.info(self._load_error)
            return None

        ledger_root = self._runtime_path(
            self.config.ledger_root,
            self.opc_home / "resource_gen" / "ledger",
        )
        archive_root = self._runtime_path(
            self.config.artifact_archive_root,
            self.opc_home / "resource_gen" / "artifacts",
        )
        self._generator = ResourceGenerator(
            scope_path=f"openopc/project:{self.project_id}",
            ledger_root=ledger_root,
            record_ledger=bool(self.config.record_ledger),
            artifact_archive_root=archive_root,
            archive_artifacts=bool(self.config.archive_live_artifacts),
            require_artifact_archive=False,
        )
        return self._generator

    def status(self) -> dict[str, Any]:
        generator = self._load_generator()
        return {
            "enabled": self.enabled,
            "available": generator is not None,
            "error": self._load_error,
            "allow_live": bool(self.config.allow_live),
            "allowed_live_candidates": sorted(set(self.config.allowed_live_candidates)),
            "record_ledger": bool(self.config.record_ledger),
            "record_dry_runs": bool(self.config.record_dry_runs),
        }
    def health(self, *, detail: bool = True) -> dict[str, Any]:
        generator = self._require_generator()
        values = generator.health_detail() if detail else generator.health()
        return {"status": self.status(), "providers": values}

    def list_candidates(
        self,
        *,
        candidate_id: str | None = None,
        provider: str | None = None,
        category: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        generator = self._require_generator()
        if candidate_id:
            specs = [generator.get_candidate(str(candidate_id).strip())]
        else:
            specs = generator.list_candidates(
                provider=str(provider).strip() if provider else None,
                category=str(category).strip() if category else None,
            )
        maximum = min(
            max(1, int(limit or self.config.max_catalog_results)),
            int(self.config.max_catalog_results),
        )
        compact = [self._candidate_summary(spec) for spec in specs[:maximum]]
        return {
            "count": len(specs),
            "returned": len(compact),
            "truncated": len(specs) > len(compact),
            "candidates": compact,
        }

    def plan(
        self,
        *,
        candidate_id: str,
        prompt: str,
        params: Mapping[str, Any] | None = None,
        media: Mapping[str, Any] | None = None,
        task_type: str | None = None,
        quality_preset: str | None = None,
        task: Any = None,
    ) -> dict[str, Any]:
        generator = self._require_generator()
        request = self._request(
            prompt=prompt,
            params=params,
            media=media,
            task_type=task_type,
            quality_preset=quality_preset,
            task=task,
        )
        spec = generator.get_candidate(str(candidate_id).strip())
        from nu_resource_gen_lib.api import evaluate_execution_policy

        decision = evaluate_execution_policy(
            spec,
            request,
            policy=generator.execution_policy,
        )
        ledger_status = None
        if self.config.record_ledger and self.config.record_dry_runs:
            ledger_status = generator.record_dry_run(
                spec.candidate_id,
                request,
                selection={"mode": "openopc_explicit_dry_run"},
            )
        return {
            "dry_run": True,
            "candidate": self._candidate_summary(spec),
            "workload_class": str(spec.category),
            "policy": decision.to_dict(),
            "request": {
                "prompt_chars": len(request.prompt),
                "prompt_preview": request.prompt[:500],
                "param_keys": sorted(request.params),
                "media_keys": sorted(request.media),
                "task_type": request.task_type,
                "scope_path": request.scope_path,
                "quality_preset": request.quality_preset,
            },
            "ledger": ledger_status,
            "live_guard": self._live_guard(spec.candidate_id),
        }

    def generate(
        self,
        *,
        candidate_id: str,
        prompt: str,
        params: Mapping[str, Any] | None = None,
        media: Mapping[str, Any] | None = None,
        task_type: str | None = None,
        quality_preset: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        confirm_live: bool = False,
        task: Any = None,
    ) -> dict[str, Any]:
        generator = self._require_generator()
        normalized_candidate = str(candidate_id or "").strip()
        guard = self._live_guard(normalized_candidate, confirm_live=confirm_live)
        if not guard["allowed"]:
            raise PermissionError("; ".join(guard["blockers"]))
        request = self._request(
            prompt=prompt,
            params=params,
            media=media,
            task_type=task_type,
            quality_preset=quality_preset,
            timeout=timeout,
            max_retries=max_retries,
            task=task,
        )
        result = generator.generate(normalized_candidate, request)
        return {
            "candidate_id": result.candidate_id,
            "provider": result.provider,
            "model": result.model,
            "status": result.status,
            "output_text": result.output_text,
            "asset_uri": result.asset_uri,
            "job_id": result.job_id,
            "latency_ms": result.latency_ms,
            "cost": result.cost,
            "cost_unit": result.cost_unit,
            "usage": dict(result.usage or {}),
            "request_id": result.request_id,
            "routing_decision_id": result.routing_decision_id,
            "attempt_id": result.attempt_id,
            "artifact_id": result.artifact_id,
            "pipeline_run_id": result.pipeline_run_id,
            "policy_version": result.policy_version,
        }

    def _request(
        self,
        *,
        prompt: str,
        params: Mapping[str, Any] | None,
        media: Mapping[str, Any] | None,
        task_type: str | None,
        quality_preset: str | None,
        task: Any,
        timeout: float = 120.0,
        max_retries: int = 2,
    ) -> Any:
        from nu_resource_gen_lib.api import ResourceRequest

        if not isinstance(params or {}, Mapping) or not isinstance(media or {}, Mapping):
            raise ValueError("params and media must be JSON objects")
        task_id = _scope_token(getattr(task, "id", None), "unbound")
        project_id = _scope_token(getattr(task, "project_id", None), self.project_id)
        bounded_timeout = min(
            max(1.0, float(timeout)),
            float(self.config.max_timeout_seconds),
        )
        return ResourceRequest(
            prompt=str(prompt or ""),
            params=dict(params or {}),
            media=dict(media or {}),
            timeout=bounded_timeout,
            max_retries=max(0, min(int(max_retries), 5)),
            retry_submit=False,
            quality_preset=(str(quality_preset).strip() if quality_preset else None),
            task_type=(str(task_type).strip() if task_type else None),
            scope_path=f"openopc/project:{project_id}/task:{task_id}",
        )

    def _live_guard(self, candidate_id: str, *, confirm_live: bool = False) -> dict[str, Any]:
        allowed_candidates = {
            str(value).strip()
            for value in self.config.allowed_live_candidates
            if str(value).strip()
        }
        blockers: list[str] = []
        if not self.config.allow_live:
            blockers.append("NU resource live execution is disabled by system.nu_resource_gen.allow_live")
        if candidate_id not in allowed_candidates:
            blockers.append(f"candidate {candidate_id!r} is not in allowed_live_candidates")
        if not confirm_live:
            blockers.append("confirm_live=true is required for a provider call")
        return {
            "allowed": not blockers,
            "candidate_id": candidate_id,
            "blockers": blockers,
        }

    def _require_generator(self) -> Any:
        generator = self._load_generator()
        if generator is None:
            raise RuntimeError(self._load_error or "NU resource generation is disabled")
        return generator

    @staticmethod
    def _candidate_summary(spec: Any) -> dict[str, Any]:
        payload = asdict(spec)
        extras = dict(payload.pop("extras", {}) or {})
        credential_env = list(payload.get("credential_env") or ())
        return {
            **payload,
            "credential_env": credential_env,
            "credentials_configured": any(os.environ.get(name) for name in credential_env),
            "status": extras.get("status") or extras.get("implementation_status") or "",
            "dry_run_recommended": bool(extras.get("dry_run_recommended", False)),
            "simulation_only": bool(extras.get("simulation_only", False)),
            "supported_task_types": list(extras.get("supported_task_types") or ()),
            "deprecated": bool(extras.get("deprecated", False)),
        }
