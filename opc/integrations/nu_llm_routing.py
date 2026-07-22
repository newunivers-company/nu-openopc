"""Safe planning bridge from OpenOPC to ``nu-llm-routing-lib``.

The shared router owns provider policy and ordering. OpenOPC keeps transport
ownership because its native runtime requires OpenAI-compatible tool-call
envelopes that the shared router intentionally does not expose yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    api_base: str
    api_key: str | None = field(default=None, repr=False)
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    unsupported_params: frozenset[str] = frozenset()

    def safe_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "api_base": self.api_base,
            "credential_configured": bool(self.api_key),
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
        self._target_cache: dict[tuple[str, bool], tuple[RoutedLLMTarget, ...]] = {}

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
    ) -> tuple[RoutedLLMTarget, ...]:
        if has_tools and not self.config.apply_to_tool_calls:
            return ()
        cache_key = (str(task_type or ""), bool(has_tools))
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

        allowed = {str(item).strip() for item in self.config.allowed_providers if str(item).strip()}
        targets: list[RoutedLLMTarget] = []
        for provider_name in ordered_names:
            if allowed and provider_name not in allowed:
                continue
            provider = getattr(router, "providers", {}).get(provider_name)
            target = self._target_from_provider(provider_name, provider)
            if target is None:
                continue
            targets.append(target)
            if len(targets) >= int(self.config.max_candidates):
                break
        result = tuple(targets)
        self._target_cache[cache_key] = result
        return result
    def has_usable_target(self) -> bool:
        return bool(self.targets(task_type="quick_tasks", has_tools=False))

    @staticmethod
    def _target_from_provider(provider_name: str, provider: Any) -> RoutedLLMTarget | None:
        if provider is None:
            return None
        module_name = str(type(provider).__module__ or "")
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
        )

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
