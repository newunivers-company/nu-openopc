from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from opc.core.config import LLMConfig, NULlmRoutingConfig
from opc.integrations.nu_llm_routing import NULlmRoutingBridge, RoutedLLMTarget
from opc.llm.provider import LLMProvider


class _FakeOpenAICompatibleProvider:
    def __init__(self) -> None:
        self.base_url = "https://router.example/v1"
        self.model = "ep-test"
        self.api_key_envs = ["NU_TEST_ROUTER_KEY"]
        self.extra_body = {"thinking": {"type": "disabled"}}
        self.unsupported_params = frozenset({"temperature"})


_FakeOpenAICompatibleProvider.__module__ = "nu_llm_routing_lib.providers.openai_compatible"


class _FakeRouter:
    def __init__(self) -> None:
        self.providers = {
            "routed": _FakeOpenAICompatibleProvider(),
            "unsupported": object(),
        }

    def route_order_for(self, _request: object) -> list[str]:
        return ["unsupported", "routed"]

    def route_diagnostics(self, request: object, *, include_status: bool = False) -> dict:
        return {
            "metadata": dict(getattr(request, "metadata", {})),
            "matched_profile_index": 2,
            "matched_profile_id": "quick-json",
            "initial_order": ["unsupported", "routed"],
            "benchmark_order": ["routed", "unsupported"],
            "final_order": ["routed"],
            "provider_exclusions": [{"provider": "unsupported", "reason": "policy"}],
            "provider_checks": {"routed": {"available": True}} if include_status else None,
            "benchmark_ranking": {
                "evaluated": True,
                "applied": True,
                "reason": "test evidence",
                "categories": ["structured_output"],
                "source_manifest": [{"large": "payload"}],
            },
        }


def _bridge(*, apply_to_tool_calls: bool = False) -> NULlmRoutingBridge:
    bridge = NULlmRoutingBridge(
        NULlmRoutingConfig(
            enabled=True,
            apply_to_tool_calls=apply_to_tool_calls,
            gpu_free_vram_mib=0,
        ),
        opc_home=Path("/tmp/openopc-test"),
    )
    bridge._router = _FakeRouter()
    bridge._load_attempted = True
    bridge._config_path = Path("/tmp/router.json")
    return bridge


class NULlmRoutingBridgeTests(unittest.TestCase):
    def test_remote_target_requires_configured_credential(self) -> None:
        bridge = _bridge()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bridge.targets(task_type="quick_tasks", has_tools=False), ())

    def test_target_maps_router_provider_to_litellm_transport(self) -> None:
        bridge = _bridge()
        with patch.dict(os.environ, {"NU_TEST_ROUTER_KEY": "secret"}, clear=True):
            target = bridge.targets(task_type="quick_tasks", has_tools=False)[0]

        self.assertEqual(target.provider, "routed")
        self.assertEqual(target.model, "openai/ep-test")
        self.assertEqual(target.api_base, "https://router.example/v1")
        self.assertEqual(target.extra_body, {"thinking": {"type": "disabled"}})
        self.assertEqual(target.unsupported_params, frozenset({"temperature"}))
        self.assertNotIn("secret", repr(target))

    def test_tool_calls_remain_on_openopc_transport_by_default(self) -> None:
        bridge = _bridge(apply_to_tool_calls=False)
        with patch.dict(os.environ, {"NU_TEST_ROUTER_KEY": "secret"}, clear=True):
            self.assertEqual(bridge.targets(task_type=None, has_tools=True), ())

    def test_diagnostics_are_compact_and_deterministic(self) -> None:
        bridge = _bridge()
        result = bridge.diagnostics(
            workload="structured_output",
            tags=["json", "korean"],
            gpu_free_vram_mib=0,
            include_status=True,
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["matched_profile_id"], "quick-json")
        self.assertEqual(result["final_order"], ["routed"])
        self.assertEqual(result["metadata"]["gpu_free_vram_mib"], 0)
        self.assertEqual(result["provider_checks"]["routed"]["available"], True)
        self.assertNotIn("source_manifest", result["benchmark_ranking"])


class NULlmProviderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_keyless_routed_target_does_not_inherit_default_api_key(self) -> None:
        provider = LLMProvider(LLMConfig(
            default_model="openai/default-model",
            api_base="https://default.example/v1",
            api_key="default-key",
        ))
        call_kwargs = {"api_key": "per-call-default-key"}

        provider._apply_target_transport(
            call_kwargs,
            RoutedLLMTarget(
                provider="local-router",
                model="openai/local-model",
                api_base="http://127.0.0.1:8000/v1",
                api_key=None,
            ),
        )

        self.assertEqual(call_kwargs["api_base"], "http://127.0.0.1:8000/v1")
        self.assertNotIn("api_key", call_kwargs)

    async def test_quick_task_uses_routed_transport_and_preserves_tool_fallback(self) -> None:
        config = LLMConfig(
            default_model="openai/default-model",
            api_base="https://default.example/v1",
            api_key="default-key",
            nu_routing=NULlmRoutingConfig(enabled=True),
        )
        provider = LLMProvider(config)
        provider.nu_router = _bridge()
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=[]),
                finish_reason="stop",
            )],
            usage=None,
        )

        with patch.dict(os.environ, {"NU_TEST_ROUTER_KEY": "route-key"}, clear=True), patch(
            "opc.llm.provider.litellm.acompletion",
            AsyncMock(return_value=response),
        ) as completion:
            result = await provider.chat(
                [{"role": "user", "content": "return json"}],
                task_type="quick_tasks",
            )
            routed_kwargs = completion.await_args.kwargs
            self.assertEqual(result["model"], "openai/ep-test")
            self.assertEqual(routed_kwargs["api_base"], "https://router.example/v1")
            self.assertEqual(routed_kwargs["api_key"], "route-key")
            self.assertNotIn("temperature", routed_kwargs)
            self.assertEqual(
                routed_kwargs["extra_body"],
                {"thinking": {"type": "disabled"}},
            )

            await provider.chat(
                [{"role": "user", "content": "use a tool"}],
                tools=[{"type": "function", "function": {"name": "demo"}}],
            )
            default_kwargs = completion.await_args.kwargs
            self.assertEqual(default_kwargs["model"], "openai/default-model")
            self.assertEqual(default_kwargs["api_base"], "https://default.example/v1")
            self.assertEqual(default_kwargs["api_key"], "default-key")


if __name__ == "__main__":
    unittest.main()
