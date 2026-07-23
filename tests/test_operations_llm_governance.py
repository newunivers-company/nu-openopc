from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from opc.core.config import LLMConfig, OperationsConfig
from opc.database.store import OPCStore
from opc.llm.provider import LLMProvider
from opc.operations.service import OperationsService


def _response(*, content: str = "ok", prompt_tokens: int = 3, completion_tokens: int = 2):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=[]),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


class _AsyncChunks:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        self._iterator = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _stream_chunks():
    return _AsyncChunks(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="hello", tool_calls=[]),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, tool_calls=[]),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=4, completion_tokens=3),
            ),
        ]
    )


class GovernedLLMProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.provider = LLMProvider(
            LLMConfig(
                default_model="openai/test-model",
                api_base="https://llm.example/v1",
                api_key="test-key",
            )
        )
        readiness = self.provider.default_transport_readiness()
        self.operations = OperationsService(
            self.store,
            OperationsConfig(),
            default_llm_model=self.provider.config.default_model,
            default_llm_api_base=self.provider.config.api_base,
            default_llm_credential_ready=readiness["credential_ready"],
            default_llm_transport_ready=readiness["transport_ready"],
        )
        self.provider.bind_operations_service(self.operations, project_id="default")

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def test_public_chat_persists_contract_and_redacts_prompt(self) -> None:
        with patch(
            "opc.llm.provider.litellm.acompletion",
            AsyncMock(return_value=_response()),
        ), patch(
            "opc.llm.provider.litellm.completion_cost",
            return_value=0.01,
        ):
            with self.provider.operations_call_context(
                run_id="run-1",
                task_id="task-1",
                caller="test",
            ):
                result = await self.provider.chat(
                    [{"role": "user", "content": "secret prompt body"}]
                )

        contracts = await self.operations.repository.list_route_execution_contracts(
            project_id="default"
        )
        usage = await self.operations.repository.list_provider_usage_events(
            project_id="default"
        )
        self.assertEqual(result["content"], "ok")
        self.assertEqual(len(contracts), 1)
        self.assertEqual(contracts[0].status, "completed")
        self.assertEqual(contracts[0].actual_provider, "openopc_config")
        self.assertEqual(contracts[0].run_id, "run-1")
        self.assertEqual(contracts[0].request_snapshot["prompt"], "")
        self.assertNotIn("secret prompt body", str(contracts[0].request_snapshot))
        self.assertEqual(usage[0].total_tokens, 5)
        self.assertTrue(usage[0].measured)

    async def test_stream_persists_measured_usage_and_terminal_state(self) -> None:
        with patch(
            "opc.llm.provider.litellm.acompletion",
            AsyncMock(return_value=_stream_chunks()),
        ), patch(
            "opc.llm.provider.litellm.cost_per_token",
            return_value=(0.002, 0.003),
        ):
            events = [
                event
                async for event in self.provider.chat_stream(
                    [{"role": "user", "content": "stream"}]
                )
            ]

        contracts = await self.operations.repository.list_route_execution_contracts(
            project_id="default"
        )
        usage = await self.operations.repository.list_provider_usage_events(
            project_id="default"
        )
        self.assertEqual(contracts[0].status, "completed")
        self.assertEqual(contracts[0].actual_provider, "openopc_config")
        self.assertEqual(usage[0].total_tokens, 7)
        self.assertEqual(usage[0].cost_usd, 0.005)
        self.assertEqual(
            [event.event_type for event in events],
            ["message_start", "assistant_delta", "usage", "message_stop"],
        )

    async def test_closing_stream_marks_contract_failed(self) -> None:
        with patch(
            "opc.llm.provider.litellm.acompletion",
            AsyncMock(return_value=_stream_chunks()),
        ):
            stream = self.provider.chat_stream(
                [{"role": "user", "content": "cancel"}]
            )
            first = await anext(stream)
            self.assertEqual(first.event_type, "message_start")
            await stream.aclose()

        contracts = await self.operations.repository.list_route_execution_contracts(
            project_id="default"
        )
        self.assertEqual(contracts[0].status, "failed")
        self.assertIn("GeneratorExit", contracts[0].error)

    async def test_route_contract_bypasses_governance_recursion(self) -> None:
        with patch(
            "opc.llm.provider.litellm.acompletion",
            AsyncMock(return_value=_response()),
        ):
            await self.provider.chat(
                [{"role": "user", "content": "internal"}],
                route_contract={
                    "provider": "openopc_config",
                    "model": "openai/test-model",
                },
            )

        contracts = await self.operations.repository.list_route_execution_contracts(
            project_id="default"
        )
        self.assertEqual(contracts, [])


if __name__ == "__main__":
    unittest.main()
