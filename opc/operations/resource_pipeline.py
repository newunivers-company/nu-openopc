"""Approval-gated NU resource execution with prompt and artifact quality gates."""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from opc.operations.capabilities import UnifiedCapabilityBroker
from opc.operations.models import CapabilityKind, CapabilityRequest, utc_now


QualityExecutor = Callable[[Mapping[str, Any], Mapping[str, Any]], Awaitable[Any] | Any]


@dataclass
class ResourcePipelineRequest:
    prompt: str
    candidate_id: str
    task_type: str
    project_id: str = "default"
    run_id: str = ""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    params: dict[str, Any] = field(default_factory=dict)
    media: dict[str, Any] = field(default_factory=dict)
    allow_live: bool = False
    confirm_live: bool = False
    require_free: bool = True
    max_cost_usd: float | None = 0.0
    approval_token: str = ""
    planner_candidate_id: str = "llm-planner-primary"
    qa_candidate_id: str = "local_gemma4_12b_nvfp4"
    quality_preset: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    gpu_free_vram_mib: int = 0
    hardware_profile: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResourcePipelineRequest":
        cost = data.get("max_cost_usd", 0.0)
        return cls(
            request_id=str(data.get("request_id", "") or str(uuid.uuid4())),
            prompt=str(data.get("prompt", "") or ""),
            candidate_id=str(data.get("candidate_id", "") or ""),
            task_type=str(data.get("task_type", "") or ""),
            project_id=str(data.get("project_id", "default") or "default"),
            run_id=str(data.get("run_id", "") or ""),
            params=dict(data.get("params", {}) or {}),
            media=dict(data.get("media", {}) or {}),
            allow_live=bool(data.get("allow_live", False)),
            confirm_live=bool(data.get("confirm_live", False)),
            require_free=bool(data.get("require_free", True)),
            max_cost_usd=None if cost is None else float(cost),
            approval_token=str(data.get("approval_token", "") or ""),
            planner_candidate_id=str(
                data.get("planner_candidate_id", "llm-planner-primary")
                or "llm-planner-primary"
            ),
            qa_candidate_id=str(data.get("qa_candidate_id", "local_gemma4_12b_nvfp4") or ""),
            quality_preset=str(data.get("quality_preset", "") or ""),
            metadata=dict(data.get("metadata", {}) or {}),
            gpu_free_vram_mib=max(0, int(data.get("gpu_free_vram_mib", 0) or 0)),
            hardware_profile=str(data.get("hardware_profile", "") or ""),
        )

    def validate(self) -> None:
        if not self.prompt.strip():
            raise ValueError("resource pipeline prompt is required")
        if not self.candidate_id.strip() or not self.task_type.strip():
            raise ValueError("resource pipeline candidate_id and task_type are required")
        if self.max_cost_usd is not None and self.max_cost_usd < 0:
            raise ValueError("max_cost_usd must be non-negative or null")


@dataclass
class ResourcePipelineResult:
    request_id: str
    project_id: str
    candidate_id: str
    status: str
    decision: str
    stages: dict[str, Any]
    blockers: list[str] = field(default_factory=list)
    artifact_manifest: str = ""
    generated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        return payload


