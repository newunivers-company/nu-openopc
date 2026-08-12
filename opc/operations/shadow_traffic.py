"""Synthetic text-turn driver that feeds the shadow decision supply.

Shadow decisions only arise on tool-less text turns served through the NU
routing path, which real agent workloads rarely produce. This driver replays
curated prompts through the injected chat callable — the same serving path
live traffic uses — so every generated decision is a genuine transport call
(``actual_run`` stays true). It is fail-closed: driving costs real provider
calls, so an explicit confirmation and a hard call ceiling are required, and
prompt bodies never appear in the report (content-free, like the ledger).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any


def load_prompts(path: Path) -> list[str]:
    """Load prompts from a JSON array or JSONL of strings / {"prompt": ...}."""

    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    rows: list[Any]
    if text.startswith("["):
        rows = json.loads(text)
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    prompts: list[str] = []
    for row in rows:
        if isinstance(row, str):
            candidate = row.strip()
        elif isinstance(row, dict):
            candidate = str(row.get("prompt", "")).strip()
        else:
            candidate = ""
        if candidate:
            prompts.append(candidate)
    return prompts


async def drive_shadow_prompts(
    chat: Callable[[str], Awaitable[Any]],
    prompts: Sequence[str],
    *,
    max_calls: int,
    confirm_live: bool,
    delay_seconds: float = 0.0,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> dict[str, Any]:
    """Replay prompts through the serving path under a hard call ceiling."""

    if not confirm_live:
        raise ValueError(
            "shadow traffic drives live, billable provider calls; "
            "explicit confirmation is required"
        )
    if max_calls < 1:
        raise ValueError("max_calls must be positive")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")
    results: list[dict[str, Any]] = []
    calls = 0
    for prompt in prompts:
        if calls >= max_calls:
            break
        try:
            await chat(prompt)
            ok, error = True, ""
        except Exception as exc:  # noqa: BLE001 - one bad turn must not stop the batch
            ok, error = False, f"{type(exc).__name__}: {exc}"[:500]
        calls += 1
        results.append({"prompt_chars": len(prompt), "ok": ok, "error": error})
        if delay_seconds and calls < max_calls and calls < len(prompts):
            await sleep(delay_seconds)
    return {
        "requested_prompts": len(prompts),
        "calls": calls,
        "succeeded": sum(1 for item in results if item["ok"]),
        "failed": sum(1 for item in results if not item["ok"]),
        "results": results,
    }
