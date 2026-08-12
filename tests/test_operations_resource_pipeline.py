from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from opc.database.store import OPCStore
from opc.operations.capabilities import UnifiedCapabilityBroker
from opc.operations.repository import OperationsRepository
from opc.operations.resource_pipeline import (
    ApprovedResourcePipeline,
    ResourceApprovalTokenIssuer,
    ResourcePipelineRequest,
)


class _ResourceBridge:
    enabled = True

    def __init__(self) -> None:
        self.generate_calls = 0
        self.candidates = {
            "llm-planner-primary": {
                "candidate_id": "llm-planner-primary",
                "provider": "llm_router",
                "model": "nu-llm-router",
                "category": "script_to_prompt",
                "cost": None,
                "cost_unit": "unknown",
                "credentials_configured": True,
                "deprecated": False,
                "simulation_only": False,
            },
            "local-image": {
                "candidate_id": "local-image",
                "provider": "comfyui",
                "model": "krea2",
                "category": "image_generation",
                "cost": 0.0,
                "cost_unit": "local",
                "credentials_configured": True,
                "deprecated": False,
                "simulation_only": False,
            },
            "local_vlm_grounded": {
                "candidate_id": "local_vlm_grounded",
                "provider": "local_vlm",
                "model": "grounded-vlm-v1",
                "category": "vision_analysis",
                "cost": 0.0,
                "cost_unit": "local",
                "credentials_configured": True,
                "deprecated": False,
                "simulation_only": False,
                "supported_task_types": ["grounding_evidence"],
            },
        }

    def list_candidates(self, *, candidate_id=None, category=None, limit=None):
        values = list(self.candidates.values())
        if candidate_id:
            values = [self.candidates[candidate_id]]
        if category:
            values = [item for item in values if item["category"] == category]
        if limit:
            values = values[:limit]
        return {"count": len(values), "candidates": values}

    def plan(self, **kwargs):
        return {
            "dry_run": True,
            "candidate": self.candidates[kwargs["candidate_id"]],
            "policy": {"allowed": True, "blocked": False},
        }

    def status(self):
        return {"allow_live": True, "allowed_live_candidates": ["local-image"]}

    def provider_readiness(self, _provider):
        return {"credential_ready": True, "transport_ready": True, "detail": "ready"}

    def candidate_readiness(self, candidate_id):
        return {
            "candidate_id": candidate_id,
            "credential_ready": True,
            "transport_ready": True,
            "detail": "ready",
        }

    def quality_execution_status(self, candidate_id):
        return {
            "allowed": candidate_id == "local_vlm_grounded",
            "blockers": (
                []
                if candidate_id == "local_vlm_grounded"
                else ["quality candidate is not allowlisted"]
            ),
        }

    def evaluate_prompt(self, **_kwargs):
        return {
            "passed": True,
            "overall_score": 96.0,
            "threshold": 70.0,
            "metrics": [{"name": "prompt_adherence", "score": 96.0, "passed": True}],
        }

    def generate(self, **_kwargs):
        self.generate_calls += 1
        return {
            "candidate_id": "local-image",
            "provider": "comfyui",
            "model": "krea2",
            "status": "completed",
            "asset_uri": "artifact://image.png",
            "artifact_id": "image-1",
            "latency_ms": 25.0,
            "cost": 0.0,
            "cost_unit": "usd",
            "usage_accounting": {
                "measured": True,
                "source": "provider_reported",
                "cost_usd": 0.0,
            },
        }


class ApprovedResourcePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = OPCStore(self.root / "tasks.db")
        await self.store.initialize()
        self.repository = OperationsRepository(self.store)
        self.bridge = _ResourceBridge()
        self.broker = UnifiedCapabilityBroker(self.repository, resource_bridge=self.bridge)
        self.issuer = ResourceApprovalTokenIssuer("test-secret-that-is-long-enough")
        self.pipeline = ApprovedResourcePipeline(
            self.broker,
            self.bridge,
            artifact_root=self.root / "artifacts",
            approval_issuer=self.issuer,
            quality_executor=lambda _metadata, _generated: {
                "score": 92.0,
                "grounded": True,
                "scope_satisfied": True,
                "evidence": ["artifact://vlm-report.json"],
                "findings": [],
            },
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    def _request(self, *, live: bool = False) -> ResourcePipelineRequest:
        return ResourcePipelineRequest(
            request_id="resource-pipeline-1",
            project_id="default",
            prompt=(
                "Vertical 9:16 cinematic webtoon panel, one protagonist at a rain-soaked "
                "bus stop, medium shot, blue hour rim light, exact title text 'NU SIGNAL'."
            ),
            candidate_id="local-image",
            task_type="image_generation",
            params={"aspect_ratio": "9:16"},
            allow_live=live,
            confirm_live=live,
            require_free=True,
            max_cost_usd=0.0,
            qa_candidate_id="local_vlm_grounded",
            quality_scope="object_presence",
            expected_classes=["person"],
        )

    async def test_dry_run_executes_planner_prompt_gate_and_quality_plan_only(self) -> None:
        result = await self.pipeline.run(self._request())

        self.assertEqual(result.status, "planned")
        self.assertEqual(result.decision, "dry_run")
        self.assertFalse(result.stages["execution"]["performed"])
        self.assertEqual(result.stages["prompt_planner"]["candidate_id"], "llm-planner-primary")
        self.assertEqual(result.stages["quality_plan"]["candidate_id"], "local_vlm_grounded")
        self.assertEqual(self.bridge.generate_calls, 0)
        self.assertTrue(Path(result.artifact_manifest).is_file())

    async def test_signed_live_approval_is_single_use_and_quality_gated(self) -> None:
        request = self._request(live=True)
        request.approval_token = self.issuer.issue(
            project_id=request.project_id,
            candidate_id=request.candidate_id,
            prompt=request.prompt,
            max_cost_usd=request.max_cost_usd,
            operator_id="test-operator",
        )

        first = await self.pipeline.run(request)
        second = await self.pipeline.run(request)

        self.assertEqual(first.status, "completed")
        self.assertEqual(first.decision, "pass")
        self.assertEqual(first.stages["artifact_quality"]["score"], 92.0)
        self.assertEqual(first.stages["approval"]["operator_id"], "test-operator")
        self.assertEqual(first.stages["approval"]["key_id"], "primary")
        self.assertTrue(first.stages["approval"]["approval_id"])
        self.assertEqual(self.bridge.generate_calls, 1)
        self.assertEqual(second.status, "blocked")
        self.assertIn("already been consumed", second.blockers[0])

    async def test_approval_is_bound_to_exact_prompt(self) -> None:
        request = self._request(live=True)
        request.approval_token = self.issuer.issue(
            project_id=request.project_id,
            candidate_id=request.candidate_id,
            prompt=request.prompt,
            max_cost_usd=request.max_cost_usd,
            operator_id="test-operator",
        )
        request.prompt += " Add another character."

        result = await self.pipeline.run(request)

        self.assertEqual(result.status, "blocked")
        self.assertTrue(any("prompt_sha256" in item for item in result.blockers))
        self.assertEqual(self.bridge.generate_calls, 0)

    async def test_prompt_evaluator_error_fails_closed_with_manifest(self) -> None:
        def fail_prompt(**_kwargs):
            raise RuntimeError("prompt evaluator unavailable")

        self.bridge.evaluate_prompt = fail_prompt  # type: ignore[method-assign]
        result = await self.pipeline.run(self._request())

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.decision, "regenerate")
        self.assertTrue(any("failed closed" in item for item in result.blockers))
        self.assertIn("prompt evaluator unavailable", result.stages["prompt_quality"]["error"])
        self.assertTrue(Path(result.artifact_manifest).is_file())

    async def test_quality_evaluator_error_requires_review_after_generation(self) -> None:
        request = self._request(live=True)
        request.approval_token = self.issuer.issue(
            project_id=request.project_id,
            candidate_id=request.candidate_id,
            prompt=request.prompt,
            max_cost_usd=request.max_cost_usd,
            operator_id="test-operator",
        )

        def fail_quality(_metadata, _generated):
            raise RuntimeError("VLM unavailable")

        self.pipeline.quality_executor = fail_quality
        result = await self.pipeline.run(request)

        self.assertEqual(result.status, "review")
        self.assertEqual(result.decision, "review")
        self.assertTrue(result.stages["execution"]["performed"])
        self.assertIn("VLM unavailable", result.stages["artifact_quality"]["reason"])
        self.assertTrue(Path(result.artifact_manifest).is_file())

    async def test_high_score_without_requested_scope_evidence_requires_review(self) -> None:
        request = self._request(live=True)
        request.approval_token = self.issuer.issue(
            project_id=request.project_id,
            candidate_id=request.candidate_id,
            prompt=request.prompt,
            max_cost_usd=request.max_cost_usd,
            operator_id="test-operator",
        )
        self.pipeline.quality_executor = lambda _metadata, _generated: {
            "score": 99.0,
            "grounded": True,
            "scope_satisfied": False,
            "evidence": ["bbox:unrelated-object"],
            "findings": [],
        }

        result = await self.pipeline.run(request)

        self.assertEqual(result.status, "review")
        self.assertEqual(result.decision, "review")
        self.assertFalse(result.stages["artifact_quality"]["scope_satisfied"])

    async def test_approval_key_rotation_verifies_retained_key_and_signs_with_new_key(self) -> None:
        old = ResourceApprovalTokenIssuer(
            "old-secret-that-is-long-enough",
            key_id="2026-q2",
        )
        old_token = old.issue(
            project_id="default",
            candidate_id="local-image",
            prompt="approved prompt",
            max_cost_usd=0.0,
            operator_id="operator-1",
        )
        rotated = ResourceApprovalTokenIssuer(
            "new-secret-that-is-long-enough",
            key_id="2026-q3",
            verification_keys={"2026-q2": "old-secret-that-is-long-enough"},
        )
        new_token = rotated.issue(
            project_id="default",
            candidate_id="local-image",
            prompt="approved prompt",
            max_cost_usd=0.0,
            operator_id="operator-2",
        )

        self.assertEqual(rotated.inspect(old_token)["key_id"], "2026-q2")
        self.assertEqual(rotated.inspect(new_token)["key_id"], "2026-q3")
        self.assertEqual(rotated.inspect(new_token)["operator_id"], "operator-2")

    async def test_approval_requires_operator_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "operator_id"):
            self.issuer.issue(
                project_id="default",
                candidate_id="local-image",
                prompt="approved prompt",
                max_cost_usd=0.0,
                operator_id="",
            )


if __name__ == "__main__":
    unittest.main()