class ResourceApprovalTokenIssuer:
    """Issue short-lived HMAC approvals bound to one prompt and candidate."""

    def __init__(self, secret: str | bytes) -> None:
        self.secret = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if len(self.secret) < 16:
            raise ValueError("resource approval secret must contain at least 16 bytes")

    def issue(
        self,
        *,
        project_id: str,
        candidate_id: str,
        prompt: str,
        max_cost_usd: float | None,
        expires_in_seconds: float = 300.0,
    ) -> str:
        now = int(time.time())
        claims = {
            "schema_version": 1,
            "nonce": uuid.uuid4().hex,
            "project_id": str(project_id),
            "candidate_id": str(candidate_id),
            "prompt_sha256": _prompt_digest(prompt),
            "max_cost_usd": max_cost_usd,
            "issued_at": now,
            "expires_at": now + max(1, int(expires_in_seconds)),
        }
        body = _b64encode(_canonical_json(claims))
        signature = _b64encode(hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def verify(
        self,
        token: str,
        *,
        project_id: str,
        candidate_id: str,
        prompt: str,
        max_cost_usd: float | None,
    ) -> dict[str, Any]:
        try:
            body, supplied_signature = str(token).split(".", 1)
            expected_signature = _b64encode(
                hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise PermissionError("resource approval signature is invalid")
            claims = json.loads(_b64decode(body).decode("utf-8"))
        except PermissionError:
            raise
        except Exception as exc:
            raise PermissionError("resource approval token is malformed") from exc
        if int(claims.get("expires_at", 0) or 0) <= int(time.time()):
            raise PermissionError("resource approval token has expired")
        expected = {
            "project_id": str(project_id),
            "candidate_id": str(candidate_id),
            "prompt_sha256": _prompt_digest(prompt),
        }
        for key, value in expected.items():
            if not hmac.compare_digest(str(claims.get(key, "")), value):
                raise PermissionError(f"resource approval {key} does not match the request")
        approved_cost = claims.get("max_cost_usd")
        if max_cost_usd is None:
            if approved_cost is not None:
                raise PermissionError("unbounded request exceeds the approval cost ceiling")
        elif approved_cost is not None and float(max_cost_usd) > float(approved_cost):
            raise PermissionError("request cost ceiling exceeds the signed approval")
        return dict(claims)


class ApprovedResourcePipeline:
    def __init__(
        self,
        broker: UnifiedCapabilityBroker,
        resource_bridge: Any,
        *,
        artifact_root: Path,
        approval_issuer: ResourceApprovalTokenIssuer | None = None,
        quality_executor: QualityExecutor | None = None,
        prompt_threshold: float = 70.0,
        pass_threshold: float = 85.0,
        review_threshold: float = 70.0,
    ) -> None:
        self.broker = broker
        self.resource_bridge = resource_bridge
        self.artifact_root = Path(artifact_root)
        self.approval_issuer = approval_issuer
        self.quality_executor = quality_executor
        self.prompt_threshold = float(prompt_threshold)
        self.pass_threshold = float(pass_threshold)
        self.review_threshold = float(review_threshold)

    async def run(
        self,
        request: ResourcePipelineRequest | Mapping[str, Any],
    ) -> ResourcePipelineResult:
        parsed = request if isinstance(request, ResourcePipelineRequest) else ResourcePipelineRequest.from_dict(request)
        parsed.validate()
        stages: dict[str, Any] = {}
        blockers: list[str] = []

        planner_request = CapabilityRequest(
            request_id=f"{parsed.request_id}:planner",
            capability_kind=CapabilityKind.RESOURCE,
            task_type="script_to_prompt",
            project_id=parsed.project_id,
            run_id=parsed.run_id,
            prompt=parsed.prompt,
            candidate_id=parsed.planner_candidate_id,
            local_first=True,
            allow_live=False,
            metadata={"workload": "structured_output", "tags": ["korean", "json"]},
        )
        planner_route = await self.broker.plan(planner_request)
        stages["prompt_planner"] = {
            "candidate_id": parsed.planner_candidate_id,
            "allowed": planner_route.allowed,
            "mode": planner_route.mode,
            "provider": planner_route.provider,
            "model": planner_route.model,
            "workload": "structured_output",
            "tags": ["korean", "json"],
            "sandboxed_tools": False,
            "gpu_free_vram_mib": 0,
            "blockers": list(planner_route.blockers),
        }
        if not planner_route.allowed:
            blockers.extend(f"planner: {item}" for item in planner_route.blockers)

        try:
            prompt_quality = await _maybe_await(
                self.resource_bridge.evaluate_prompt(
                    candidate_id=parsed.candidate_id,
                    prompt=parsed.prompt,
                    params=parsed.params,
                    media=parsed.media,
                    task_type=parsed.task_type,
                    metric_threshold=self.prompt_threshold,
                    overall_threshold=self.prompt_threshold,
                )
            )
        except Exception as exc:
            prompt_quality = {
                "passed": False,
                "overall_score": 0.0,
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            }
            blockers.append("prompt quality evaluation failed closed")
        stages["prompt_quality"] = dict(prompt_quality)
        if not bool(prompt_quality.get("passed", False)):
            blockers.append(
                f"prompt quality {float(prompt_quality.get('overall_score', 0.0)):.2f} "
                f"did not pass {self.prompt_threshold:.2f}"
            )

        target_request = CapabilityRequest(
            request_id=f"{parsed.request_id}:generation",
            capability_kind=CapabilityKind.RESOURCE,
            task_type=parsed.task_type,
            project_id=parsed.project_id,
            run_id=parsed.run_id,
            prompt=parsed.prompt,
            candidate_id=parsed.candidate_id,
            parameters=dict(parsed.params),
            local_first=True,
            require_free=parsed.require_free,
            max_cost_usd=parsed.max_cost_usd,
            allow_live=False,
            gpu_free_vram_mib=parsed.gpu_free_vram_mib,
            hardware_profile=parsed.hardware_profile,
            metadata={
                "media": dict(parsed.media),
                "quality_preset": parsed.quality_preset,
            },
        )
        target_route = await self.broker.plan(target_request)
        stages["generation_plan"] = {
            "route_id": target_route.route_id,
            "allowed": target_route.allowed,
            "mode": target_route.mode,
            "provider": target_route.provider,
            "candidate_id": target_route.candidate_id,
            "model": target_route.model,
            "readiness": dict(target_route.readiness),
            "estimated_cost_usd": target_route.estimated_cost_usd,
            "hardware_readiness": dict(
                target_route.diagnostics.get("hardware_readiness", {}) or {}
            ),
            "blockers": list(target_route.blockers),
        }
        if not target_route.allowed:
            blockers.extend(f"generation: {item}" for item in target_route.blockers)

        if parsed.qa_candidate_id:
            qa_route = await self.broker.plan(
                CapabilityRequest(
                    request_id=f"{parsed.request_id}:qa-plan",
                    capability_kind=CapabilityKind.RESOURCE,
                    task_type="vision_analysis",
                    project_id=parsed.project_id,
                    run_id=parsed.run_id,
                    prompt="Evaluate the generated artifact with grounded findings and a 0-100 score.",
                    candidate_id=parsed.qa_candidate_id,
                    allow_live=False,
                    local_first=True,
                    require_free=True,
                    max_cost_usd=0.0,
                    gpu_free_vram_mib=parsed.gpu_free_vram_mib,
                    hardware_profile=parsed.hardware_profile,
                )
            )
            stages["quality_plan"] = {
                "candidate_id": parsed.qa_candidate_id,
                "allowed": qa_route.allowed,
                "mode": qa_route.mode,
                "provider": qa_route.provider,
                "model": qa_route.model,
                "simulation_only": bool(
                    qa_route.diagnostics.get("selected", {}).get("simulation_only", False)
                ),
                "hardware_readiness": dict(
                    qa_route.diagnostics.get("hardware_readiness", {}) or {}
                ),
                "blockers": list(qa_route.blockers),
            }

        if blockers:
            return await self._finish(parsed, "blocked", "regenerate", stages, blockers)
        if not parsed.allow_live:
            stages["execution"] = {"performed": False, "reason": "dry_run"}
            return await self._finish(parsed, "planned", "dry_run", stages, blockers)
        if not parsed.confirm_live:
            blockers.append("confirm_live=true is required")
            return await self._finish(parsed, "blocked", "blocked", stages, blockers)
        if self.approval_issuer is None or not parsed.approval_token:
            blockers.append("a configured signed approval token is required for live generation")
            return await self._finish(parsed, "blocked", "blocked", stages, blockers)

        try:
            claims = self.approval_issuer.verify(
                parsed.approval_token,
                project_id=parsed.project_id,
                candidate_id=parsed.candidate_id,
                prompt=parsed.prompt,
                max_cost_usd=parsed.max_cost_usd,
            )
            await self.broker.repository.consume_resource_approval(
                token_digest=hashlib.sha256(parsed.approval_token.encode("utf-8")).hexdigest(),
                project_id=parsed.project_id,
                candidate_id=parsed.candidate_id,
                request_id=parsed.request_id,
                claims=claims,
            )
        except PermissionError as exc:
            blockers.append(str(exc))
            return await self._finish(parsed, "blocked", "blocked", stages, blockers)
        target_request.allow_live = True
        target_request.metadata["confirm_live"] = True

        async def generate(_route: Any, _request: Any) -> Any:
            return await _maybe_await(
                self.resource_bridge.generate(
                    candidate_id=parsed.candidate_id,
                    prompt=parsed.prompt,
                    params=parsed.params,
                    media=parsed.media,
                    task_type=parsed.task_type,
                    quality_preset=parsed.quality_preset or None,
                    confirm_live=True,
                )
            )

        try:
            _route, generated = await self.broker.execute(target_request, generate)
        except Exception as exc:
            blockers.append(f"generation execution failed: {type(exc).__name__}: {exc}")
            stages["execution"] = {"performed": True, "success": False}
            return await self._finish(parsed, "blocked", "regenerate", stages, blockers)
        generated_map = dict(generated) if isinstance(generated, Mapping) else {"result": str(generated)}
        stages["execution"] = {
            "performed": True,
            **{
                key: generated_map.get(key)
                for key in (
                    "candidate_id",
                    "provider",
                    "model",
                    "status",
                    "asset_uri",
                    "artifact_id",
                    "latency_ms",
                    "cost",
                    "cost_unit",
                )
            },
        }
        if self.quality_executor is None:
            stages["artifact_quality"] = {
                "performed": False,
                "decision": "review",
                "reason": "no grounded VLM quality executor was configured",
            }
            return await self._finish(parsed, "review", "review", stages, blockers)

        try:
            quality = await _maybe_await(self.quality_executor(parsed.metadata, generated_map))
        except Exception as exc:
            stages["artifact_quality"] = {
                "performed": False,
                "decision": "review",
                "reason": f"quality evaluator failed: {type(exc).__name__}: {exc}"[:1000],
            }
            return await self._finish(parsed, "review", "review", stages, blockers)
        quality_map = dict(quality) if isinstance(quality, Mapping) else {}
        try:
            score = float(quality_map.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if not math.isfinite(score) or not 0.0 <= score <= 100.0:
            score = 0.0
        grounded = bool(quality_map.get("grounded", False))
        evidence = [str(item) for item in quality_map.get("evidence", []) or []]
        if score >= self.pass_threshold and grounded and evidence:
            decision = "pass"
            status = "completed"
        elif score >= self.review_threshold:
            decision = "review"
            status = "review"
        else:
            decision = "regenerate"
            status = "blocked"
        stages["artifact_quality"] = {
            **quality_map,
            "score": score,
            "grounded": grounded,
            "evidence": evidence,
            "pass_threshold": self.pass_threshold,
            "review_threshold": self.review_threshold,
            "decision": decision,
        }
        return await self._finish(parsed, status, decision, stages, blockers)

    async def _finish(
        self,
        request: ResourcePipelineRequest,
        status: str,
        decision: str,
        stages: dict[str, Any],
        blockers: list[str],
    ) -> ResourcePipelineResult:
        result = ResourcePipelineResult(
            request_id=request.request_id,
            project_id=request.project_id,
            candidate_id=request.candidate_id,
            status=status,
            decision=decision,
            stages=stages,
            blockers=list(dict.fromkeys(blockers)),
        )
        directory = self.artifact_root / _safe_segment(request.project_id) / _safe_segment(request.request_id)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / "resource-pipeline.json"
        result.artifact_manifest = str(destination)
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(destination)
        return result


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _prompt_digest(prompt: str) -> str:
    return hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()


def _safe_segment(value: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in str(value))
    return normalized.strip("-.")[:120] or "default"
