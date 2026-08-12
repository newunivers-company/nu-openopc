"""Tool registry — central registry for all tools available to agents."""

from __future__ import annotations

import inspect
import traceback
from typing import Any, Callable, Coroutine

from loguru import logger

from opc.layer4_tools.output_budget import budget_tool_output

# Maximum serialized tool output size (characters). Outputs exceeding this
# limit are previewed before being returned to the agent loop; recoverable
# tools persist full output to disk.
_OUTPUT_LIMIT = 20_000


ToolFunc = Callable[..., Coroutine[Any, Any, Any]]

_PARAM_ALIASES: dict[str, str] = {
    "cmd": "command",
    "dir": "working_directory",
    "cwd": "working_directory",
    "directory": "working_directory",
    "pattern": "query",
    "search_query": "query",
    "search_term": "query",
    "keyword": "query",
    "filepath": "file_path",
    "filename": "file_path",
    "file": "file_path",
    "text": "content",
    "body": "content",
}


class ToolDefinition:
    """Metadata and callable for a single tool."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: ToolFunc,
        category: str = "general",
        requires_confirmation: bool = False,
        concurrency_safe: bool | None = None,
        read_only: bool | None = None,
        runtime_managed: bool = False,
        max_result_chars: int = _OUTPUT_LIMIT,
        persist_large_results: bool = True,
        self_bounded_output: bool = False,
        preview_chars: int | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.func = func
        self.category = category
        self.requires_confirmation = requires_confirmation
        self.concurrency_safe = concurrency_safe
        self.read_only = read_only
        self.runtime_managed = runtime_managed
        self.max_result_chars = max_result_chars
        self.persist_large_results = persist_large_results
        self.self_bounded_output = self_bounded_output
        self.preview_chars = preview_chars

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """Manages all available tools and dispatches execution."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._approval_callback: Any = None

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool
        logger.debug(f"Tool registered: {tool.name} [{tool.category}]")

    def unregister(self, name: str) -> None:
        """Remove a tool by name. No-op if not found."""
        if self._tools.pop(name, None):
            logger.debug(f"Tool unregistered: {name}")

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self, category: str | None = None, allowed: list[str] | None = None) -> list[ToolDefinition]:
        tools = list(self._tools.values())
        if category:
            tools = [t for t in tools if t.category == category]
        if allowed:
            tools = [t for t in tools if t.name in allowed]
        return tools

    def get_schemas(self, allowed: list[str] | None = None) -> list[dict[str, Any]]:
        tools = self.list_tools(allowed=allowed)
        return [t.to_schema() for t in tools]

    def set_approval_callback(self, callback: Any) -> None:
        self._approval_callback = callback

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        task: Any = None,
        on_progress: Any = None,
        skip_approval: bool = False,
    ) -> dict[str, Any]:
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"Unknown tool: {name}", "success": False}

        if self._approval_callback and not skip_approval:
            allowed, decision = await self._approval_callback(tool, arguments, task, on_progress)
            if not allowed:
                return {
                    "error": f"Tool execution blocked by autonomy policy: {decision.rationale}",
                    "approval": {
                        "action": decision.action.value,
                        "risk_level": decision.risk_level.value,
                        "confidence": decision.confidence,
                        "policy_source": decision.policy_source,
                        "rationale": decision.rationale,
                        **dict(decision.metadata or {}),
                    },
                    "success": False,
                }

        return await self.invoke(name, arguments, task=task, on_progress=on_progress)

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        task: Any = None,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"Unknown tool: {name}", "success": False}

        try:
            call_args = self._prepare_call_args(tool, arguments, task=task, on_progress=on_progress)
            result = await tool.func(**call_args)
            output = {"result": result, "success": True}
        except Exception as e:
            # Loguru has no stdlib-style ``exc_info`` kwarg: extra kwargs are
            # format() arguments, which forces str.format() on the message — an
            # error message containing ``{...}`` (e.g. a JSON error body) then
            # raises KeyError FROM the logging call, escaping this handler and
            # killing the caller instead of returning the error output below.
            # Positional formatting keeps brace-containing values inert, and
            # opt(exception=True) is the loguru way to log the traceback.
            logger.opt(exception=True).error(
                "Tool {} failed ({}): {}", name, type(e).__name__, e
            )
            output = {
                "error": str(e),
                "traceback": traceback.format_exc(),
                "success": False,
            }

        return self._truncate_output(output, tool=tool, task=task)

    def _prepare_call_args(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        *,
        task: Any = None,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        call_args = dict(arguments)
        signature = inspect.signature(tool.func)
        for alias, canonical in _PARAM_ALIASES.items():
            if alias in call_args and alias not in signature.parameters and canonical in signature.parameters:
                call_args[canonical] = call_args.pop(alias)
        if "task" in signature.parameters and "task" not in call_args:
            call_args["task"] = task
        if "on_progress" in signature.parameters and "on_progress" not in call_args:
            call_args["on_progress"] = on_progress
        # Reject unknown arguments with a helpful error instead of silently
        # dropping them. The error is caught by `invoke()` and packaged as
        # `{"success": False, "error": ...}`, which the agent's tool-call
        # loop feeds back into the model so it can retry with the right
        # parameter names. Silent dropping would hide data loss when a
        # tool signature is changed without updating agent prompts.
        has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in signature.parameters.values()
        )
        if not has_var_keyword:
            valid_params = [
                name for name in signature.parameters
                if name not in {"task", "on_progress"}
            ]
            extra = sorted(set(call_args) - set(signature.parameters))
            if extra:
                raise ValueError(
                    f"Tool `{tool.name}` received unknown argument(s): "
                    f"{', '.join(repr(key) for key in extra)}. "
                    f"Valid arguments: {', '.join(repr(p) for p in valid_params)}. "
                    "Please retry with a supported argument name."
                )
        return call_args

    @staticmethod
    def _truncate_output(
        output: dict[str, Any],
        *,
        tool: ToolDefinition,
        task: Any = None,
    ) -> dict[str, Any]:
        """Apply a recoverable output budget when serialized output is large."""
        return budget_tool_output(
            output,
            tool_name=tool.name,
            task=task,
            max_chars=int(tool.max_result_chars or _OUTPUT_LIMIT),
            preview_chars=tool.preview_chars,
            persist_large_results=bool(tool.persist_large_results),
            self_bounded_output=bool(tool.self_bounded_output),
        )
