from __future__ import annotations

import asyncio
from datetime import timedelta
import tempfile
from pathlib import Path
import unittest

from opc.database.store import OPCStore
from opc.operations.capabilities import UnifiedCapabilityBroker
from opc.operations.models import CapabilityKind, CapabilityRequest, utc_now
from opc.operations.repository import OperationsRepository


class _SubscriptionTarget:
    provider = "codex"
    model = "gpt-subscription"

    def safe_dict(self):
        return {
            "provider": self.provider,
            "model": self.model,
            "api_base": "",
            "transport_kind": "subscription_cli",
            "credential_configured": True,
            "transport_ready": True,
            "supports_tools": False,
            "supports_streaming": False,
        }


class _SubscriptionRouter:
    enabled = True

    def diagnostics(self, **_kwargs):
        return {"available": True, "final_order": ["codex"]}

    def targets(self, **_kwargs):
        return (_SubscriptionTarget(),)


class SubscriptionCallQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.repository = OperationsRepository(self.store)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    def _broker(self, *, limit: int) -> UnifiedCapabilityBroker:
        return UnifiedCapabilityBroker(
            self.repository,
            llm_router=_SubscriptionRouter(),
            subscription_call_limit=limit,
            subscription_window_seconds=3600,
            subscription_providers=["codex"],
        )

    @staticmethod
    def _request() -> CapabilityRequest:
        return CapabilityRequest(
            capability_kind=CapabilityKind.LLM,
            task_type="dialogue",
            project_id="default",
            allow_live=True,
            local_first=False,
        )

    @staticmethod
    def _result() -> dict[str, object]:
        return {
            "provider": "codex",
            "candidate_id": "gpt-subscription",
            "model": "gpt-subscription",
            "usage_accounting": {
                "measured": False,
                "source": "subscription_cli_unreported",
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "cost_usd": None,
                "subscription_quota": {},
            },
        }

    async def test_unknown_subscription_usage_is_bounded_by_call_count(self) -> None:
        broker = self._broker(limit=2)
        executed = 0

        async def execute(_route, _request):
            nonlocal executed
            executed += 1
            return self._result()

        await broker.execute(self._request(), execute)
        await broker.execute(self._request(), execute)
        with self.assertRaisesRegex(PermissionError, "quota exhausted"):
            await broker.execute(self._request(), execute)

        status = await self.repository.provider_call_quota_status(
            project_id="default",
            provider="codex",
            limit=2,
            window_seconds=3600,
        )
        usage = await self.repository.list_provider_usage_events(
            project_id="default", provider="codex"
        )
        self.assertEqual(executed, 2)
        self.assertEqual(status["used"], 2)
        self.assertEqual(status["remaining"], 0)
        self.assertFalse(status["allowed"])
        self.assertEqual(len(usage), 2)
        self.assertTrue(all(not item.measured for item in usage))

    async def test_failed_provider_attempt_still_consumes_conservative_quota(self) -> None:
        broker = self._broker(limit=1)

        async def fail(_route, _request):
            raise RuntimeError("provider failed after dispatch")

        with self.assertRaisesRegex(RuntimeError, "after dispatch"):
            await broker.execute(self._request(), fail)
        with self.assertRaisesRegex(PermissionError, "quota exhausted"):
            await broker.execute(self._request(), lambda *_args: self._result())

        reservations = await self.repository.count_rows(
            "provider_call_reservations"
        )
        self.assertEqual(reservations, 1)

    async def test_concurrent_reservations_cannot_oversubscribe_limit(self) -> None:
        broker = self._broker(limit=1)
        entered = asyncio.Event()
        release = asyncio.Event()
        executions = 0

        async def execute(_route, _request):
            nonlocal executions
            executions += 1
            entered.set()
            await release.wait()
            return self._result()

        first = asyncio.create_task(broker.execute(self._request(), execute))
        await entered.wait()
        second = asyncio.create_task(broker.execute(self._request(), execute))
        await asyncio.sleep(0)
        release.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)

        self.assertEqual(executions, 1)
        self.assertEqual(sum(isinstance(item, PermissionError) for item in outcomes), 1)

    async def test_rolling_window_expires_old_reservations(self) -> None:
        now = utc_now()
        await self.repository.reserve_provider_call(
            contract_id="contract-old",
            request_id="request-old",
            project_id="default",
            provider="codex",
            model="gpt-subscription",
            limit=1,
            window_seconds=60,
            now=now,
        )

        status = await self.repository.provider_call_quota_status(
            project_id="default",
            provider="codex",
            limit=1,
            window_seconds=60,
            now=now + timedelta(seconds=61),
        )

        self.assertEqual(status["used"], 0)
        self.assertTrue(status["allowed"])


if __name__ == "__main__":
    unittest.main()
