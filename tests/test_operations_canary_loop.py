from __future__ import annotations

import unittest
from typing import Any

from opc.operations.canary import run_status_canary_loop
from opc.operations.models import CapabilityKind, CapabilityRequest, ProviderCanaryResult


class _FakeCanaryService:
    def __init__(self, successes: list[bool]) -> None:
        self.successes = successes
        self.calls = 0

    async def status_canary(
        self, request: CapabilityRequest, *, expected_model: str = ""
    ) -> ProviderCanaryResult:
        success = self.successes[self.calls % len(self.successes)]
        self.calls += 1
        return ProviderCanaryResult(
            project_id=request.project_id,
            capability_kind=CapabilityKind.LLM,
            provider="codex",
            model=expected_model or "status-model",
            success=success,
            available=success,
            credential_ready=success,
            transport_ready=success,
            latency_ms=12.5,
        )


class StatusCanaryLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_samples_persist_and_report_failures(self) -> None:
        service = _FakeCanaryService(successes=[True, False, True])
        sleeps: list[float] = []
        seen: list[dict[str, Any]] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        report = await run_status_canary_loop(
            service,
            CapabilityRequest(capability_kind=CapabilityKind.LLM, task_type="dialogue"),
            interval_seconds=300.0,
            iterations=3,
            sleep=fake_sleep,
            on_result=seen.append,
        )

        self.assertEqual(service.calls, 3)
        self.assertEqual(report["samples"], 3)
        self.assertEqual(report["failures"], 1)
        self.assertEqual(sleeps, [300.0, 300.0])
        self.assertEqual(len(seen), 3)
        self.assertEqual(report["last"]["provider"], "codex")

    async def test_single_iteration_never_sleeps(self) -> None:
        service = _FakeCanaryService(successes=[True])
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        report = await run_status_canary_loop(
            service,
            CapabilityRequest(capability_kind=CapabilityKind.LLM, task_type="dialogue"),
            iterations=1,
            sleep=fake_sleep,
        )
        self.assertEqual(report["samples"], 1)
        self.assertEqual(sleeps, [])

    async def test_invalid_parameters_fail_closed(self) -> None:
        service = _FakeCanaryService(successes=[True])
        request = CapabilityRequest(
            capability_kind=CapabilityKind.LLM, task_type="dialogue"
        )
        with self.assertRaises(ValueError):
            await run_status_canary_loop(service, request, interval_seconds=0)
        with self.assertRaises(ValueError):
            await run_status_canary_loop(service, request, iterations=-1)


if __name__ == "__main__":
    unittest.main()
