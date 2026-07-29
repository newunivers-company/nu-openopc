from __future__ import annotations

from scripts.verify_nu_compatibility import verify


def test_pinned_nu_stable_facades_are_compatible() -> None:
    report = verify(expected_llm="0.4.0", expected_resource="0.2.2")

    assert report["compatible"] is True
    assert report["failures"] == []
    assert report["resource_stable_api"] == "1"
