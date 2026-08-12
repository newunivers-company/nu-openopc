from __future__ import annotations

import unittest

from opc.operations.benchmarks import _TRUSTED_AUTHORITIES
from opc.operations.judging import (
    DRAFT_AUTHORITY,
    JudgmentRubric,
    build_draft_prompt,
    confirm_draft,
    parse_draft_response,
)

_GOAL = {
    "goal_id": "benchmark-software-race-fix",
    "acceptance_criteria": [
        {"criterion_id": "correctness", "description": "Race is fixed", "minimum_score": 0.9},
        {"criterion_id": "evidence", "description": "Tests prove it", "minimum_score": 0.85},
    ],
}


class JudgmentRubricTests(unittest.TestCase):
    def test_rubric_digest_is_deterministic_and_criteria_sensitive(self) -> None:
        first = JudgmentRubric.from_goal(_GOAL)
        second = JudgmentRubric.from_goal(_GOAL)
        self.assertEqual(first.digest, second.digest)
        changed = JudgmentRubric.from_goal(
            {
                **_GOAL,
                "acceptance_criteria": [
                    {**_GOAL["acceptance_criteria"][0], "minimum_score": 0.5},
                    _GOAL["acceptance_criteria"][1],
                ],
            }
        )
        self.assertNotEqual(first.digest, changed.digest)

    def test_rubric_requires_criteria(self) -> None:
        with self.assertRaises(ValueError):
            JudgmentRubric.from_goal({"goal_id": "empty", "acceptance_criteria": []})


