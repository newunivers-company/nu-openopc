"""Size-safe normalization for durable verification evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any


_SUMMARY_CHAR_LIMIT = 4_000
_RAW_OUTPUT_CHAR_LIMIT = 12_000
_CHECK_COUNT_LIMIT = 24
_CHECK_CHAR_LIMIT = 4_000
_CHECK_FIELD_CHAR_LIMIT = 320
_CHECK_PREVIEW_CHAR_LIMIT = 1_000
_NESTED_STRING_LIMIT = 2_000
_NESTED_ITEM_LIMIT = 24
_NESTED_DEPTH_LIMIT = 4


def _clip_text(value: Any, *, limit: int, label: str) -> tuple[str, bool, int, str]:
    text = str(value or "").strip()
    original_chars = len(text)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    if original_chars <= limit:
        return text, False, original_chars, digest

    omitted = original_chars
    marker = ""
    available = 0
    for _attempt in range(3):
        marker = f"\n[{label}: {omitted} chars omitted; sha256={digest}]\n"
        available = max(0, limit - len(marker))
        omitted = original_chars - available
    head_size = (available * 2) // 3
    tail_size = available - head_size
    tail = text[-tail_size:] if tail_size else ""
    return (
        f"{text[:head_size].rstrip()}{marker}{tail.lstrip()}".strip(),
        True,
        original_chars,
        digest,
    )


def _compact_nested(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _clip_text(
            value,
            limit=_NESTED_STRING_LIMIT,
            label="verification field truncated",
        )[0]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= _NESTED_DEPTH_LIMIT:
        return _clip_text(
            json.dumps(value, ensure_ascii=False, default=str),
            limit=_NESTED_STRING_LIMIT,
            label="verification nesting truncated",
        )[0]
    if isinstance(value, dict):
        items = list(value.items())
        compacted = {
            str(key): _compact_nested(item, depth=depth + 1)
            for key, item in items[:_NESTED_ITEM_LIMIT]
        }
        if len(items) > _NESTED_ITEM_LIMIT:
            compacted["_omitted_keys"] = len(items) - _NESTED_ITEM_LIMIT
        return compacted
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        compacted = [
            _compact_nested(item, depth=depth + 1)
            for item in items[:_NESTED_ITEM_LIMIT]
        ]
        if len(items) > _NESTED_ITEM_LIMIT:
            compacted.append({"_omitted_items": len(items) - _NESTED_ITEM_LIMIT})
        return compacted
    return _clip_text(
        value,
        limit=_NESTED_STRING_LIMIT,
        label="verification value truncated",
    )[0]


def _compact_check(check: dict[str, Any]) -> dict[str, Any]:
    compacted = _compact_nested(check)
    if not isinstance(compacted, dict):
        compacted = {"summary": str(compacted)}
    serialized = json.dumps(
        compacted,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if len(serialized) <= _CHECK_CHAR_LIMIT:
        return compacted

    prior_preview = str(check.get("_preview", "") or "").strip()
    preview_source = prior_preview or serialized
    preview = _clip_text(
        preview_source,
        limit=_CHECK_PREVIEW_CHAR_LIMIT,
        label="verification check preview truncated",
    )[0]
    essential: dict[str, Any] = {}
    for key in (
        "check",
        "command",
        "result",
        "status",
        "exit_code",
        "summary",
        "observed_output",
    ):
        raw_field = check.get(key)
        if raw_field in (None, "", [], {}):
            continue
        compacted_field = _compact_nested(raw_field)
        encoded_field = json.dumps(
            compacted_field,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if len(encoded_field) > _CHECK_FIELD_CHAR_LIMIT:
            compacted_field = _clip_text(
                raw_field
                if isinstance(raw_field, str)
                else encoded_field,
                limit=_CHECK_FIELD_CHAR_LIMIT,
                label=f"verification {key} truncated",
            )[0]
        essential[key] = compacted_field
    essential["_compacted"] = True
    essential["_original_chars"] = int(
        check.get("_original_chars", 0) or len(serialized)
    )
    essential["_sha256"] = str(
        check.get("_sha256", "")
        or hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    )
    essential["_preview"] = preview
    return essential


def compact_verification_evidence(value: Any) -> dict[str, Any]:
    """Return bounded evidence while retaining verdict and audit identity.

    Full provider output is already written to the external runtime log when
    available. Durable Task, WorkItem, and checkpoint rows only need a bounded
    preview plus the original size and digest.
    """

    if not isinstance(value, dict):
        return {}

    summary, summary_truncated, summary_chars, summary_digest = _clip_text(
        value.get("summary", ""),
        limit=_SUMMARY_CHAR_LIMIT,
        label="verification summary truncated",
    )
    raw_output, raw_truncated, raw_chars, raw_digest = _clip_text(
        value.get("raw_output", ""),
        limit=_RAW_OUTPUT_CHAR_LIMIT,
        label="verification raw output truncated",
    )
    raw_checks = [
        item for item in list(value.get("checks", []) or []) if isinstance(item, dict)
    ]
    checks = [_compact_check(item) for item in raw_checks[:_CHECK_COUNT_LIMIT]]

    evidence = {
        "status": str(value.get("status", "") or "").strip(),
        "verdict": str(value.get("verdict", "") or "").strip().lower(),
        "summary": summary,
        "checks": checks,
        "raw_output": raw_output,
    }
    if summary_truncated or bool(value.get("summary_truncated", False)):
        evidence.update(
            {
                "summary_truncated": True,
                "summary_original_chars": int(
                    value.get("summary_original_chars", 0) or summary_chars
                ),
                "summary_sha256": str(
                    value.get("summary_sha256", "") or summary_digest
                ),
            }
        )
    if raw_truncated or bool(value.get("raw_output_truncated", False)):
        evidence.update(
            {
                "raw_output_truncated": True,
                "raw_output_original_chars": int(
                    value.get("raw_output_original_chars", 0) or raw_chars
                ),
                "raw_output_sha256": str(
                    value.get("raw_output_sha256", "") or raw_digest
                ),
            }
        )
    if len(raw_checks) > _CHECK_COUNT_LIMIT or bool(
        value.get("checks_truncated", False)
    ):
        evidence["checks_truncated"] = True
        evidence["checks_original_count"] = int(
            value.get("checks_original_count", 0) or len(raw_checks)
        )
    return evidence
