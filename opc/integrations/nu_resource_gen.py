"""Stable, policy-preserving bridge to ``nu-resource-gen-lib``."""

from __future__ import annotations

from dataclasses import asdict
import json
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


def _candidate_runtime_path(candidate_id: str, key: str, configured: str) -> Path:
    """Mirror trusted RF-DETR runtime resolution without mutating its catalog."""

    path = Path(configured).expanduser()
    if key != "python" or not str(candidate_id).startswith("local_rfdetr_"):
        return path
    override = str(os.environ.get("NU_RFDETR_PYTHON", "") or "").strip()
    if override:
        return Path(override).expanduser()
    if path.is_file() or path.parent.parent.name != "rfdetr-py311":
        return path
    py312 = path.parent.parent.parent / "rfdetr-py312" / "bin" / path.name
    return py312 if py312.is_file() else path


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
            "allow_local_quality_execution": bool(
                self.config.allow_local_quality_execution
            ),
            "local_quality_candidates": sorted(
                set(self.config.local_quality_candidates)
            ),
            "record_ledger": bool(self.config.record_ledger),
            "record_dry_runs": bool(self.config.record_dry_runs),
        }
    def health(self, *, detail: bool = True) -> dict[str, Any]:
        generator = self._require_generator()
        values = generator.health_detail() if detail else generator.health()
        return {"status": self.status(), "providers": values}

    def provider_readiness(self, provider: str) -> dict[str, Any]:
        """Return a normalized, secret-free readiness view for one provider."""
        normalized = str(provider or "").strip().lower()
        try:
            providers = self.health(detail=True).get("providers", {})
            raw_value = providers.get(normalized) if isinstance(providers, Mapping) else None
            if isinstance(raw_value, Mapping):
                raw = dict(raw_value)
            elif isinstance(raw_value, bool):
                raw = {"has_credentials": raw_value}
            elif isinstance(raw_value, str) and raw_value.strip():
                detail = raw_value.strip()
                lowered = detail.lower()
                ready = any(token in lowered for token in ("ready", "available", "healthy"))
                blocked = any(
                    token in lowered
                    for token in ("unavailable", "missing", "not configured", "error", "failed")
                )
                raw = {
                    "has_credentials": bool(ready and not blocked),
                    "status": detail,
                }
            else:
                raw = {}
        except Exception as exc:
            return {
                "credential_ready": False,
                "transport_ready": False,
                "detail": f"health probe failed: {type(exc).__name__}: {exc}"[:500],
            }
        if not raw:
            return {
                "credential_ready": False,
                "transport_ready": False,
                "detail": f"provider health unavailable: {normalized}"[:500],
            }
        credential_keys = ("has_credentials", "credentials_configured", "authenticated")
        has_explicit_signal = any(key in raw for key in credential_keys)
        credential_ready = bool(
            any(bool(raw.get(key)) for key in credential_keys)
            if has_explicit_signal
            else raw.get("available") or raw.get("healthy")
        )
        explicitly_unavailable = raw.get("available") is False or raw.get("healthy") is False
        transport_ready = bool(credential_ready and not explicitly_unavailable)
        return {
            "credential_ready": credential_ready,
            "transport_ready": transport_ready,
            "detail": str(raw.get("detail") or raw.get("status") or "")[:500],
        }

    def candidate_readiness(self, candidate_id: str) -> dict[str, Any]:
        """Check candidate-local runtime files in addition to provider health."""

        generator = self._require_generator()
        spec = generator.get_candidate(str(candidate_id or "").strip())
        provider = str(spec.provider or "").strip().lower()
        provider_state = self.provider_readiness(provider)
        extras = dict(spec.extras or {})
        path_checks: dict[str, dict[str, Any]] = {}
        for key in ("python", "smoke_script", "script"):
            raw = str(extras.get(key, "") or "").strip()
            if not raw:
                continue
            path = _candidate_runtime_path(spec.candidate_id, key, raw)
            path_checks[key] = {"path": str(path), "exists": path.is_file()}
        missing = [key for key, row in path_checks.items() if not row["exists"]]
        local_no_secret = provider.startswith("local_")
        credential_ready = bool(
            provider_state.get("credential_ready", False) or local_no_secret
        )
        transport_ready = bool(
            credential_ready
            and (provider_state.get("transport_ready", False) or local_no_secret)
            and not missing
            and not bool(extras.get("deprecated", False))
            and not bool(extras.get("simulation_only", False))
        )
        return {
            "candidate_id": spec.candidate_id,
            "provider": spec.provider,
            "credential_ready": credential_ready,
            "transport_ready": transport_ready,
            "local_no_secret": local_no_secret,
            "path_checks": path_checks,
            "missing_runtime_files": missing,
            "deprecated": bool(extras.get("deprecated", False)),
            "simulation_only": bool(extras.get("simulation_only", False)),
            "detail": (
                "candidate runtime ready"
                if transport_ready
                else "candidate runtime is missing required files or is not executable"
            ),
        }

    def quality_execution_status(self, candidate_id: str) -> dict[str, Any]:
        """Authorize only verified-free, local, non-simulated quality candidates."""

        normalized = str(candidate_id or "").strip()
        blockers: list[str] = []
        if not self.config.allow_local_quality_execution:
            blockers.append("local quality execution is disabled")
        allowed = {
            str(value).strip()
            for value in self.config.local_quality_candidates
            if str(value).strip()
        }
        if normalized not in allowed:
            blockers.append(f"quality candidate {normalized!r} is not allowlisted")
        generator = self._require_generator()
        spec = generator.get_candidate(normalized)
        extras = dict(spec.extras or {})
        if not str(spec.provider).startswith("local_"):
            blockers.append("quality candidate must use a local provider")
        if spec.cost != 0.0 or str(spec.cost_unit).lower() != "local":
            blockers.append("quality candidate cost must be verified local/free")
        if bool(extras.get("deprecated", False)):
            blockers.append("quality candidate is deprecated")
        if bool(extras.get("simulation_only", False)):
            blockers.append("simulation-only output cannot prove artifact quality")
        readiness = self.candidate_readiness(normalized)
        if not readiness["transport_ready"]:
            blockers.append(str(readiness["detail"]))
        return {
            "allowed": not blockers,
            "candidate_id": normalized,
            "blockers": blockers,
            "readiness": readiness,
        }

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

    def evaluate_prompt(
        self,
        *,
        candidate_id: str,
        prompt: str,
        params: Mapping[str, Any] | None = None,
        media: Mapping[str, Any] | None = None,
        task_type: str | None = None,
        metric_threshold: float = 70.0,
        overall_threshold: float = 70.0,
    ) -> dict[str, Any]:
        """Run the shared, provider-specific prompt gate without generation."""

        generator = self._require_generator()
        request = self._request(
            prompt=prompt,
            params=params,
            media=media,
            task_type=task_type,
            quality_preset=None,
            task=None,
        )
        return dict(
            generator.evaluate_prompt(
                str(candidate_id).strip(),
                request,
                metric_threshold=float(metric_threshold),
                overall_threshold=float(overall_threshold),
            )
        )

    def evaluate_artifact_quality(
        self,
        metadata: Mapping[str, Any],
        generated: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Run a bounded local grounding gate against one generated artifact."""

        candidate_id = str(metadata.get("qa_candidate_id", "") or "").strip()
        status = self.quality_execution_status(candidate_id)
        if not status["allowed"]:
            raise PermissionError("; ".join(status["blockers"]))
        scope = str(metadata.get("quality_scope", "object_presence") or "").strip()
        expected_classes = _string_list(metadata.get("expected_classes"))
        asset_ref = str(generated.get("asset_uri", "") or "").strip()
        if asset_ref.startswith("file://"):
            asset_ref = asset_ref[7:]
        asset = Path(asset_ref).expanduser()
        if not asset.is_file():
            raise ValueError("local quality execution requires an existing artifact file")

        request_id = _scope_token(metadata.get("request_id"), "quality")
        output_dir = (
            self.opc_home
            / "resource_gen"
            / "quality"
            / self.project_id
            / request_id
            / _scope_token(candidate_id, "candidate")
        )
        raw_qa_params = metadata.get("qa_params", {})
        qa_params = dict(raw_qa_params) if isinstance(raw_qa_params, Mapping) else {}
        params: dict[str, Any] = {
            "output_dir": str(output_dir),
            "skip_sample_downloads": True,
        }
        if "threshold" in qa_params:
            params["threshold"] = qa_params["threshold"]
        if expected_classes:
            params["expected_classes"] = ",".join(expected_classes)
        request = self._request(
            prompt=(
                "Verify expected object presence and return grounded bounding-box evidence. "
                f"Expected classes: {', '.join(expected_classes) or 'not supplied'}."
            ),
            params=params,
            media={"image": str(asset.resolve())},
            task_type="grounding_evidence",
            quality_preset=None,
            task=None,
            timeout=float(self.config.max_timeout_seconds),
            max_retries=0,
        )
        generator = self._require_generator()
        result = generator.generate(candidate_id, request)
        try:
            payload = json.loads(str(result.output_text or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("quality candidate returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("quality candidate JSON must be an object")
        findings = [
            dict(item)
            for item in payload.get("findings", []) or []
            if isinstance(item, Mapping)
        ]
        evidence_details: list[dict[str, Any]] = []
        grounded_classes: set[str] = set()
        for finding in findings:
            evidence_type = next(
                (
                    key
                    for key in ("bbox", "mask", "point", "points")
                    if finding.get(key) not in (None, "", [], {})
                ),
                "",
            )
            if not evidence_type:
                continue
            class_name = str(finding.get("class_name", "") or "").strip()
            if class_name:
                grounded_classes.add(class_name)
            evidence_details.append(
                {
                    "type": evidence_type,
                    "class_name": class_name,
                    "value": finding[evidence_type],
                    "confidence": finding.get("confidence"),
                }
            )
        missing = _string_list(payload.get("missing_expected_classes"))
        supported_scope = scope in {"object_presence", "prop_continuity"}
        scope_satisfied = bool(
            supported_scope
            and expected_classes
            and not missing
            and set(expected_classes).issubset(grounded_classes)
        )
        evidence = [
            f"{item['type']}:{item['class_name'] or 'unclassified'}"
            for item in evidence_details
        ]
        return {
            "candidate_id": result.candidate_id,
            "provider": result.provider,
            "model": result.model,
            "status": result.status,
            "score": payload.get("score", 0.0),
            "summary": str(payload.get("summary", "") or ""),
            "findings": findings,
            "grounded": bool(evidence_details),
            "evidence": evidence,
            "evidence_details": evidence_details,
            "quality_scope": scope,
            "scope_satisfied": scope_satisfied,
            "scope_limit_reason": (
                ""
                if scope_satisfied
                else (
                    "RF-DETR can auto-pass only object_presence/prop_continuity "
                    "with explicit expected_classes and grounded evidence"
                )
            ),
            "expected_classes": expected_classes,
            "missing_expected_classes": missing,
            "detected_classes": _string_list(payload.get("detected_classes")),
            "qa_asset_uri": result.asset_uri,
            "job_id": result.job_id,
            "latency_ms": result.latency_ms,
            "cost": result.cost,
            "cost_unit": result.cost_unit,
            "usage": dict(result.usage or {}),
            "usage_accounting": {
                "measured": True,
                "source": "local_provider_reported",
                "cost_usd": 0.0,
            },
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
            "execution": str(extras.get("execution", "") or ""),
            "device": str(extras.get("device", "") or ""),
            "requires_gpu_arch": str(extras.get("requires_gpu_arch", "") or ""),
            "approx_loaded_vram_gb": extras.get("approx_loaded_vram_gb"),
            "approx_weights_vram_gb": extras.get("approx_weights_vram_gb"),
        }


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = []
    return list(
        dict.fromkeys(str(item).strip() for item in items if str(item).strip())
    )