class DraftPromptTests(unittest.TestCase):
    def test_prompt_contains_criteria_and_truncates_artifacts(self) -> None:
        rubric = JudgmentRubric.from_goal(_GOAL)
        messages = build_draft_prompt(
            rubric,
            artifacts={"output.md": "A" * 100, "notes.md": "B" * 100},
            max_artifact_chars=120,
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("DRAFT", messages[0]["content"])
        user = messages[1]["content"]
        self.assertIn("correctness", user)
        self.assertIn("evidence", user)
        self.assertIn("[truncated]", user)


class ParseDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rubric = JudgmentRubric.from_goal(_GOAL)

    def test_parses_json_embedded_in_prose(self) -> None:
        response = (
            "Here is my assessment:\n"
            '{"criterion_scores": {"correctness": 0.92, "evidence": 0.88},'
            ' "criterion_notes": {"correctness": "lock added", "evidence": "test log"}}'
            "\nDone."
        )
        draft = parse_draft_response(
            self.rubric,
            run_id="run-1",
            judge_model="m-1",
            response_text=response,
            artifact_digest="a" * 64,
        )
        self.assertEqual(draft.authority, DRAFT_AUTHORITY)
        self.assertEqual(draft.criterion_scores["correctness"], 0.92)
        self.assertEqual(draft.rubric_digest, self.rubric.digest)
        self.assertEqual(draft.artifact_digest, "a" * 64)

    def test_missing_criterion_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            parse_draft_response(
                self.rubric,
                run_id="run-1",
                judge_model="m-1",
                response_text='{"criterion_scores": {"correctness": 0.9}}',
            )

    def test_out_of_range_score_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            parse_draft_response(
                self.rubric,
                run_id="run-1",
                judge_model="m-1",
                response_text=(
                    '{"criterion_scores": {"correctness": 1.4, "evidence": 0.9}}'
                ),
            )


class ConfirmDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        rubric = JudgmentRubric.from_goal(_GOAL)
        self.draft = parse_draft_response(
            rubric,
            run_id="run-1",
            judge_model="m-1",
            response_text=(
                '{"criterion_scores": {"correctness": 0.92, "evidence": 0.88},'
                ' "criterion_notes": {"correctness": "ok", "evidence": "ok"}}'
            ),
            artifact_digest="a" * 64,
        ).to_dict()

    def test_draft_authority_is_never_trusted_by_benchmark_gates(self) -> None:
        self.assertNotIn(DRAFT_AUTHORITY, _TRUSTED_AUTHORITIES)

    def test_confirmation_mints_human_authority_with_provenance(self) -> None:
        result = confirm_draft(
            self.draft,
            operator_id="ops-kim",
            adjusted_scores={"evidence": 0.8},
            evidence={"correctness": ["output.md"]},
            artifact_digest="a" * 64,
        )
        self.assertEqual(result["criterion_scores"]["evidence"], 0.8)
        self.assertEqual(result["criterion_scores"]["correctness"], 0.92)
        metadata = result["metadata"]
        self.assertEqual(metadata["judgment_authority"], "human_confirmed")
        self.assertEqual(metadata["confirmed_by"], "ops-kim")
        self.assertEqual(metadata["llm_draft"]["judge_model"], "m-1")
        self.assertEqual(metadata["llm_draft"]["draft_scores"]["evidence"], 0.88)
        self.assertEqual(metadata["llm_draft"]["adjusted"], ["evidence"])
        self.assertEqual(metadata["benchmark_artifact_digest"], "a" * 64)

    def test_confirmation_rejects_a_different_or_missing_artifact_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires the sealed artifact"):
            confirm_draft(self.draft, operator_id="ops")
        with self.assertRaisesRegex(ValueError, "differs from the reviewed draft"):
            confirm_draft(
                self.draft,
                operator_id="ops",
                artifact_digest="b" * 64,
            )

    def test_confirming_a_non_draft_is_rejected(self) -> None:
        confirmed = {**self.draft, "authority": "human_confirmed"}
        with self.assertRaises(ValueError):
            confirm_draft(
                confirmed,
                operator_id="ops-kim",
                artifact_digest="a" * 64,
            )

    def test_confirmation_requires_operator_and_valid_authority(self) -> None:
        with self.assertRaises(ValueError):
            confirm_draft(
                self.draft,
                operator_id="  ",
                artifact_digest="a" * 64,
            )
        with self.assertRaises(ValueError):
            confirm_draft(
                self.draft,
                operator_id="ops",
                authority="simulation",
                artifact_digest="a" * 64,
            )

    def test_adjustments_cannot_invent_criteria_or_exceed_range(self) -> None:
        with self.assertRaises(ValueError):
            confirm_draft(
                self.draft,
                operator_id="ops",
                adjusted_scores={"style": 0.9},
                artifact_digest="a" * 64,
            )
        with self.assertRaises(ValueError):
            confirm_draft(
                self.draft,
                operator_id="ops",
                adjusted_scores={"evidence": 1.5},
                artifact_digest="a" * 64,
            )


class AgreementTests(unittest.TestCase):
    def test_perfect_agreement_has_kappa_one(self) -> None:
        from opc.operations.judging import cohens_kappa

        self.assertEqual(cohens_kappa([True, False, True], [True, False, True]), 1.0)

    def test_known_kappa_value(self) -> None:
        from opc.operations.judging import cohens_kappa

        # 3/4 observed agreement; pA(T)=0.5, pB(T)=0.75 -> pe = 0.5
        kappa = cohens_kappa(
            [True, True, False, False], [True, True, True, False]
        )
        self.assertAlmostEqual(kappa, 0.5)

    def test_judgment_agreement_reports_disagreements_and_deltas(self) -> None:
        from opc.operations.judging import judgment_agreement

        minimums = {"correctness": 0.9, "evidence": 0.85}
        report = judgment_agreement(
            {"criterion_scores": {"correctness": 0.95, "evidence": 0.9}},
            {"criterion_scores": {"correctness": 0.92, "evidence": 0.7}},
            minimum_scores=minimums,
        )
        self.assertEqual(report["disagreements"], ["evidence"])
        self.assertEqual(report["pass_fail_agreement_rate"], 0.5)
        self.assertAlmostEqual(report["max_abs_score_delta"], 0.2)

    def test_agreement_requires_shared_criteria(self) -> None:
        from opc.operations.judging import judgment_agreement

        with self.assertRaises(ValueError):
            judgment_agreement(
                {"criterion_scores": {"a": 1.0}},
                {"criterion_scores": {"b": 1.0}},
                minimum_scores={"c": 0.5},
            )


if __name__ == "__main__":
    unittest.main()
