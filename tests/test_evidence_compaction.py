from __future__ import annotations

import hashlib
import json
import unittest

from opc.core.evidence import compact_verification_evidence


class VerificationEvidenceCompactionTests(unittest.TestCase):
    def test_large_provider_evidence_is_bounded_and_auditable(self) -> None:
        raw_output = "provider output\n" * 100_000
        huge_observation = "test output\n" * 50_000
        evidence = compact_verification_evidence(
            {
                "status": "provided",
                "verdict": "pass",
                "summary": "All checks passed.",
                "checks": [
                    {
                        "check": "full suite",
                        "command": "pytest -q",
                        "observed_output": huge_observation,
                        "result": "PASS",
                    }
                ],
                "raw_output": raw_output,
            }
        )

        serialized = json.dumps(evidence, ensure_ascii=False)
        self.assertLess(len(serialized), 30_000)
        self.assertEqual(evidence["status"], "provided")
        self.assertEqual(evidence["verdict"], "pass")
        self.assertTrue(evidence["raw_output_truncated"])
        self.assertEqual(
            evidence["raw_output_original_chars"],
            len(raw_output.strip()),
        )
        self.assertEqual(
            evidence["raw_output_sha256"],
            hashlib.sha256(raw_output.strip().encode("utf-8")).hexdigest(),
        )
        self.assertEqual(evidence["checks"][0]["command"], "pytest -q")
        self.assertEqual(evidence["checks"][0]["result"], "PASS")

    def test_compaction_is_idempotent_with_trailing_whitespace(self) -> None:
        first = compact_verification_evidence(
            {
                "status": "provided",
                "verdict": "pass",
                "summary": ("summary\n" * 2_000),
                "checks": [
                    {
                        "check": "full suite",
                        "command": "pytest -q",
                        "observed_output": "test output\n" * 2_000,
                        "result": "PASS",
                    }
                ],
                "raw_output": ("provider output\n" * 2_000),
            }
        )

        self.assertEqual(compact_verification_evidence(first), first)
        self.assertLess(
            len(json.dumps(first["checks"][0], ensure_ascii=False)),
            4_000,
        )


if __name__ == "__main__":
    unittest.main()
