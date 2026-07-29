"""LLM provider layer built on LiteLLM for unified model access."""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Mapping
from urllib.parse import urlparse

from opc.core.windows_ssl import sanitize_windows_sslkeylogfile

sanitize_windows_sslkeylogfile()

import litellm
from loguru import logger

from opc.core.attachment_content import attachment_suffix
from opc.core.attachment_store import AttachmentRef
from opc.core.config import LLMConfig
from opc.core.models import ModelCapabilitySet, RuntimeLLMEvent
from opc.integrations.nu_llm_routing import NULlmRoutingBridge, RoutedLLMTarget

litellm.suppress_debug_info = True
litellm.drop_params = True

_MULTIMODAL_MODEL_HINTS = (
    "gpt-4.1",
    "gpt-4o",
    "gpt-4.5",
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "claude-3",
    "claude-sonnet-4",
    "claude-opus-4",
    "gemini",
    "pixtral",
    "llava",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "internvl",
    "minicpm-v",
    "glm-4v",
)

_DOCUMENT_MODEL_HINTS = (
    "gpt-4.1",
    "gpt-4o",
    "gpt-4.5",
    "gpt-5",
    "claude-3",
    "claude-sonnet-4",
    "claude-opus-4",
    "gemini",
)

_VIDEO_MODEL_HINTS = (
    "gemini",
    "veo",
    "video",
)

_TOOL_PROTOCOL_ERROR_HINTS = (
    "no tool output found for function call",
    "no tool output found",
    "messages with role 'tool' must be a response to a preceding message with 'tool_calls'",
    "assistant message with tool_calls",
    "tool_calls must be followed by tool messages",
    "missing tool response",
    "missing tool output",
    "tool_call_id",
)


def _normalized_model_name(model: str) -> str:
    if "/" in model:
        return model.split("/", 1)[1].strip().lower()
    return model.strip().lower()


# Used when neither user config nor litellm can supply a window. Conservative
# enough for modern models so compaction still has a real denominator.
_CONTEXT_WINDOW_FALLBACK = 128_000
_context_window_fallback_warned: set[str] = set()
_max_tokens_clamp_warned: set[str] = set()


def _clamp_max_tokens(model: str, requested: int) -> int:
    """Cap the requested output tokens at the model's known output limit.

    Providers disagree on how to handle an oversized max_tokens: some clamp
    silently, others (e.g. DeepSeek) reject the request outright. Clamping
    here keeps a generous config default (32768) safe on small-cap models.
    Unknown models pass through unchanged.
    """
    try:
        info = litellm.get_model_info(model)
        cap = info.get("max_output_tokens") or info.get("max_tokens")
    except Exception:
        return requested
    if not cap or requested <= int(cap):
        return requested
    if model not in _max_tokens_clamp_warned:
        _max_tokens_clamp_warned.add(model)
        logger.info(
            "max_tokens {} exceeds output limit {} of model={}; clamping.",
            requested,
            cap,
            model,
        )
    return int(cap)


_CONTEXT_WINDOW_OVERRIDES: tuple[tuple[str, int], ...] = (
    ("gpt-5.4-pro", 1_050_000),
    ("gpt-5.4-mini", 400_000),
    ("gpt-5.4-nano", 400_000),
    ("gpt-5.4", 1_050_000),
    ("gpt-5-pro", 400_000),
    ("gpt-5", 400_000),
)


_POE_CONTEXT_WINDOW_OVERRIDES: tuple[tuple[str, int], ...] = (
    ("claude-sonnet-4.5", 64_000),
    ("claude-sonnet-4-5", 64_000),
)


def _context_window_override(model: str) -> int | None:
    normalized = _normalized_model_name(model)
    for prefix, window in _CONTEXT_WINDOW_OVERRIDES:
        if normalized == prefix or normalized.startswith(f"{prefix}-"):
            return window
    return None


def _poe_context_window_override(model: str) -> int | None:
    normalized = _normalized_model_name(model)
    for prefix, window in _POE_CONTEXT_WINDOW_OVERRIDES:
        if normalized == prefix or normalized.startswith(f"{prefix}-"):
            return window
    return None


def _is_official_openai_base(api_base: str | None) -> bool:
    normalized = str(api_base or "").strip()
    if not normalized:
        return True
    try:
        parsed = urlparse(normalized)
    except Exception:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    return hostname in {"api.openai.com", "openai.com"}


def _is_poe_base(api_base: str | None) -> bool:
    normalized = str(api_base or "").strip()
    if not normalized:
        return False
    try:
        parsed = urlparse(normalized)
    except Exception:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    return hostname == "api.poe.com"


def _looks_like_multimodal_model(model: str) -> bool:
    normalized = _normalized_model_name(model)
    if any(hint in normalized for hint in _MULTIMODAL_MODEL_HINTS):
        return True
    return normalized.endswith("-vl") or "-vl-" in normalized or "_vl" in normalized


def _looks_like_document_capable_model(model: str) -> bool:
    normalized = _normalized_model_name(model)
    return any(hint in normalized for hint in _DOCUMENT_MODEL_HINTS)


def _looks_like_video_capable_model(model: str) -> bool:
    normalized = _normalized_model_name(model)
    return any(hint in normalized for hint in _VIDEO_MODEL_HINTS)


