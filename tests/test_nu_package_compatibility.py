from __future__ import annotations

import pytest

from scripts.verify_nu_compatibility import verify


def test_installed_nu_stable_facades_are_compatible() -> None:
    llm = pytest.importorskip("nu_llm_routing_lib.api")
    resource = pytest.importorskip("nu_resource_gen_lib.api")
    report = verify(
        expected_llm=llm.__version__,
        expected_resource=resource.__version__,
    )

    assert report["compatible"] is True
    assert report["failures"] == []
    assert report["resource_stable_api"] == "1"
