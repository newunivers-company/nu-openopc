#!/usr/bin/env python3
"""Fail CI when an operations scorecard regresses beyond the allowed drop."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from opc.operations.evaluation import compare_scorecards
from opc.operations.models import RunScorecard


def _load(path: Path) -> RunScorecard:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"scorecard must be a JSON object: {path}")
    return RunScorecard.from_dict(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--maximum-regression", type=float, default=0.05)
    args = parser.parse_args()
    result = compare_scorecards(
        _load(args.candidate),
        _load(args.baseline),
        maximum_regression=args.maximum_regression,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
