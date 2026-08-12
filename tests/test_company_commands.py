from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from opc.channels.base import BaseChannel
from opc.channels.company_commands import (
    execute_company_command,
    parse_company_command,
)


class _Channel(BaseChannel):
    name = "test"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, message) -> None:
        _ = message


class CompanyCommandParserTests(unittest.TestCase):
    def test_non_command_is_ignored_and_valid_commands_are_structured(self) -> None:
        self.assertIsNone(parse_company_command("please check status"))
        status = parse_company_command("/opc status")
        assert status is not None
        self.assertEqual(status.name, "status")
        plan = parse_company_command(
            "/opc action plan recover_run run-1 'reviewed last event'"
        )
        assert plan is not None
        self.assertEqual(plan.name, "action_plan")
        self.assertEqual(plan.arguments["target_id"], "run-1")
        self.assertEqual(plan.arguments["reason"], "reviewed last event")

    def test_execute_requires_exact_sha256_shape(self) -> None:
        invalid = parse_company_command(
            "/opc action execute action-1 short owner"
        )
        assert invalid is not None
        self.assertEqual(invalid.name, "invalid")
        valid = parse_company_command(
            f"/opc action execute action-1 {'a' * 64} owner"
        )
        assert valid is not None
        self.assertEqual(valid.name, "action_execute")

    def test_base_channel_attaches_structured_command_metadata(self) -> None:
        channel = _Channel(
            SimpleNamespace(allow_from=["*"]),
            SimpleNamespace(),
        )
        metadata = channel.build_inbound_metadata(
            {
                "sender_id": "owner",
                "chat_id": "chat",
                "content": "/opc skills improve release testing",
            },
            [],
        )
        self.assertEqual(metadata["company_command"]["name"], "skills")
        self.assertEqual(
            metadata["company_command"]["arguments"]["goal"],
            "improve release testing",
        )


class CompanyCommandExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_and_skill_commands_are_deterministic(self) -> None:
        mission = SimpleNamespace(
            daily_brief=AsyncMock(return_value="Mission Control — default")
        )
        assembly = SimpleNamespace(
            recommend=lambda **kwargs: {
                "goal": kwargs["goal"],
                "catalog_size": 2,
                "catalog_digest": "d" * 64,
                "roles": [
                    {
                        "role_id": "qa",
                        "recommended_additions": [
                            {
                                "skill_ref": "release-check",
                                "score": 2.0,
                                "content_digest": "e" * 64,
                            }
                        ],
                        "capability_gaps": [],
                    }
                ],
            }
        )
        operations = SimpleNamespace(
            mission_control=mission,
            skill_assembly=assembly,
        )
        status = await execute_company_command(
            parse_company_command("/opc status"),
            operations=operations,
            project_id="default",
        )
        skills = await execute_company_command(
            parse_company_command("/opc skills verified release"),
            operations=operations,
            project_id="default",
            roles=[{"role_id": "qa"}],
        )
        self.assertEqual(status, "Mission Control — default")
        self.assertIn("release-check", skills)
        self.assertIn("No role configuration was changed", skills)

    async def test_action_command_preserves_two_phase_confirmation(self) -> None:
        actions = SimpleNamespace(
            plan=AsyncMock(
                return_value={
                    "kind": "recover_run",
                    "target_id": "run-1",
                    "action_id": "action-1",
                    "plan_digest": "a" * 64,
                    "expires_at": "2026-07-27T12:00:00+00:00",
                    "consequence": "Acquire a fenced lease.",
                }
            ),
            execute=AsyncMock(
                return_value={
                    "kind": "recover_run",
                    "target_id": "run-1",
                    "action_id": "action-1",
                    "plan_digest": "a" * 64,
                    "operator_id": "owner",
                    "status": "executed",
                    "result": {"recovered": True},
                }
            ),
        )
        operations = SimpleNamespace(operator_actions=actions)
        plan_reply = await execute_company_command(
            parse_company_command(
                "/opc action plan recover_run run-1 'reviewed last event'"
            ),
            operations=operations,
            project_id="default",
        )
        self.assertIn("/opc action execute action-1", plan_reply)
        actions.execute.assert_not_awaited()

        execute_reply = await execute_company_command(
            parse_company_command(
                f"/opc action execute action-1 {'a' * 64} owner"
            ),
            operations=operations,
            project_id="default",
        )
        actions.execute.assert_awaited_once_with(
            project_id="default",
            action_id="action-1",
            plan_digest="a" * 64,
            operator_id="owner",
            confirmed=True,
        )
        self.assertIn("is executed", execute_reply)


if __name__ == "__main__":
    unittest.main()
