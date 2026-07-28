"""Deterministic cross-channel commands for operating an OpenOPC company."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import shlex
from typing import Any, Mapping, Sequence


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class CompanyCommand:
    name: str
    arguments: dict[str, str] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": dict(self.arguments),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompanyCommand":
        return cls(
            name=str(data.get("name", "")),
            arguments={
                str(key): str(value)
                for key, value in dict(data.get("arguments", {}) or {}).items()
            },
            error=str(data.get("error", "") or ""),
        )


def command_help() -> str:
    return (
        "OpenOPC channel commands:\n"
        "- `/opc status` — deterministic Mission Control brief\n"
        "- `/opc skills <goal>` — installed-skill recommendations by role\n"
        "- `/opc action plan <kind> <target_id> <reason>` — create a short-lived plan\n"
        "- `/opc action execute <action_id> <plan_digest> <operator_id>` — confirm it\n"
        "- `/opc help` — show this list"
    )


def parse_company_command(content: str) -> CompanyCommand | None:
    text = str(content or "").strip()
    if not text.startswith("/opc"):
        return None
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        return CompanyCommand("invalid", error=f"cannot parse command: {exc}")
    if not parts or parts[0] != "/opc":
        return None
    if len(parts) == 1 or parts[1].lower() == "help":
        return CompanyCommand("help")
    verb = parts[1].lower()
    if verb == "status" and len(parts) == 2:
        return CompanyCommand("status")
    if verb == "skills" and len(parts) >= 3:
        return CompanyCommand("skills", {"goal": " ".join(parts[2:])})
    if verb == "action" and len(parts) >= 3:
        phase = parts[2].lower()
        if phase == "plan" and len(parts) >= 6:
            return CompanyCommand(
                "action_plan",
                {
                    "kind": parts[3],
                    "target_id": parts[4],
                    "reason": " ".join(parts[5:]),
                },
            )
        if phase == "execute" and len(parts) == 6:
            if not _SHA256_RE.fullmatch(parts[4]):
                return CompanyCommand(
                    "invalid",
                    error="action execute requires a 64-character SHA-256 plan digest",
                )
            return CompanyCommand(
                "action_execute",
                {
                    "action_id": parts[3],
                    "plan_digest": parts[4].lower(),
                    "operator_id": parts[5],
                },
            )
    return CompanyCommand("invalid", error="unsupported or incomplete /opc command")


async def execute_company_command(
    command: CompanyCommand,
    *,
    operations: Any | None,
    project_id: str,
    roles: Sequence[Mapping[str, Any]] = (),
) -> str:
    if command.name == "help":
        return command_help()
    if command.name == "invalid":
        return f"{command.error}\n\n{command_help()}"
    if operations is None:
        return "Operations is not enabled for this project."
    if command.name == "status":
        return await operations.mission_control.daily_brief(project_id=project_id)
    if command.name == "skills":
        report = operations.skill_assembly.recommend(
            goal=command.arguments["goal"],
            roles=roles,
            project_id=project_id,
        )
        lines = [
            f"Skill assembly — {report['goal']}",
            f"Catalog {report['catalog_size']} installed · digest {report['catalog_digest'][:12]}",
        ]
        for role in report["roles"]:
            additions = role["recommended_additions"]
            lines.append(f"- {role['role_id']}:")
            if additions:
                lines.extend(
                    (
                        f"  + {item['skill_ref']} "
                        f"(score {item['score']:.3f}, sha256:{item['content_digest'][:12]})"
                    )
                    for item in additions
                )
            else:
                lines.append("  + no matching installed additions")
            if role["capability_gaps"]:
                lines.append(
                    "  gaps: " + ", ".join(role["capability_gaps"])
                )
        lines.append("No role configuration was changed.")
        return "\n".join(lines)
    if command.name == "action_plan":
        action = await operations.operator_actions.plan(
            project_id=project_id,
            kind=command.arguments["kind"],
            target_id=command.arguments["target_id"],
            reason=command.arguments["reason"],
        )
        return (
            f"Governed action planned: {action['kind']} → {action['target_id']}\n"
            f"Action ID: {action['action_id']}\n"
            f"Plan digest: {action['plan_digest']}\n"
            f"Expires: {action['expires_at']}\n"
            f"Consequence: {action['consequence']}\n\n"
            "Confirm only after reviewing the exact target and digest:\n"
            f"`/opc action execute {action['action_id']} "
            f"{action['plan_digest']} <operator_id>`"
        )
    if command.name == "action_execute":
        action = await operations.operator_actions.execute(
            project_id=project_id,
            action_id=command.arguments["action_id"],
            plan_digest=command.arguments["plan_digest"],
            operator_id=command.arguments["operator_id"],
            confirmed=True,
        )
        return (
            f"Operator action {action['action_id']} is {action['status']}.\n"
            f"Target: {action['kind']} → {action['target_id']}\n"
            f"Operator: {action.get('operator_id', '')}\n"
            f"Receipt: {json.dumps(action.get('result', {}), ensure_ascii=False, sort_keys=True)}"
        )
    return command_help()
