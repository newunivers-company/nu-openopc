from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from opc.core.config import NUResourceGenConfig
from opc.integrations.nu_resource_gen import NUResourceGenBridge
from opc.layer4_tools.nu_resource_gen import create_nu_resource_gen_tools


class NUResourceGenBridgeTests(unittest.TestCase):
    def _bridge(self, root: Path, **overrides: object) -> NUResourceGenBridge:
        config = NUResourceGenConfig(
            enabled=True,
            record_ledger=False,
            archive_live_artifacts=False,
            **overrides,
        )
        return NUResourceGenBridge(config, opc_home=root, project_id="demo")

    def test_catalog_and_policy_plan_use_stable_library_without_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            bridge = self._bridge(Path(raw_root))
            catalog = bridge.list_candidates(candidate_id="gemini_image_flash")
            plan = bridge.plan(
                candidate_id="gemini_image_flash",
                prompt="Clean 9:16 webtoon panel without text",
                params={"aspect_ratio": "9:16"},
                task_type="image_generation",
                task=SimpleNamespace(id="task/1", project_id="demo project"),
            )

        self.assertEqual(catalog["returned"], 1)
        self.assertEqual(catalog["candidates"][0]["provider"], "gemini")
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["workload_class"], "image_generation")
        self.assertIn("policy", plan)
        self.assertEqual(
            plan["request"]["scope_path"],
            "openopc/project:demo-project/task:task-1",
        )
        self.assertFalse(plan["live_guard"]["allowed"])

    def test_live_generation_is_blocked_by_every_local_guard(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            bridge = self._bridge(Path(raw_root))
            with self.assertRaisesRegex(PermissionError, "allow_live"):
                bridge.generate(
                    candidate_id="gemini_image_flash",
                    prompt="test",
                    confirm_live=True,
                )

    def test_local_provider_health_signal_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            bridge = self._bridge(Path(raw_root))
            bridge.health = lambda **_kwargs: {  # type: ignore[method-assign]
                "providers": {
                    "local_audio": {"has_credentials": False},
                    "comfyui": {"has_credentials": True},
                }
            }

            audio = bridge.provider_readiness("local_audio")
            comfy = bridge.provider_readiness("comfyui")

        self.assertFalse(audio["credential_ready"])
        self.assertFalse(audio["transport_ready"])
        self.assertTrue(comfy["credential_ready"])
        self.assertTrue(comfy["transport_ready"])

    def test_string_provider_health_is_normalized_without_mapping_errors(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            bridge = self._bridge(Path(raw_root))
            bridge.health = lambda **_kwargs: {  # type: ignore[method-assign]
                "providers": {
                    "local_vlm": "available: local runtime ready",
                    "gemini": "unavailable: API key missing",
                }
            }

            local = bridge.provider_readiness("local_vlm")
            remote = bridge.provider_readiness("gemini")

        self.assertTrue(local["transport_ready"])
        self.assertFalse(remote["transport_ready"])

    def test_rfdetr_readiness_uses_trusted_runtime_override_not_catalog_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            runtime = root / "rfdetr" / "bin" / "python"
            runtime.parent.mkdir(parents=True)
            runtime.write_text("#!/bin/sh\n", encoding="utf-8")
            bridge = self._bridge(root)
            bridge.provider_readiness = lambda _provider: {  # type: ignore[method-assign]
                "credential_ready": True,
                "transport_ready": True,
            }
            with patch.dict(os.environ, {"NU_RFDETR_PYTHON": str(runtime)}):
                readiness = bridge.candidate_readiness("local_rfdetr_detection_nano")

        self.assertTrue(readiness["transport_ready"])
        self.assertEqual(readiness["path_checks"]["python"]["path"], str(runtime))

    def test_explicit_live_allowlist_and_confirmation_reach_generator(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            bridge = self._bridge(
                Path(raw_root),
                allow_live=True,
                allowed_live_candidates=["fake-safe"],
            )

            class FakeGenerator:
                def generate(self, candidate_id: str, request: object) -> object:
                    self.candidate_id = candidate_id
                    self.request = request
                    return SimpleNamespace(
                        candidate_id=candidate_id,
                        provider="fake",
                        model="fake-model",
                        status="completed",
                        output_text="done",
                        asset_uri="/tmp/fake.png",
                        job_id="job-1",
                        latency_ms=12.0,
                        cost=0.0,
                        cost_unit="usd",
                        usage={},
                        request_id="request-1",
                        routing_decision_id="route-1",
                        attempt_id="attempt-1",
                        artifact_id="artifact-1",
                        pipeline_run_id="pipeline-1",
                        policy_version="test/v1",
                    )

            fake = FakeGenerator()
            bridge._generator = fake
            bridge._load_attempted = True
            result = bridge.generate(
                candidate_id="fake-safe",
                prompt="test",
                confirm_live=True,
                timeout=10_000,
                max_retries=99,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(fake.request.timeout, 900)
        self.assertEqual(fake.request.max_retries, 5)
        self.assertFalse(fake.request.retry_submit)

    def test_tool_contract_marks_only_live_generation_as_confirmation_required(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            bridge = self._bridge(Path(raw_root))
            tools = {tool.name: tool for tool in create_nu_resource_gen_tools(bridge)}

        self.assertEqual(
            set(tools),
            {
                "nu_resource_candidates",
                "nu_resource_health",
                "nu_resource_plan",
                "nu_resource_generate",
            },
        )
        self.assertTrue(tools["nu_resource_generate"].requires_confirmation)
        self.assertFalse(tools["nu_resource_generate"].read_only)
        self.assertFalse(tools["nu_resource_candidates"].requires_confirmation)

    def test_local_artifact_quality_requires_expected_grounded_scope(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            artifact = root / "frame.png"
            artifact.write_bytes(b"test-image")
            bridge = self._bridge(
                root,
                local_quality_candidates=["local-grounding"],
            )

            class FakeGenerator:
                execution_policy = SimpleNamespace()

                def get_candidate(self, candidate_id: str) -> object:
                    return SimpleNamespace(
                        candidate_id=candidate_id,
                        provider="local_vlm",
                        model="grounding-v1",
                        cost=0.0,
                        cost_unit="local",
                        extras={},
                    )

                def generate(self, candidate_id: str, request: object) -> object:
                    self.request = request
                    return SimpleNamespace(
                        candidate_id=candidate_id,
                        provider="local_vlm",
                        model="grounding-v1",
                        status="completed",
                        output_text=json.dumps(
                            {
                                "score": 93,
                                "summary": "person grounded",
                                "findings": [
                                    {
                                        "class_name": "person",
                                        "confidence": 0.91,
                                        "bbox": [1, 2, 30, 40],
                                    }
                                ],
                                "detected_classes": ["person"],
                                "missing_expected_classes": [],
                            }
                        ),
                        asset_uri=str(artifact),
                        job_id="quality-1",
                        latency_ms=10.0,
                        cost=0.0,
                        cost_unit="local",
                        usage={"count": 1},
                    )

            fake = FakeGenerator()
            bridge._generator = fake
            bridge._load_attempted = True
            result = bridge.evaluate_artifact_quality(
                {
                    "request_id": "request-1",
                    "qa_candidate_id": "local-grounding",
                    "quality_scope": "object_presence",
                    "expected_classes": ["person"],
                    "qa_params": {"python": "/tmp/untrusted", "threshold": 0.4},
                },
                {"asset_uri": str(artifact)},
            )

        self.assertTrue(result["scope_satisfied"])
        self.assertTrue(result["grounded"])
        self.assertEqual(result["evidence"], ["bbox:person"])
        self.assertNotIn("python", fake.request.params)
        self.assertEqual(fake.request.params["threshold"], 0.4)


if __name__ == "__main__":
    unittest.main()