def _parse_tool_arguments(tool_name: str, arguments: Any) -> tuple[Any, str | None, str | None]:
    """Parse tool-call arguments and preserve failures for downstream recovery."""
    if not isinstance(arguments, str):
        return arguments, None, None

    raw = arguments
    try:
        parsed = json.loads(raw)
        return parsed, raw, None
    except json.JSONDecodeError as e:
        snippet = raw[:500].replace("\n", "\\n")
        error = f"Invalid tool arguments JSON for `{tool_name}`: {e.msg} at char {e.pos}"
        logger.warning(f"{error}. Raw snippet: {snippet}")
        return raw, raw, error


class LLMProvider:
    """Unified LLM interface via LiteLLM supporting tool calls."""

    def __init__(self, config: LLMConfig, opc_home: Path | None = None) -> None:
        self.config = config
        self.opc_home = opc_home
        self._total_tokens_in = 0
        self._total_tokens_out = 0
        self._total_cost = 0.0
        self._measured_calls = 0
        self._unmeasured_calls = 0
        self._calls_by_provider: dict[str, int] = {}

        self._api_key = config.api_key or (
            os.environ.get(config.api_key_env) if config.api_key_env else None
        ) or None
        self._api_base = config.api_base or None
        self.nu_router = NULlmRoutingBridge(config.nu_routing, opc_home=opc_home)
        self._last_route_target: RoutedLLMTarget | None = None
        self._operations_service: Any | None = None
        self._operations_project_id = "default"
        self._operations_context: ContextVar[dict[str, Any]] = ContextVar(
            f"openopc_llm_operations_context_{id(self)}",
            default={},
        )

    def bind_operations_service(self, service: Any | None, *, project_id: str = "default") -> None:
        """Route future public calls through the persisted operations contract."""

        self._operations_service = service
        self._operations_project_id = str(project_id or "default")

    def inherit_operations_binding(self, source: "LLMProvider") -> None:
        """Copy durable governance binding into a model-override child provider."""

        self.bind_operations_service(
            getattr(source, "_operations_service", None),
            project_id=getattr(source, "_operations_project_id", "default"),
        )

    @contextmanager
    def operations_call_context(
        self,
        context: Mapping[str, Any] | None = None,
        **values: Any,
    ) -> Iterator[None]:
        """Attach task-local execution identity without leaking across coroutines."""

        merged = {
            **dict(self._operations_context.get()),
            **dict(context or {}),
            **values,
        }
        token = self._operations_context.set(merged)
        try:
            yield
        finally:
            self._operations_context.reset(token)

    def _resolved_operations_context(
        self,
        explicit: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "project_id": self._operations_project_id,
            **dict(self._operations_context.get()),
            **dict(explicit or {}),
        }

    def has_credentials(self) -> bool:
        """Whether an LLM call can plausibly authenticate.

        True when an explicit key, an endpoint/model-matching provider env var,
        a keyless local endpoint, or an authenticated NU subscription target is
        available. Callers use False to skip work that would certainly fail;
        the runtime can still degrade to rule-based behavior.
        """
        return bool(
            self.config.transport_readiness()["credential_ready"]
            or self.nu_router.has_usable_target()
        )

    def default_transport_readiness(self) -> dict[str, bool]:
        """Describe the configured LiteLLM fallback without making a model call."""
        return self.config.transport_readiness()

    @property
    def stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "tokens_in": self._total_tokens_in,
            "tokens_out": self._total_tokens_out,
            "estimated_cost": self._total_cost,
            "measured_calls": self._measured_calls,
            "unmeasured_calls": self._unmeasured_calls,
            "calls_by_provider": dict(sorted(self._calls_by_provider.items())),
        }
        if self._last_route_target is not None:
            stats["nu_route_target"] = self._last_route_target.safe_dict()
        return stats

    def _configured_target(self, model: str) -> RoutedLLMTarget:
        readiness = self.default_transport_readiness()
        return RoutedLLMTarget(
            provider="openopc_config",
            model=model,
            api_base=self._api_base or "",
            api_key=self._api_key,
            credential_configured=readiness["credential_ready"],
            transport_ready=readiness["transport_ready"],
        )

    def _candidate_targets(
        self,
        task_type: str | None = None,
        *,
        has_tools: bool = False,
    ) -> list[RoutedLLMTarget]:
        if task_type and task_type in self.config.routing:
            return [self._configured_target(self.config.routing[task_type])]
        routed = list(self.nu_router.targets(task_type=task_type, has_tools=has_tools))
        if routed:
            return routed
        return [self._configured_target(self.config.default_model)]

    def _targets_for_execution_contract(
        self,
        targets: list[RoutedLLMTarget],
        contract: Mapping[str, Any] | Any | None,
    ) -> list[RoutedLLMTarget]:
        if contract is None:
            return targets
        payload = (
            dict(contract)
            if isinstance(contract, Mapping)
            else dict(contract.to_dict())
            if callable(getattr(contract, "to_dict", None))
            else {}
        )
        order = [
            dict(item)
            for item in payload.get("fallback_order", []) or []
            if isinstance(item, Mapping)
        ]
        if not order:
            primary = {
                "provider": payload.get("planned_provider", payload.get("provider", "")),
                "model": payload.get("planned_model", payload.get("model", "")),
            }
            order = [primary]
            order.extend(
                dict(item)
                for item in payload.get("alternatives", []) or []
                if isinstance(item, Mapping)
            )
        selected: list[RoutedLLMTarget] = []
        for planned in order:
            provider = str(planned.get("provider", "") or "")
            model = str(planned.get("model", "") or "")
            match = next(
                (
                    target
                    for target in targets
                    if target not in selected
                    and target.provider == provider
                    and (not model or target.model == model)
                ),
                None,
            )
            if match is not None:
                selected.append(match)
        if not selected:
            raise RuntimeError("no currently available LLM target satisfies the execution contract")
        return selected

    def _select_target(
        self,
        task_type: str | None = None,
        *,
        has_tools: bool = False,
    ) -> RoutedLLMTarget:
        target = self._candidate_targets(task_type, has_tools=has_tools)[0]
        self._last_route_target = target
        return target

    def _select_model(self, task_type: str | None = None) -> str:
        return self._select_target(task_type).model

    def _apply_target_transport(
        self,
        call_kwargs: dict[str, Any],
        target: RoutedLLMTarget,
    ) -> None:
        if target.transport_kind in {"subscription_cli", "nu_native"}:
            raise ValueError("native NU targets must execute through the NU router bridge")
        api_base = target.api_base or self._api_base
        if api_base:
            call_kwargs["api_base"] = api_base
        if target.provider == "openopc_config":
            if target.api_key:
                call_kwargs["api_key"] = target.api_key
        else:
            # A routed endpoint owns its authentication boundary.  In
            # particular, never forward OpenOPC's default provider key to a
            # keyless localhost target selected by the shared router.
            call_kwargs.pop("api_key", None)
            if target.api_key:
                call_kwargs["api_key"] = target.api_key
        if target.extra_body:
            merged_extra_body = dict(target.extra_body)
            existing = call_kwargs.get("extra_body")
            if isinstance(existing, dict):
                merged_extra_body.update(existing)
            call_kwargs["extra_body"] = merged_extra_body
        for parameter in target.unsupported_params:
            call_kwargs.pop(parameter, None)

    def _config_context_window_override(self, model: str) -> int | None:
        """User-configured context window for models litellm cannot map.

        Per-model overrides win over the scalar default. Both come from
        ``LLMConfig`` so proxy/self-hosted models (doubao, minimax, glm, …)
        can report a real context window to the usage ring and compaction.
        """
        per_model = getattr(self.config, "context_window_overrides", None) or {}
        normalized = _normalized_model_name(model)
        for key, window in per_model.items():
            candidate = _normalized_model_name(str(key))
            if candidate and (normalized == candidate or normalized.startswith(f"{candidate}-")):
                try:
                    value = int(window)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    return value
        try:
            scalar = int(getattr(self.config, "context_window", 0) or 0)
        except (TypeError, ValueError):
            scalar = 0
        return scalar if scalar > 0 else None

    def get_context_window(self, task_type: str | None = None, model: str | None = None) -> int | None:
        # Stream fallbacks pass the model resolved by the successful candidate.
        # Preserve that candidate's transport metadata instead of selecting the
        # first route again (which would also corrupt the reported last target).
        target = (
            self._last_route_target
            if model is not None and task_type is None and self._last_route_target is not None
            else self._select_target(task_type)
        )
        resolved_model = model or target.model
        selected_api_base = target.api_base or self._api_base
        config_override = self._config_context_window_override(resolved_model)
        if config_override is not None:
            return config_override
        poe_override = _poe_context_window_override(resolved_model) if _is_poe_base(selected_api_base) else None
        if poe_override is not None:
            return poe_override
        override = _context_window_override(resolved_model) if _is_official_openai_base(selected_api_base) else None
        if override is not None:
            return override
        try:
            # max_input_tokens is the context window; litellm.get_max_tokens()
            # returns the "max_tokens" map entry, which for many models (e.g.
            # deepseek) is the OUTPUT cap and would wildly under-report here.
            info = litellm.get_model_info(resolved_model)
            limit = info.get("max_input_tokens") or info.get("max_tokens")
            if limit:
                return int(limit)
            reason = "model is not mapped in litellm"
        except Exception as e:
            reason = str(e)
        # Subscription CLIs can legitimately return a provider-local alias
        # (for example ``opus``) when their JSON payload omits the canonical
        # model revision. LiteLLM cannot resolve those aliases, and there is no
        # authoritative context-window value in the route contract. Keep the
        # conservative denominator without presenting this expected transport
        # limitation as a user configuration error.
        if target.transport_kind == "subscription_cli":
            logger.debug(
                "Using conservative context window for subscription alias model={} "
                "from provider={}: {}",
                resolved_model,
                target.provider,
                reason,
            )
            return _CONTEXT_WINDOW_FALLBACK
        if resolved_model not in _context_window_fallback_warned:
            _context_window_fallback_warned.add(resolved_model)
            logger.warning(
                "Unable to resolve context window for model={} ({}); assuming {} tokens. "
                "Set llm.context_window or llm.context_window_overrides in llm_config.yaml "
                "to use the model's real window.",
                resolved_model,
                reason,
                _CONTEXT_WINDOW_FALLBACK,
            )
        return _CONTEXT_WINDOW_FALLBACK

    def count_input_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
        model: str | None = None,
    ) -> int | None:
        resolved_model = model or self._select_target(task_type, has_tools=bool(tools)).model
        try:
            return int(litellm.token_counter(
                model=resolved_model,
                messages=messages,
                tools=tools,
            ))
        except Exception as e:
            logger.warning(f"Unable to count prompt tokens for model={resolved_model}: {e}")
            return None

    def count_text_tokens(
        self,
        text: str,
        task_type: str | None = None,
        model: str | None = None,
    ) -> int | None:
        resolved_model = model or self._select_model(task_type)
        try:
            return int(litellm.token_counter(
                model=resolved_model,
                text=text,
            ))
        except Exception as e:
            logger.warning(f"Unable to count text tokens for model={resolved_model}: {e}")
            return None

    def get_capabilities(
        self,
        task_type: str | None = None,
        model: str | None = None,
    ) -> ModelCapabilitySet:
        target = self._select_target(task_type)
        resolved_model = model or target.model
        normalized = _normalized_model_name(resolved_model)
        provider_family = resolved_model.split("/", 1)[0].strip().lower() if "/" in resolved_model else ""
        supports_thinking = any(hint in normalized for hint in ("o1", "o3", "o4", "gpt-5", "claude", "reason"))
        return ModelCapabilitySet(
            model=resolved_model,
            supports_streaming=target.supports_streaming,
            supports_tool_calling=target.supports_tools,
            supports_streaming_tool_calls=target.supports_streaming and target.supports_tools,
            supports_thinking=supports_thinking,
            supports_multimodal=_looks_like_multimodal_model(resolved_model),
            supports_documents=_looks_like_document_capable_model(resolved_model),
            supports_video=_looks_like_video_capable_model(resolved_model),
            provider_family=provider_family,
            metadata={
                "api_base": target.api_base or self._api_base or "",
                "transport_kind": target.transport_kind,
                "provider": target.provider,
            },
        )

    def build_cache_fingerprint(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
        model: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        resolved_model = model or self._select_model(task_type)
        payload = {
            "model": resolved_model,
            "messages": messages,
            "tools": tools or [],
            "extra": extra or {},
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_context_overflow_error(self, error: Exception) -> bool:
        if isinstance(error, litellm.exceptions.ContextWindowExceededError):
            return True
        message = str(error).lower()
        keywords = (
            "context window",
            "context length",
            "maximum context length",
            "prompt is too long",
            "too many tokens",
            "context_length_exceeded",
            "token limit exceeded",
        )
        return any(keyword in message for keyword in keywords)

    def is_tool_protocol_error(self, error: Exception) -> bool:
        message = str(error).lower()
        return any(hint in message for hint in _TOOL_PROTOCOL_ERROR_HINTS)

    @staticmethod
    def sanitize_tool_call_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop incomplete or stray assistant/tool tool-call transcripts."""
        sanitized: list[dict[str, Any]] = []
        pending_ids: list[str] = []
        buffered_block: list[dict[str, Any]] = []

        def _copy_message(message: dict[str, Any]) -> dict[str, Any]:
            cloned = dict(message)
            if isinstance(message.get("tool_calls"), list):
                cloned["tool_calls"] = [dict(item) for item in message["tool_calls"]]
            return cloned

        for message in messages:
            role = str(message.get("role", "") or "").strip()
            if not pending_ids:
                if role == "assistant" and isinstance(message.get("tool_calls"), list) and message["tool_calls"]:
                    ids = [
                        str(item.get("id", "") or "").strip()
                        for item in message["tool_calls"]
                        if isinstance(item, dict) and str(item.get("id", "") or "").strip()
                    ]
                    if not ids:
                        continue
                    buffered_block = [_copy_message(message)]
                    pending_ids = ids
                    continue
                if role == "tool":
                    continue
                sanitized.append(_copy_message(message))
                continue

            if role == "tool":
                tool_call_id = str(message.get("tool_call_id", "") or "").strip()
                if tool_call_id and tool_call_id in pending_ids:
                    buffered_block.append(_copy_message(message))
                    pending_ids = [item for item in pending_ids if item != tool_call_id]
                    if not pending_ids:
                        sanitized.extend(buffered_block)
                        buffered_block = []
                continue

            if role == "assistant" and isinstance(message.get("tool_calls"), list) and message["tool_calls"]:
                ids = [
                    str(item.get("id", "") or "").strip()
                    for item in message["tool_calls"]
                    if isinstance(item, dict) and str(item.get("id", "") or "").strip()
                ]
                buffered_block = [_copy_message(message)] if ids else []
                pending_ids = ids
                continue

            buffered_block = []
            pending_ids = []
            sanitized.append(_copy_message(message))

        return sanitized

    def prepare_user_message_content(
        self,
        content: str,
        *,
        attachment_refs: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
    ) -> str | list[dict[str, Any]]:
        text = str(content or "")
        refs = list(attachment_refs or [])
        if not refs:
            return text

        model = self._select_model(task_type)
        parts = self._build_direct_attachment_parts(model, refs)
        if not parts:
            return text

        content_parts: list[dict[str, Any]] = []
        if text:
            content_parts.append({"type": "text", "text": text})
        content_parts.extend(parts)
        return content_parts

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        route_contract: Mapping[str, Any] | Any | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        operations_context = kwargs.pop("operations_context", None)
        if route_contract is None and self._operations_service is not None:
            context = self._resolved_operations_context(
                operations_context if isinstance(operations_context, Mapping) else None
            )
            request = self._operations_service.create_llm_request(
                messages=messages,
                task_type=task_type,
                tools=tools,
                context=context,
            )
            governed_kwargs = dict(kwargs)
            if tools is not None:
                governed_kwargs["tools"] = tools
            if temperature is not None:
                governed_kwargs["temperature"] = temperature
            if max_tokens is not None:
                governed_kwargs["max_tokens"] = max_tokens
            _route, result = await self._operations_service.execute_llm(
                request,
                self,
                messages,
                **governed_kwargs,
            )
            return result
        targets = self._targets_for_execution_contract(
            self._candidate_targets(task_type, has_tools=bool(tools)),
            route_contract,
        )
        temp = temperature if temperature is not None else self.config.temperature
        requested_max = max_tokens if max_tokens is not None else self.config.max_tokens
        timeout_seconds = float(kwargs.pop("timeout", kwargs.pop("timeout_seconds", 120.0)) or 120.0)
        errors: list[tuple[str, Exception]] = []
        for index, target in enumerate(targets):
            self._last_route_target = target
            model = target.model
            max_tok = _clamp_max_tokens(model, requested_max)
            logger.debug(
                "LLM call: model={}, provider={}, transport={}, base={}, msgs={}, tools={}",
                model,
                target.provider,
                target.transport_kind,
                target.api_base or self._api_base or "default",
                len(messages),
                len(tools or []),
            )
            try:
                if target.transport_kind in {"subscription_cli", "nu_native"}:
                    if tools:
                        raise ValueError("native NU LLM routes do not support tool calls")
                    metadata = {
                        key: value
                        for key, value in kwargs.items()
                        if key
                        in {
                            "reasoning_effort",
                            "codex_reasoning_effort",
                            "grok_reasoning_effort",
                            "web_search",
                        }
                    }
                    response = await asyncio.to_thread(
                        self.nu_router.execute_text_target,
                        target,
                        messages=messages,
                        temperature=temp,
                        max_tokens=max_tok,
                        timeout_seconds=timeout_seconds,
                        metadata=metadata,
                    )
                    return self._normalize_subscription_response(response, target)

                call_kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temp,
                    "max_tokens": max_tok,
                    "timeout": timeout_seconds,
                    **kwargs,
                }
                self._apply_target_transport(call_kwargs, target)
                if tools:
                    call_kwargs["tools"] = tools
                    call_kwargs["tool_choice"] = "auto"
                response = await litellm.acompletion(**call_kwargs)
                break
            except Exception as exc:
                errors.append((target.provider, exc))
                if index + 1 < len(targets):
                    logger.warning(
                        "LLM route {} failed ({}); trying {}",
                        target.provider,
                        type(exc).__name__,
                        targets[index + 1].provider,
                    )
                    continue
                detail = "; ".join(
                    f"{provider}: {type(error).__name__}: {error}"
                    for provider, error in errors
                )
                logger.error("LLM call failed across {} route(s): {}", len(errors), detail)
                raise exc
        else:  # pragma: no cover - targets always contains at least the configured fallback
            raise RuntimeError("no LLM targets available")

        usage = getattr(response, "usage", None)
        cost = 0.0
        accounted_cost: float | None = None
        if usage:
            self._total_tokens_in += getattr(usage, "prompt_tokens", 0)
            self._total_tokens_out += getattr(usage, "completion_tokens", 0)
            try:
                cost = litellm.completion_cost(completion_response=response)
                accounted_cost = max(0.0, float(cost))
                self._total_cost += cost
            except Exception:
                pass
        measured = usage is not None
        self._record_call_accounting(target.provider, measured=measured)

        choice = response.choices[0]
        message = choice.message

        result: dict[str, Any] = {
            "content": message.content or "",
            "tool_calls": [],
            "finish_reason": choice.finish_reason,
            "model": model,
            "provider": target.provider,
            "cost": cost,
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
            },
            "usage_accounting": {
                "measured": measured,
                "source": "provider_reported" if measured else "unknown",
                "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                "total_tokens": (
                    int(getattr(usage, "prompt_tokens", 0) or 0)
                    + int(getattr(usage, "completion_tokens", 0) or 0)
                    if usage
                    else None
                ),
                "cost_usd": accounted_cost,
                "subscription_quota": {},
            },
        }

        if message.tool_calls:
            for tc in message.tool_calls:
                args, raw_args, parse_error = _parse_tool_arguments(tc.function.name, tc.function.arguments)
                tool_call = {
                    "id": tc.id,
                    "function": tc.function.name,
                    "arguments": args,
                }
                if raw_args is not None:
                    tool_call["arguments_raw"] = raw_args
                if parse_error:
                    tool_call["arguments_parse_error"] = parse_error
                result["tool_calls"].append(tool_call)

        return result

    def _normalize_subscription_response(
        self,
        response: Any,
        target: RoutedLLMTarget,
    ) -> dict[str, Any]:
        usage = dict(getattr(response, "usage", {}) or {})
        measured = any(
            key in usage
            for key in ("input_tokens", "output_tokens", "total_tokens", "total_cost_usd")
        )
        prompt_tokens = int(usage.get("input_tokens", 0) or 0)
        completion_tokens = int(usage.get("output_tokens", 0) or 0)
        cost = float(usage.get("total_cost_usd", 0.0) or 0.0)
        self._total_tokens_in += prompt_tokens
        self._total_tokens_out += completion_tokens
        self._total_cost += cost
        self._record_call_accounting(target.provider, measured=measured)
        quota = usage.get("subscription_quota")
        return {
            "content": str(getattr(response, "content", "") or ""),
            "tool_calls": [],
            "finish_reason": "stop",
            "model": str(getattr(response, "model", "") or target.model),
            "provider": str(getattr(response, "provider", "") or target.provider),
            "cost": cost,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            "usage_accounting": {
                "measured": measured,
                "source": (
                    "provider_reported"
                    if measured
                    else "subscription_cli_unreported"
                ),
                "input_tokens": prompt_tokens if measured else None,
                "output_tokens": completion_tokens if measured else None,
                "total_tokens": prompt_tokens + completion_tokens if measured else None,
                "cost_usd": cost if "total_cost_usd" in usage else None,
                "subscription_quota": dict(quota) if isinstance(quota, dict) else {},
            },
        }

    def _record_call_accounting(self, provider: str, *, measured: bool) -> None:
        normalized = str(provider or "unknown")
        self._calls_by_provider[normalized] = self._calls_by_provider.get(normalized, 0) + 1
        if measured:
            self._measured_calls += 1
        else:
            self._unmeasured_calls += 1

    def normalize_stream_event(
        self,
        chunk: Any,
        *,
        model: str,
    ) -> list[RuntimeLLMEvent]:
        events: list[RuntimeLLMEvent] = []
        usage = getattr(chunk, "usage", None)
        if usage:
            events.append(RuntimeLLMEvent(
                event_type="usage",
                model=model,
                payload={
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(usage, "completion_tokens", 0),
                    "context_window": self.get_context_window(model=model),
                },
            ))

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return events

        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is not None:
            text = getattr(delta, "content", None)
            if text:
                events.append(RuntimeLLMEvent(
                    event_type="assistant_delta",
                    model=model,
                    payload={"text": text},
                ))
            thinking_text = (
                getattr(delta, "reasoning", None)
                or getattr(delta, "reasoning_content", None)
                or getattr(delta, "thinking", None)
            )
            if thinking_text:
                events.append(RuntimeLLMEvent(
                    event_type="thinking_delta",
                    model=model,
                    payload={"text": str(thinking_text)},
                ))
            tool_calls = getattr(delta, "tool_calls", None) or []
            for tool_call in tool_calls:
                function = getattr(tool_call, "function", None)
                events.append(RuntimeLLMEvent(
                    event_type="tool_call_delta",
                    model=model,
                    payload={
                        "index": getattr(tool_call, "index", 0),
                        "id": getattr(tool_call, "id", ""),
                        "name": getattr(function, "name", "") if function else "",
                        "arguments": getattr(function, "arguments", "") if function else "",
                    },
                ))

        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason:
            events.append(RuntimeLLMEvent(
                event_type="message_stop",
                model=model,
                payload={"finish_reason": finish_reason},
            ))
        return events

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        route_contract: Mapping[str, Any] | Any | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[RuntimeLLMEvent]:
        operations_context = kwargs.pop("operations_context", None)
        if route_contract is None and self._operations_service is not None:
            context = self._resolved_operations_context(
                operations_context if isinstance(operations_context, Mapping) else None
            )
            request = self._operations_service.create_llm_request(
                messages=messages,
                task_type=task_type,
                tools=tools,
                context=context,
            )
            governed_kwargs = dict(kwargs)
            if tools is not None:
                governed_kwargs["tools"] = tools
            if temperature is not None:
                governed_kwargs["temperature"] = temperature
            if max_tokens is not None:
                governed_kwargs["max_tokens"] = max_tokens
            governed_stream = self._operations_service.execute_llm_stream(
                request,
                self,
                messages,
                **governed_kwargs,
            )
            try:
                async for event in governed_stream:
                    yield event
            finally:
                await governed_stream.aclose()
            return
        targets = self._targets_for_execution_contract(
            self._candidate_targets(task_type, has_tools=bool(tools)),
            route_contract,
        )
        target = targets[0]
        self._last_route_target = target
        if target.transport_kind in {"subscription_cli", "nu_native"}:
            try:
                result = await self.chat(
                    messages,
                    tools=tools,
                    task_type=task_type,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    route_contract=route_contract,
                    **kwargs,
                )
            except Exception as exc:
                yield RuntimeLLMEvent(
                    event_type="error",
                    model=target.model,
                    payload={"message": str(exc)},
                )
                raise
            resolved_model = str(result.get("model") or target.model)
            yield RuntimeLLMEvent(
                event_type="message_start",
                model=resolved_model,
                payload={"model": resolved_model, "provider": str(result.get("provider") or target.provider)},
            )
            content = str(result.get("content") or "")
            if content:
                yield RuntimeLLMEvent(
                    event_type="assistant_delta",
                    model=resolved_model,
                    payload={"text": content},
                )
            usage = dict(result.get("usage", {}) or {})
            yield RuntimeLLMEvent(
                event_type="usage",
                model=resolved_model,
                payload={
                    "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                    "estimated_cost_delta": float(result.get("cost", 0.0) or 0.0),
                    "estimated_cost_total": self._total_cost,
                    "context_window": self.get_context_window(model=resolved_model),
                    "model": resolved_model,
                    "provider": str(result.get("provider") or target.provider),
                    "usage_accounting": dict(result.get("usage_accounting", {}) or {}),
                },
            )
            yield RuntimeLLMEvent(
                event_type="message_stop",
                model=resolved_model,
                payload={"finish_reason": str(result.get("finish_reason") or "stop")},
            )
            return
        model = target.model
        temp = temperature if temperature is not None else self.config.temperature
        max_tok = _clamp_max_tokens(model, max_tokens if max_tokens is not None else self.config.max_tokens)

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tok,
            "stream": True,
            **kwargs,
        }
        self._apply_target_transport(call_kwargs, target)
        if tools:
            call_kwargs["tools"] = tools
            call_kwargs["tool_choice"] = "auto"

        logger.debug(
            "LLM stream call: model={}, provider={}, base={}, msgs={}, tools={}",
            model,
            target.provider,
            target.api_base or self._api_base or "default",
            len(messages),
            len(tools or []),
        )

        last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        usage_observed = False
        call_cost_total = 0.0
        call_cost_known = True
        yield RuntimeLLMEvent(
            event_type="message_start",
            model=model,
            payload={"model": model, "provider": target.provider},
        )

        try:
            stream = await litellm.acompletion(**call_kwargs)
            if hasattr(stream, "__aiter__"):
                async for chunk in stream:
                    for event in self.normalize_stream_event(chunk, model=model):
                        if event.event_type == "usage":
                            usage_observed = True
                            total_prompt = int(event.payload.get("prompt_tokens", 0) or 0)
                            total_completion = int(event.payload.get("completion_tokens", 0) or 0)
                            delta_prompt = max(0, total_prompt - last_usage["prompt_tokens"])
                            delta_completion = max(0, total_completion - last_usage["completion_tokens"])
                            last_usage["prompt_tokens"] = total_prompt
                            last_usage["completion_tokens"] = total_completion
                            cost: float | None = None
                            try:
                                prompt_cost, completion_cost = litellm.cost_per_token(
                                    model=model,
                                    prompt_tokens=delta_prompt,
                                    completion_tokens=delta_completion,
                                )
                                cost = float(prompt_cost or 0.0) + float(completion_cost or 0.0)
                            except Exception:
                                cost = None
                            self._total_tokens_in += delta_prompt
                            self._total_tokens_out += delta_completion
                            if cost is not None:
                                self._total_cost += cost
                                call_cost_total += cost
                            else:
                                call_cost_known = False
                            event.payload = {
                                **dict(event.payload),
                                "prompt_tokens": delta_prompt,
                                "completion_tokens": delta_completion,
                                "prompt_tokens_total": total_prompt,
                                "completion_tokens_total": total_completion,
                                "estimated_cost_delta": cost,
                                "estimated_cost_total": self._total_cost,
                                "context_window": event.payload.get("context_window") or self.get_context_window(model=model),
                                "model": model,
                                "provider": target.provider,
                                "usage_accounting": {
                                    "measured": True,
                                    "source": "provider_reported",
                                    "input_tokens": total_prompt,
                                    "output_tokens": total_completion,
                                    "total_tokens": total_prompt + total_completion,
                                    "cost_usd": call_cost_total if call_cost_known else None,
                                    "subscription_quota": {},
                                },
                            }
                        yield event
            else:
                # Provider fallback: treat the response as a single non-streaming completion.
                choice = stream.choices[0]
                message = choice.message
                if getattr(message, "content", None):
                    yield RuntimeLLMEvent(
                        event_type="assistant_delta",
                        model=model,
                        payload={"text": message.content},
                    )
                for tc in getattr(message, "tool_calls", None) or []:
                    yield RuntimeLLMEvent(
                        event_type="tool_call_delta",
                        model=model,
                        payload={
                            "index": 0,
                            "id": getattr(tc, "id", ""),
                            "name": getattr(getattr(tc, "function", None), "name", ""),
                            "arguments": getattr(getattr(tc, "function", None), "arguments", ""),
                        },
                    )
                usage = getattr(stream, "usage", None)
                if usage:
                    usage_observed = True
                    cost: float | None = None
                    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                    try:
                        prompt_cost, completion_cost = litellm.cost_per_token(
                            model=model,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                        )
                        cost = float(prompt_cost or 0.0) + float(completion_cost or 0.0)
                    except Exception:
                        cost = None
                    self._total_tokens_in += prompt_tokens
                    self._total_tokens_out += completion_tokens
                    if cost is not None:
                        self._total_cost += cost
                    yield RuntimeLLMEvent(
                        event_type="usage",
                        model=model,
                        payload={
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "prompt_tokens_total": prompt_tokens,
                            "completion_tokens_total": completion_tokens,
                            "estimated_cost_delta": cost,
                            "estimated_cost_total": self._total_cost,
                            "context_window": self.get_context_window(model=model),
                            "model": model,
                            "provider": target.provider,
                            "usage_accounting": {
                                "measured": True,
                                "source": "provider_reported",
                                "input_tokens": prompt_tokens,
                                "output_tokens": completion_tokens,
                                "total_tokens": prompt_tokens + completion_tokens,
                                "cost_usd": cost,
                                "subscription_quota": {},
                            },
                        },
                    )
                yield RuntimeLLMEvent(
                    event_type="message_stop",
                    model=model,
                    payload={"finish_reason": getattr(choice, "finish_reason", "stop")},
                )
            self._record_call_accounting(target.provider, measured=usage_observed)
        except Exception as e:
            self._record_call_accounting(target.provider, measured=False)
            logger.error(f"LLM stream failed: {e}")
            yield RuntimeLLMEvent(
                event_type="error",
                model=model,
                payload={"message": str(e)},
            )
            raise

    async def simple_chat(
        self,
        prompt: str,
        system: str | None = None,
        task_type: str | None = None,
        *,
        operations_context: Mapping[str, Any] | None = None,
    ) -> str:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        result = await self.chat(
            messages,
            task_type=task_type,
            operations_context=operations_context,
        )
        return result["content"]

    def get_tool_definitions(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert internal tool definitions to OpenAI function-calling format."""
        formatted = []
        for tool in tools:
            formatted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                },
            })
        return formatted

    def _build_direct_attachment_parts(
        self,
        model: str,
        attachment_refs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        capabilities = self._attachment_capabilities(model)
        if not capabilities["enabled"]:
            return []

        parts: list[dict[str, Any]] = []
        for ref_dict in attachment_refs:
            try:
                ref = AttachmentRef.from_dict(ref_dict)
                data_url = self._attachment_data_url(ref)
            except Exception as exc:
                logger.warning(f"Skipping direct attachment payload: {exc}")
                continue

            if not data_url:
                continue

            if ref.mime_type.startswith("image/") and capabilities["image_mode"]:
                parts.append(self._build_attachment_part("image_url", ref, data_url))
            elif ref.mime_type == "application/pdf" and capabilities["pdf_mode"]:
                parts.append(self._build_attachment_part(str(capabilities["pdf_mode"]), ref, data_url))
            elif ref.mime_type.startswith("video/") and capabilities["video_mode"]:
                parts.append(self._build_attachment_part(str(capabilities["video_mode"]), ref, data_url))

        return parts

    def _build_attachment_part(
        self,
        mode: str,
        ref: AttachmentRef,
        data_url: str,
    ) -> dict[str, Any]:
        if mode == "image_url":
            return {
                "type": "image_url",
                "image_url": {"url": data_url},
            }
        if mode == "video_url":
            return {
                "type": "video_url",
                "video_url": {"url": data_url},
            }
        if mode == "file":
            return {
                "type": "file",
                "file": {
                    "file_data": data_url,
                    "filename": ref.filename,
                },
            }
        raise ValueError(f"Unsupported attachment transport mode: {mode}")

    def _attachment_capabilities(self, model: str) -> dict[str, Any]:
        provider = model.split("/", 1)[0].strip().lower()
        api_base = (self._api_base or "").lower()

        image_mode: str | None = None
        pdf_mode: str | None = None
        video_mode: str | None = None

        if "api.poe.com" in api_base:
            image_mode = "image_url"
            pdf_mode = "file"
            if _looks_like_multimodal_model(model):
                video_mode = "file"
        elif "openrouter.ai" in api_base:
            image_mode = "image_url"
            pdf_mode = "file"
            if _looks_like_video_capable_model(model):
                video_mode = "video_url"
        elif provider in {"openai", "azure", "anthropic"}:
            image_mode = "image_url"
            pdf_mode = "file"
        elif provider in {"google", "gemini", "vertex_ai", "vertex"}:
            image_mode = "image_url"
            pdf_mode = "file"
            if _looks_like_video_capable_model(model):
                video_mode = "video_url"
        elif _looks_like_multimodal_model(model):
            image_mode = "image_url"
            pdf_mode = "file" if _looks_like_document_capable_model(model) else None
            if _looks_like_video_capable_model(model):
                video_mode = "video_url"

        return {
            "enabled": bool(image_mode or pdf_mode or video_mode),
            "image_mode": image_mode,
            "pdf_mode": pdf_mode,
            "video_mode": video_mode,
        }

    def _attachment_data_url(self, ref: AttachmentRef) -> str | None:
        path = self._resolve_attachment_path(ref)
        if path is None:
            return None
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        mime_type = ref.mime_type or _guess_mime_from_filename(ref.filename)
        return f"data:{mime_type};base64,{payload}"

    def _resolve_attachment_path(self, ref: AttachmentRef) -> Path | None:
        if not self.opc_home or not ref.disk_path:
            return None
        resolved = (self.opc_home / ref.disk_path).resolve()
        opc_home_resolved = self.opc_home.resolve()
        if not str(resolved).startswith(str(opc_home_resolved)):
            raise ValueError(f"Attachment path escapes OPC home: {ref.disk_path}")
        return resolved


def _guess_mime_from_filename(filename: str) -> str:
    suffix = attachment_suffix(filename)
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix == ".pptx":
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if suffix == ".mp4":
        return "video/mp4"
    if suffix in {".mpeg", ".mpg"}:
        return "video/mpeg"
    if suffix == ".mov":
        return "video/quicktime"
    if suffix == ".webm":
        return "video/webm"
    return "application/octet-stream"
