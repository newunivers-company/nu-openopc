from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from opc.operations.canary import build_drill_result_template
from opc.operations.shadow_artifacts import ShadowArtifactStore
from opc.operations.shadow_traffic import drive_shadow_prompts, load_prompts


class DrillTemplateTests(unittest.TestCase):
    def test_template_starts_unpassable_and_carries_runbook(self) -> None:
        template = build_drill_result_template(
            "credential_expiry", provider="codex", model="gpt-5.6-sol"
        )
        self.assertEqual(template["provider"], "codex")
        self.assertFalse(template["actual_injection"])
        self.assertFalse(template["recovery_verified"])
        self.assertEqual(template["authority"], "")
        self.assertEqual(template["evidence"], [])
        self.assertTrue(template["runbook"])

    def test_all_required_scenarios_have_templates(self) -> None:
        for scenario in (
            "credential_expiry",
            "transport_timeout",
            "quota_exhaustion",
            "model_drift",
        ):
            self.assertEqual(
                build_drill_result_template(scenario)["scenario"], scenario
            )

    def test_unknown_scenario_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_drill_result_template("chaos_monkey")


class ShadowArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ShadowArtifactStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_and_get_round_trip_with_sha256(self) -> None:
        index = self.store.add(
            "decision-1", served_text="served body", challenger_text="challenger body"
        )
        self.assertEqual(
            index["served_sha256"],
            hashlib.sha256(b"served body").hexdigest(),
        )
        digests = self.store.digests("decision-1")
        self.assertEqual(digests["served_artifact_digest"], index["served_sha256"])
        self.assertEqual(
            digests["challenger_artifact_digest"], index["challenger_sha256"]
        )

    def test_duplicate_requires_overwrite(self) -> None:
        self.store.add("decision-1", served_text="a", challenger_text="b")
        with self.assertRaises(FileExistsError):
            self.store.add("decision-1", served_text="a", challenger_text="b")
        self.store.add(
            "decision-1", served_text="a2", challenger_text="b2", overwrite=True
        )
        self.assertEqual(
            self.store.get("decision-1")["served_sha256"],
            hashlib.sha256(b"a2").hexdigest(),
        )

    def test_tampered_artifact_fails_closed(self) -> None:
        self.store.add("decision-1", served_text="a", challenger_text="b")
        served = Path(self._tmp.name) / "decision-1" / "served.md"
        served.write_text("tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.get("decision-1")

    def test_missing_decision_and_bad_ids_are_rejected(self) -> None:
        with self.assertRaises(KeyError):
            self.store.get("decision-404")
        with self.assertRaises(ValueError):
            self.store.add("../escape", served_text="a", challenger_text="b")
        with self.assertRaises(ValueError):
            self.store.add("d1", served_text=" ", challenger_text="b")


class ShadowTrafficTests(unittest.IsolatedAsyncioTestCase):
    async def test_driver_requires_confirmation_and_positive_ceiling(self) -> None:
        async def chat(prompt: str) -> str:
            return "ok"

        with self.assertRaises(ValueError):
            await drive_shadow_prompts(
                chat, ["p1"], max_calls=1, confirm_live=False
            )
        with self.assertRaises(ValueError):
            await drive_shadow_prompts(
                chat, ["p1"], max_calls=0, confirm_live=True
            )

    async def test_ceiling_bounds_calls_and_failures_do_not_stop_batch(self) -> None:
        calls: list[str] = []

        async def chat(prompt: str) -> str:
            calls.append(prompt)
            if prompt == "boom":
                raise RuntimeError("provider down")
            return "ok"

        report = await drive_shadow_prompts(
            chat,
            ["p1", "boom", "p3", "p4"],
            max_calls=3,
            confirm_live=True,
        )
        self.assertEqual(report["calls"], 3)
        self.assertEqual(report["succeeded"], 2)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("boom", json.dumps(report))  # content-free report

    async def test_delay_between_calls_uses_injected_sleep(self) -> None:
        sleeps: list[float] = []

        async def chat(prompt: str) -> str:
            return "ok"

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        await drive_shadow_prompts(
            chat,
            ["p1", "p2", "p3"],
            max_calls=3,
            confirm_live=True,
            delay_seconds=2.5,
            sleep=fake_sleep,
        )
        self.assertEqual(sleeps, [2.5, 2.5])


class PromptLoaderTests(unittest.TestCase):
    def test_loads_json_array_and_jsonl_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            array_path = Path(tmp) / "prompts.json"
            array_path.write_text(
                json.dumps(["Summarize A", {"prompt": "Draft B"}, {"other": "x"}]),
                encoding="utf-8",
            )
            self.assertEqual(load_prompts(array_path), ["Summarize A", "Draft B"])

            jsonl_path = Path(tmp) / "prompts.jsonl"
            jsonl_path.write_text(
                '"Translate C"\n{"prompt": "Review D"}\n', encoding="utf-8"
            )
            self.assertEqual(load_prompts(jsonl_path), ["Translate C", "Review D"])


if __name__ == "__main__":
    unittest.main()
