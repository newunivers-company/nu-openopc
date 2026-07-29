"""Safe planning bridge from OpenOPC to ``nu-llm-routing-lib``.

The shared router owns provider policy and ordering. OpenOPC keeps transport
ownership because its native runtime requires OpenAI-compatible tool-call
envelopes that the shared router intentionally does not expose yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from loguru import logger

from opc.core.config import NULlmRoutingConfig


_ROUTER_CONFIG_ENV = "NU_LLM_ROUTER_CONFIG"


@dataclass(frozen=True)
class RoutedLLMTarget:
    provider: str
    model: str
    api_base: str = ""
    api_key: str | None = field(default=None, repr=False)
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    unsupported_params: frozenset[str] = frozenset()
    transport_kind: str = "openai_compatible"
    credential_configured: bool = False
    transport_ready: bool = False
    supports_tools: bool = True
    supports_streaming: bool = True
    status_detail: str = ""
    workload: str = ""

    def safe_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "api_base": self.api_base,
            "transport_kind": self.transport_kind,
            "credential_configured": self.credential_configured,
            "transport_ready": self.transport_ready,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "status_detail": self.status_detail,
            "workload": self.workload,
            "unsupported_params": sorted(self.unsupported_params),
        }


class NULlmRoutingBridge:
    """Load one shared router and expose compact, network-free decisions."""

    def __init__(self, config: NULlmRoutingConfig, *, opc_home: Path | None = None) -> None:
        self.config = config
        self.opc_home = Path(opc_home) if opc_home is not None else None
        self._router: Any | None = None
        self._config_path: Path | None = None
        self._load_attempted = False
        self._load_error = ""
        self._target_cache: dict[
            tuple[str, bool, tuple[str, ...]],
            tuple[RoutedLLMTarget, ...],
        ] = {}
        self._background_shadow: Any | None = None
        self._shadow_error = ""
        self._shadow_recovered = 0
        self._last_shadow_submission: dict[str, Any] = {}
        self._last_shadow_shutdown: dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def available(self) -> bool:
        return self._load_router() is not None

    @property
    def config_path(self) -> Path | None:
        self._load_router()
        return self._config_path

    @property
    def load_error(self) -> str:
        self._load_router()
        return self._load_error

    def _candidate_config_paths(self) -> list[Path]:
        candidates: list[Path] = []
        configured = str(self.config.config_path or "").strip()
        environment = str(os.environ.get(_ROUTER_CONFIG_ENV) or "").strip()
        for raw in (configured, environment):
            if not raw:
                continue
            path = Path(raw).expanduser()
            if path.is_absolute():
                candidates.append(path)
                continue
            candidates.append(Path.cwd() / path)
            if self.opc_home is not None:
                candidates.append(self.opc_home / path)

        # Development checkout convention used across NewUnivers sibling repos.
        repository_root = Path(__file__).resolve().parents[2]
        candidates.append(
            repository_root.parent
            / "nu-llm-routing-lib"
            / "configs"
            / "byteplus.production.json"
        )
        unique: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            resolved = path.resolve(strict=False)
            token = str(resolved)
            if token not in seen:
                seen.add(token)
                unique.append(resolved)
        return unique

    def _load_router(self) -> Any | None:
        if not self.enabled:
            return None
        if self._load_attempted:
            return self._router
        self._load_attempted = True
        try:
            from nu_llm_routing_lib.api import load_router_from_file
        except (ImportError, ModuleNotFoundError) as exc:
            self._load_error = f"nu-llm-routing-lib unavailable: {type(exc).__name__}"
            logger.info(self._load_error)
            return None

        for path in self._candidate_config_paths():
            if not path.is_file():
                continue
            try:
                self._router = load_router_from_file(path)
                self._config_path = path
                logger.info("NU LLM router loaded from {}", path)
                return self._router
            except Exception as exc:
                self._load_error = f"failed to load NU LLM router config {path}: {exc}"
                if not self.config.fail_open:
                    raise RuntimeError(self._load_error) from exc
                logger.warning(self._load_error)
                return None

        self._load_error = (
            "NU LLM routing is enabled but no router config was found; set "
            f"{_ROUTER_CONFIG_ENV} or llm.nu_routing.config_path"
        )
        if not self.config.fail_open:
            raise RuntimeError(self._load_error)
        logger.info(self._load_error)
        return None

    def _workload(self, task_type: str | None, has_tools: bool) -> str:
        if has_tools:
            return "agentic_tools"
        normalized = str(task_type or "").strip()
        if normalized and normalized in self.config.workload_map:
            return str(self.config.workload_map[normalized]).strip() or "dialogue"
        return "dialogue"

    def _metadata(
        self,
        *,
        workload: str,
        tags: Sequence[str] | None = None,
        sandboxed_tools: bool | None = None,
        gpu_free_vram_mib: int | None = None,
        hardware_profile: str | None = None,
    ) -> dict[str, Any]:
        from nu_llm_routing_lib import agentic_tools_metadata, coding_metadata
        from nu_llm_routing_lib.api import route_metadata

        extra = {
            "sandboxed_tools": (
                bool(self.config.sandboxed_tools)
                if sandboxed_tools is None
                else bool(sandboxed_tools)
            ),
            "gpu_free_vram_mib": int(
                self.config.gpu_free_vram_mib
                if gpu_free_vram_mib is None
                else max(0, gpu_free_vram_mib)
            ),
            "source_application": "openopc",
        }
        common = {
            "hardware_profile": hardware_profile or self.config.hardware_profile or None,
            "tags": list(tags or ()),
            "gpu_free_vram_mib": extra.pop("gpu_free_vram_mib"),
            "extra": extra,
        }
        if workload == "agentic_tools":
            return agentic_tools_metadata(
                sandboxed_tools=bool(common["extra"].pop("sandboxed_tools", False)),
                **common,
            )
        if workload == "coding":
            return coding_metadata(**common)
        return route_metadata(
            workload=workload,
            hardware_profile=common["hardware_profile"],
            tags=common["tags"],
            extra={
                **common["extra"],
                "gpu_free_vram_mib": common["gpu_free_vram_mib"],
            },
        )

    def _request(self, metadata: Mapping[str, Any]) -> Any:
        from nu_llm_routing_lib.api import ChatRequest

        return ChatRequest.from_text("OpenOPC route planning", metadata=dict(metadata))

    def diagnostics(
        self,
        *,
        workload: str,
        tags: Sequence[str] | None = None,
        sandboxed_tools: bool | None = None,
        gpu_free_vram_mib: int | None = None,
        hardware_profile: str | None = None,
        include_status: bool = False,
    ) -> dict[str, Any]:
        router = self._load_router()
        if router is None:
            return {
                "available": False,
                "config_path": str(self._config_path or ""),
                "error": self._load_error or "NU LLM routing is disabled",
            }
        metadata = self._metadata(
            workload=str(workload or "dialogue").strip() or "dialogue",
            tags=tags,
            sandboxed_tools=sandboxed_tools,
            gpu_free_vram_mib=gpu_free_vram_mib,
            hardware_profile=hardware_profile,
        )
        raw = router.route_diagnostics(
            self._request(metadata),
            include_status=bool(include_status),
        )
        return self._compact_diagnostics(raw, metadata=metadata)

    def targets(
        self,
        *,
        task_type: str | None,
        has_tools: bool,
        preferred_providers: Sequence[str] | None = None,
    ) -> tuple[RoutedLLMTarget, ...]:
        if has_tools and not self.config.apply_to_tool_calls:
            return ()
        preferred = tuple(
            dict.fromkeys(
                str(item).strip().lower()
                for item in (preferred_providers or ())
                if str(item).strip()
            )
        )
        cache_key = (str(task_type or ""), bool(has_tools), preferred)
        if cache_key in self._target_cache:
            return self._target_cache[cache_key]
        router = self._load_router()
        if router is None:
            return ()
        workload = self._workload(task_type, has_tools)
        metadata = self._metadata(workload=workload)
        request = self._request(metadata)
        try:
            ordered_names = list(router.route_order_for(request))
        except Exception as exc:
            if not self.config.fail_open:
                raise
            logger.warning("NU LLM route planning failed: {}", exc)
            return ()

        if preferred:
            preferred_order = [
                provider_name
                for preference in preferred
                for provider_name in ordered_names
                if _provider_family_match(provider_name, preference)
            ]
            ordered_names = list(
                dict.fromkeys([*preferred_order, *ordered_names])
            )
        allowed = {str(item).strip() for item in self.config.allowed_providers if str(item).strip()}
        targets: list[RoutedLLMTarget] = []
        for provider_name in ordered_names:
            if allowed and provider_name not in allowed:
                continue
            provider = getattr(router, "providers", {}).get(provider_name)
            target = self._target_from_provider(
                provider_name,
                provider,
                workload=workload,
            )
            if target is None:
                continue
            # Enabling routed tool turns does not make text-only transports
            # tool-capable. Only candidates that preserve the OpenAI tool-call
            # envelope can enter an executable tool turn.
            if has_tools and not target.supports_tools:
                continue
            targets.append(target)
            if len(targets) >= int(self.config.max_candidates):
                break
        result = tuple(targets)
        self._target_cache[cache_key] = result
        return result
    def has_usable_target(self) -> bool:
        return bool(self.targets(task_type="quick_tasks", has_tools=False))

    def provider_readiness(self, provider_name: str) -> dict[str, Any]:
        """Return a privacy-safe status code for one explicitly requested provider."""

        name = str(provider_name or "").strip()
        router = self._load_router()
        if router is None:
            return {
                "provider": name,
                "registered": False,
                "available": False,
                "category": "router_unavailable",
            }
        provider = getattr(router, "providers", {}).get(name)
        if provider is None:
            return {
                "provider": name,
                "registered": False,
                "available": False,
                "category": "not_registered",
            }
        status_reader = getattr(provider, "status", None)
        if not callable(status_reader):
            return {
                "provider": name,
                "registered": True,
                "available": False,
                "category": "status_unsupported",
            }
        try:
            status = status_reader()
        except Exception:
            return {
                "provider": name,
                "registered": True,
                "available": False,
                "category": "status_error",
            }
        available = bool(getattr(status, "available", False))
        detail = str(getattr(status, "detail", "") or "")
        return {
            "provider": name,
            "registered": True,
            "available": available,
            "category": "ready" if available else _provider_status_category(detail),
        }

    @staticmethod
    def _target_from_provider(
        provider_name: str,
        provider: Any,
        *,
        workload: str = "",
    ) -> RoutedLLMTarget | None:
        if provider is None:
            return None
        module_name = str(type(provider).__module__ or "")
        if module_name.endswith((".codex_cli", ".claude_cli", ".grok_cli")):
            try:
                status = provider.status()
            except Exception as exc:
                logger.warning("NU subscription provider health failed for {}: {}", provider_name, exc)
                return None
            if not bool(getattr(status, "available", False)):
                return None
            model = str(getattr(provider, "model", "") or provider_name).strip()
            return RoutedLLMTarget(
                provider=provider_name,
                model=model,
                transport_kind="subscription_cli",
                credential_configured=True,
                transport_ready=True,
                supports_tools=False,
                supports_streaming=False,
                status_detail=str(getattr(status, "detail", "") or "")[:500],
                workload=workload,
            )
        if module_name.endswith((".ollama", ".gemini")):
            try:
                status = provider.status()
            except Exception as exc:
                logger.warning("NU native provider health failed for {}: {}", provider_name, exc)
                return None
            if not bool(getattr(status, "available", False)):
                return None
            model = str(getattr(provider, "model", "") or provider_name).strip()
            return RoutedLLMTarget(
                provider=provider_name,
                model=model,
                transport_kind="nu_native",
                credential_configured=True,
                transport_ready=True,
                supports_tools=False,
                supports_streaming=False,
                status_detail=str(getattr(status, "detail", "") or "")[:500],
                workload=workload,
            )
        if not module_name.endswith(".openai_compatible"):
            return None
        api_base = str(getattr(provider, "base_url", "") or "").strip().rstrip("/")
        raw_model = str(getattr(provider, "model", "") or "").strip()
        if not api_base or not raw_model:
            return None
        env_names = list(getattr(provider, "api_key_envs", ()) or ())
        if not env_names:
            single = str(getattr(provider, "api_key_env", "") or "").strip()
            env_names = [single] if single else []
        api_key = next((os.environ.get(name) for name in env_names if os.environ.get(name)), None)
        is_local = api_base.startswith("http://127.0.0.1") or api_base.startswith("http://localhost")
        if not api_key and not is_local:
            return None
        model = raw_model if "/" in raw_model else f"openai/{raw_model}"
        return RoutedLLMTarget(
            provider=provider_name,
            model=model,
            api_base=api_base,
            api_key=api_key,
            extra_body=dict(getattr(provider, "extra_body", {}) or {}),
            unsupported_params=frozenset(
                str(item) for item in (getattr(provider, "unsupported_params", ()) or ())
            ),
            transport_kind="openai_compatible",
            credential_configured=bool(api_key) or is_local,
            transport_ready=bool(api_key) or is_local,
            workload=workload,
        )

    def _shadow_db_path(self) -> Path:
        configured = str(self.config.shadow_event_db_path or "").strip()
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_absolute():
                return candidate.resolve(strict=False)
            base = self.opc_home or Path.cwd()
            return (base / candidate).resolve(strict=False)
        base = self.opc_home or (Path.cwd() / ".opc")
        return (base / "operations" / "llm_shadow.sqlite3").resolve(strict=False)

    def start_background_shadow(self) -> dict[str, Any]:
        """Start an explicitly budgeted, durable challenger worker."""

        if not self.config.background_shadow_enabled:
            return self.shadow_status()
        if self._background_shadow is not None:
            prior = self._background_shadow.status()
            if prior.running or prior.accepting:
                return self.shadow_status()
            # A timeout-bounded shutdown may return while a daemon worker is
            # still finishing its provider request. Once that worker has
            # exited, allow the next service start to create a fresh executor.
            self._background_shadow = None
        experiment_id = str(self.config.shadow_experiment_id or "").strip()
        budget_limit = int(self.config.shadow_max_total_calls)
        if not experiment_id or budget_limit < 1:
            self._shadow_error = (
                "background shadow requires shadow_experiment_id and "
                "shadow_max_total_calls > 0"
            )
            if not self.config.fail_open:
                raise ValueError(self._shadow_error)
            return self.shadow_status()
        router = self._load_router()
        if router is None:
            self._shadow_error = self._load_error or "NU LLM router unavailable"
            return self.shadow_status()
        try:
            from nu_llm_routing_lib.api import (
                BackgroundShadowExecutor,
                DurableShadowBudget,
                ShadowExecutor,
            )
            from nu_llm_routing_lib.event_ingestion import build_routing_event_recorder
            from nu_llm_routing_lib.sqlite_event_store import SQLiteEventStore

            config_path = self.config_path
            if config_path is None or not config_path.is_file():
                raise ValueError("loaded NU LLM router config path is unavailable")
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(raw_config, Mapping):
                raise ValueError("NU LLM router config must be a JSON object")
            database_path = self._shadow_db_path()
            database_path.parent.mkdir(parents=True, exist_ok=True)
            recorder = getattr(router, "routing_event_recorder", None)
            if recorder is None:
                recorder = build_routing_event_recorder(database_path, raw_config)
                router.routing_event_recorder = recorder
            store = getattr(recorder, "store", None)
            if not isinstance(store, SQLiteEventStore):
                raise ValueError("background shadow requires a durable SQLite event recorder")
            database_path = Path(store.path)
            ledger = DurableShadowBudget(
                database_path,
                experiment_id=experiment_id,
                max_total_calls=budget_limit,
            )
            self._shadow_recovered = ledger.recover_stale(
                older_than_seconds=float(
                    self.config.shadow_recover_stale_after_seconds
                )
            )
            executor = ShadowExecutor(
                router,
                {
                    "enabled": True,
                    "max_total_calls": budget_limit,
                    "max_calls_per_decision": 1,
                    "timeout_seconds": float(self.config.shadow_timeout_seconds),
                },
                budget=ledger,
            )
            background = BackgroundShadowExecutor(
                executor,
                worker_count=int(self.config.shadow_worker_count),
                queue_capacity=int(self.config.shadow_queue_capacity),
            )
            background.start()
            self._background_shadow = background
            self._shadow_error = ""
        except Exception as exc:
            self._shadow_error = f"{type(exc).__name__}: {exc}"[:1000]
            logger.warning("NU LLM background shadow did not start: {}", self._shadow_error)
            if not self.config.fail_open:
                raise
        return self.shadow_status()

    def stop_background_shadow(self) -> dict[str, Any]:
        background = self._background_shadow
        if background is None:
            return self.shadow_status()
        status = background.stop(
            drain=True,
            timeout_seconds=float(self.config.shadow_shutdown_timeout_seconds),
        )
        report = status.to_dict()
        report["shutdown_timed_out"] = not status.drained
        self._last_shadow_shutdown = report
        if not status.running:
            self._background_shadow = None
        return self.shadow_status()

    def shadow_status(self) -> dict[str, Any]:
        status = (
            self._background_shadow.status().to_dict()
            if self._background_shadow is not None
            else {
                "running": False,
                "accepting": False,
                "drained": True,
                "queued": 0,
                "inflight": 0,
            }
        )
        return {
            "enabled": bool(self.config.background_shadow_enabled),
            "experiment_id": str(self.config.shadow_experiment_id or ""),
            "event_db_path": (
                str(self._shadow_db_path())
                if self.config.background_shadow_enabled
                else ""
            ),
            "error": self._shadow_error,
            "recovered_stale_reservations": self._shadow_recovered,
            "last_submission": dict(self._last_shadow_submission),
            "last_shutdown": dict(self._last_shadow_shutdown),
            **status,
        }

    def shadow_evidence_report(
        self,
        *,
        minimum_decisions: int = 20,
    ) -> dict[str, Any]:
        """Read the durable content-free event log without invoking a model."""

        from opc.operations.experiments import shadow_transport_report

        status = self.shadow_status()
        database_path = Path(str(status.get("event_db_path", "") or ""))
        if not status["enabled"]:
            return {
                "available": False,
                "status": status,
                "blockers": ["background_shadow_disabled"],
            }
        try:
            report = shadow_transport_report(
                database_path,
                minimum_decisions=max(1, int(minimum_decisions)),
            )
        except Exception as exc:
            return {
                "available": False,
                "status": status,
                "blockers": [f"{type(exc).__name__}: {exc}"],
            }
        return {"available": True, "status": status, **report}

    def execute_text_target(
        self,
        target: RoutedLLMTarget,
        *,
        messages: Sequence[Mapping[str, Any]],
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        """Execute one routed text-only target through its native NU provider."""
        if target.transport_kind not in {"subscription_cli", "nu_native"}:
            raise ValueError(f"target {target.provider!r} is not a native NU transport")
        router = self._load_router()
        if router is None:
            raise RuntimeError(self._load_error or "NU LLM router unavailable")
        provider = getattr(router, "providers", {}).get(target.provider)
        if provider is None:
            raise KeyError(f"NU LLM provider not found: {target.provider}")

        from nu_llm_routing_lib.api import ChatMessage, ChatRequest

        turns: list[Any] = []
        for item in messages:
            content = item.get("content", "")
            if not isinstance(content, str):
                raise ValueError(
                    "native NU routes currently require text-only message content"
                )
            turns.append(
                ChatMessage(
                    role=str(item.get("role", "user") or "user"),
                    content=content,
                )
            )
        request = ChatRequest(
            messages=turns,
            temperature=float(temperature),
            max_tokens=max(1, int(max_tokens)),
            timeout_seconds=max(1.0, float(timeout_seconds)),
            metadata={
                "workload": target.workload or "dialogue",
                **dict(metadata or {}),
            },
        )
        if self._background_shadow is not None:
            from nu_llm_routing_lib.api import chat_with_background_shadow

            result = chat_with_background_shadow(
                router,
                request,
                self._background_shadow,
                provider=target.provider,
            )
            self._last_shadow_submission = result.submission.to_dict()
            return result.response
        return provider.chat(request)

    def execute_subscription(
        self,
        target: RoutedLLMTarget,
        **kwargs: Any,
    ) -> Any:
        """Backward-compatible subscription-only execution entry point."""
        if target.transport_kind != "subscription_cli":
            raise ValueError(f"target {target.provider!r} is not a subscription CLI transport")
        return self.execute_text_target(target, **kwargs)

    @staticmethod
    def _compact_diagnostics(raw: Mapping[str, Any], *, metadata: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "available": True,
            "metadata": dict(metadata),
            "matched_profile_index": raw.get("matched_profile_index"),
            "matched_profile_id": raw.get("matched_profile_id"),
            "initial_order": list(raw.get("initial_order") or ()),
            "benchmark_order": list(raw.get("benchmark_order") or ()),
            "final_order": list(raw.get("final_order") or ()),
            "excluded": raw.get("excluded") or raw.get("provider_exclusions") or [],
        }
        ranking = raw.get("benchmark_ranking")
        if isinstance(ranking, Mapping):
            result["benchmark_ranking"] = {
                key: ranking.get(key)
                for key in ("evaluated", "applied", "reason", "categories")
                if key in ranking
            }
        checks = raw.get("provider_checks")
        if checks is not None:
            result["provider_checks"] = checks
        return result


def _provider_family_match(provider: str, preferred: str) -> bool:
    candidate = str(provider or "").strip().lower()
    family = str(preferred or "").strip().lower()
    return bool(
        candidate
        and family
        and (
            candidate == family
            or candidate.startswith(f"{family}-")
            or candidate.startswith(f"{family}_")
        )
    )


def _provider_status_category(detail: str) -> str:
    text = str(detail or "").strip().lower()
    if "subscription quota" in text or "usage limit" in text or "rate limit" in text:
        return "subscription_quota"
    if any(
        token in text
        for token in (
            "authentication",
            "not authenticated",
            "unauthorized",
            "login failed",
            "sign in",
        )
    ):
        return "authentication"
    if "not found" in text or "was not found on path" in text:
        return "missing_command"
    if "policy" in text:
        return "policy_denied"
    return "unavailable"
