from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from opc.layer5_memory.skill_library import SkillLibrary
from opc.operations.skill_assembly import SkillAssemblyService


class SkillAssemblyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._write_skill(
            "release-check",
            """---
name: release-check
description: Verify software releases with deterministic tests.
metadata:
  capabilities: [testing, release, evidence]
---

Inspect the diff, run focused tests, then run the full regression suite.
""",
        )
        self._write_skill(
            "research-evidence",
            """---
name: research-evidence
description: Collect and compare primary research evidence.
metadata:
  capabilities: [research, citations]
---

Record primary sources and distinguish facts from inference.
""",
        )
        self.library = SkillLibrary(self.root)
        self.library.load_all("default")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_skill(self, name: str, content: str) -> None:
        directory = self.root / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(content, encoding="utf-8")

    def test_role_pack_resolves_exact_content_and_reports_missing_refs(self) -> None:
        pack = self.library.build_role_skill_pack(
            ["release-check", "missing-skill", "release-check"],
            project_id="default",
            execution_mode="company_mode",
            role_id="qa",
        )

        self.assertEqual([item["name"] for item in pack["skills"]], ["release-check"])
        self.assertEqual(pack["missing"], ["missing-skill"])
        self.assertEqual(len(pack["skills"][0]["content_digest"]), 64)
        self.assertIn("Role-Assigned Skills", pack["content"])
        self.assertIn("cannot grant permissions", pack["content"])
        self.assertIn("run the full regression suite", pack["content"])

    def test_goal_to_role_recommendation_is_evidence_backed_and_non_mutating(self) -> None:
        roles = [
            {
                "role_id": "qa",
                "name": "Quality Engineer",
                "responsibility": "Test release quality and collect evidence.",
                "capabilities": ["testing", "release"],
                "skill_refs": [],
            },
            {
                "role_id": "analyst",
                "name": "Research Analyst",
                "responsibility": "Compare primary research and citations.",
                "skill_refs": ["research-evidence"],
            },
        ]
        report = SkillAssemblyService(self.library).recommend(
            goal="Ship a verified release with cited research",
            roles=roles,
            required_capabilities=["testing", "citations", "payroll"],
            project_id="default",
        )

        qa = next(item for item in report["roles"] if item["role_id"] == "qa")
        analyst = next(
            item for item in report["roles"] if item["role_id"] == "analyst"
        )
        self.assertEqual(qa["recommended_additions"][0]["skill_ref"], "release-check")
        self.assertEqual(
            len(qa["recommended_additions"][0]["content_digest"]),
            64,
        )
        self.assertIn("research-evidence", analyst["existing_skill_refs"])
        self.assertIn("payroll", report["organization_capability_gaps"])
        self.assertNotIn("payroll", qa["capability_gaps"])
        self.assertFalse(report["mutations_applied"])
        self.assertEqual(roles[0]["skill_refs"], [])
        self.assertEqual(len(report["catalog_digest"]), 64)

    def test_unbound_service_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no bound"):
            SkillAssemblyService().recommend(
                goal="test",
                roles=[{"role_id": "qa"}],
            )

    def test_incidental_body_term_is_not_capability_evidence(self) -> None:
        self._write_skill(
            "session-memory",
            """---
name: session-memory
description: Preserve durable session notes.
---

Use this after coding work to record stable decisions.
""",
        )
        self.library.load_all("default")

        report = SkillAssemblyService(self.library).recommend(
            goal="Implement a production service",
            roles=[
                {
                    "role_id": "engineer",
                    "capabilities": ["coding"],
                    "skill_refs": [],
                }
            ],
            required_capabilities=["coding"],
            project_id="default",
        )

        role = report["roles"][0]
        self.assertNotIn(
            "session-memory",
            [item["skill_ref"] for item in role["recommended_additions"]],
        )
        self.assertEqual(role["capability_gaps"], ["coding"])

    def test_legacy_flat_core_skills_are_loaded_with_domains(self) -> None:
        core = self.root / "skills" / "core"
        core.mkdir()
        (core / "deployment.md").write_text(
            """---
name: deployment
description: Production deployment and rollback.
domain: [deployment, devops]
always_on: true
trigger: When releasing software
---

Verify the release, deploy it, observe it, and retain a rollback path.
""",
            encoding="utf-8",
        )
        self.library.load_all("default")

        skill = self.library.get("deployment")
        self.assertIsNotNone(skill)
        assert skill is not None
        self.assertTrue(skill.always)
        self.assertEqual(skill.metadata["domains"], ["deployment", "devops"])
        self.assertEqual(skill.metadata["trigger"], "When releasing software")

        report = SkillAssemblyService(self.library).recommend(
            goal="Deploy a verified release",
            roles=[
                {
                    "role_id": "platform",
                    "capabilities": ["deployment"],
                    "skill_refs": [],
                }
            ],
            required_capabilities=["deployment"],
            project_id="default",
        )
        role = report["roles"][0]
        self.assertEqual(
            role["recommended_additions"][0]["skill_ref"],
            "deployment",
        )
        self.assertEqual(role["capability_gaps"], [])
        self.assertEqual(
            report["organization_capability_coverage"]["deployment"],
            ["platform"],
        )

    def test_global_capabilities_are_distributed_instead_of_mounted_everywhere(self) -> None:
        self._write_skill(
            "writing",
            """---
name: writing
description: Write clear operational documentation.
metadata:
  capabilities: [writing]
---

Produce concise documentation.
""",
        )
        self.library.load_all("default")

        report = SkillAssemblyService(self.library).recommend(
            goal="Ship code with clear documentation",
            roles=[
                {
                    "role_id": "engineer",
                    "capabilities": ["testing", "release"],
                    "skill_refs": [],
                },
                {
                    "role_id": "writer",
                    "capabilities": ["writing"],
                    "skill_refs": [],
                },
            ],
            required_capabilities=["testing", "release", "writing"],
            project_id="default",
        )

        engineer = report["roles"][0]
        writer = report["roles"][1]
        assert "writing" not in engineer["proposed_skill_refs"]
        assert "writing" in writer["proposed_skill_refs"]
        assert report["organization_capability_coverage"]["writing"] == [
            "writer"
        ]
        assert report["organization_capability_gaps"] == []


if __name__ == "__main__":
    unittest.main()
