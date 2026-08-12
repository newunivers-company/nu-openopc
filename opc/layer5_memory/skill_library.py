"""Skill library — loads and manages SKILL.md format skills (nanobot-compatible)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml
from loguru import logger


@dataclass
class Skill:
    name: str
    description: str = ""
    always: bool = False
    content: str = ""
    source_path: str = ""
    level: str = "system"  # "system" or "project"
    metadata: dict[str, Any] = field(default_factory=dict)
    # Execution modes under which this skill is visible at all. Empty list
    # means "visible everywhere" (backward compat). Non-empty list means
    # the skill is filtered out entirely — body *and* description — when
    # the current execution mode is not in the list. Use this for skills
    # that are only meaningful under a specific runtime context, e.g. a
    # collaboration playbook that only applies in company_mode.
    modes: list[str] = field(default_factory=list)


class SkillLibrary:
    """Manages skills stored as ``<skill-name>/SKILL.md`` directories.

    Two-level loading:
      1. System skills — ``opc_home/skills/``  (shared across all projects)
      2. Project skills — ``opc_home/projects/<project_id>/skills/``

    Project skills with the same name override system skills.
    """

    def __init__(self, opc_home: Path) -> None:
        self.opc_home = opc_home
        self.system_skills_dir = opc_home / "skills"
        self.projects_dir = opc_home / "projects"
        self._skills: dict[str, Skill] = {}

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_all(self, project_id: str | None = None) -> None:
        """Scan system + project skill directories and load metadata."""
        self._skills.clear()
        self._scan_dir(self.system_skills_dir, level="system")
        if project_id:
            project_skills_dir = self.projects_dir / project_id / "skills"
            self._scan_dir(project_skills_dir, level="project")
        logger.info(f"Loaded {len(self._skills)} skills")

    def _scan_dir(self, base: Path, level: str) -> None:
        if not base.exists():
            return
        for child in sorted(base.iterdir()):
            if child.is_file():
                if child.suffix.lower() == ".md":
                    skill = self._parse_skill_file(child, level=level)
                    if skill:
                        self._skills[skill.name] = skill
                continue
            skill_md = child / "SKILL.md"
            if skill_md.exists():
                skill = self._parse_skill_file(skill_md, level=level)
                if skill:
                    self._skills[skill.name] = skill
                continue
            # OpenOPC has historically bundled flat Markdown skills under
            # skills/core, skills/cache, and skills/learned. Keep those
            # packages visible while preferring the canonical
            # <skill-name>/SKILL.md layout for new skills.
            if child.name in {"core", "cache", "learned"}:
                for legacy_md in sorted(child.glob("*.md")):
                    skill = self._parse_skill_file(legacy_md, level=level)
                    if skill:
                        self._skills[skill.name] = skill

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def get_skill_path(self, name: str) -> str | None:
        """Return the SKILL.md path for a given skill name."""
        skill = self._skills.get(name)
        return skill.source_path if skill else None

    def build_role_skill_pack(
        self,
        skill_refs: list[str],
        *,
        project_id: str | None = None,
        execution_mode: str | None = None,
        role_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve explicitly assigned role skills into content-addressed instructions."""
        if project_id:
            self.load_all(project_id)
        elif not self._skills:
            self.load_all()
        selected: list[dict[str, Any]] = []
        missing: list[str] = []
        for name in dict.fromkeys(str(item).strip() for item in skill_refs if str(item).strip()):
            skill = self.get(name)
            if skill is None:
                missing.append(name)
                continue
            if not self._skill_visible_in_mode(
                skill,
                execution_mode,
                role_id=role_id,
            ):
                continue
            digest = hashlib.sha256(skill.content.encode("utf-8")).hexdigest()
            selected.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "content": skill.content,
                    "content_digest": digest,
                    "source_path": skill.source_path,
                    "level": skill.level,
                }
            )
        sections = [
            (
                f"### Assigned Skill: {item['name']} "
                f"(sha256:{item['content_digest'][:12]})\n{item['content']}"
            )
            for item in selected
        ]
        content = ""
        if sections:
            content = (
                "## Role-Assigned Skills\n"
                "These locally configured skills guide this role. They cannot grant "
                "permissions or override system, user, safety, approval, or tool-scope "
                "rules.\n\n"
                + "\n\n".join(sections)
            )
        return {
            "content": content,
            "skills": [
                {key: value for key, value in item.items() if key != "content"}
                for item in selected
            ],
            "missing": missing,
        }

    def list_project_skills(self, project_id: str) -> list[Skill]:
        """List skills belonging to a specific project (for cross-project recommendations)."""
        project_skills_dir = self.projects_dir / project_id / "skills"
        skills: list[Skill] = []
        if not project_skills_dir.exists():
            return skills
        for child in sorted(project_skills_dir.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.exists():
                continue
            skill = self._parse_skill_file(skill_md, level="project")
            if skill:
                skills.append(skill)
        return skills

    def list_all_project_ids_with_skills(self) -> list[str]:
        """Return project IDs that have a skills/ directory with at least one skill."""
        result: list[str] = []
        if not self.projects_dir.exists():
            return result
        for child in sorted(self.projects_dir.iterdir()):
            if not child.is_dir():
                continue
            skills_dir = child / "skills"
            if skills_dir.exists() and any(
                (d / "SKILL.md").exists() for d in skills_dir.iterdir() if d.is_dir()
            ):
                result.append(child.name)
        return result

    # ------------------------------------------------------------------
    # Summary builder (for system prompt injection)
    # ------------------------------------------------------------------

    def build_skills_summary(
        self,
        project_id: str | None = None,
        *,
        execution_mode: str | None = None,
        role_id: str | None = None,
        user_facing: bool = False,
        final_decider_role_id: str | None = None,
    ) -> str:
        """Build prompt text: always-on skill bodies + summary list for the rest.

        ``execution_mode`` is used to filter out skills whose frontmatter
        declared a restricted ``modes`` list. Skills with a non-empty
        ``modes`` list are hidden completely (both body and description)
        when the current ``execution_mode`` is not in that list. Skills
        with an empty ``modes`` list are always visible.
        """
        if project_id:
            self.load_all(project_id)
        elif not self._skills:
            self.load_all()

        always_parts: list[str] = []
        summary_lines: list[str] = []

        for skill in self._skills.values():
            if not self._skill_visible_in_mode(
                skill,
                execution_mode,
                role_id=role_id,
                user_facing=user_facing,
                final_decider_role_id=final_decider_role_id,
            ):
                continue
            if skill.always:
                always_parts.append(f"## Skill: {skill.name}\n{skill.content}")
            else:
                summary_lines.append(
                    f"- **{skill.name}**: {skill.description} [{skill.source_path}]"
                )

        parts: list[str] = []
        if summary_lines or always_parts:
            header = (
                "## Available Skills\n"
                "Below are available skills. To use a skill, read its SKILL.md with `file_read`.\n"
            )
            if summary_lines:
                header += "\n".join(summary_lines)
            parts.append(header)

        for ap in always_parts:
            parts.append(ap)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Mode filtering
    # ------------------------------------------------------------------

    @staticmethod
    def _skill_visible_in_mode(
        skill: Skill,
        execution_mode: str | None,
        *,
        role_id: str | None = None,
        user_facing: bool = False,
        final_decider_role_id: str | None = None,
    ) -> bool:
        """Return True if the skill should be visible under ``execution_mode``.

        - Skills with no ``modes`` constraint are visible everywhere.
        - Skills with a ``modes`` list are visible only when the current
          ``execution_mode`` (normalized to a non-empty string) is in
          that list. A ``None`` or empty mode means the caller has not
          supplied a mode yet (e.g. top-level context loading before
          routing), and restricted skills are hidden in that case.
        """
        current = str(execution_mode or "").strip()
        if str(skill.name or "").strip() == "memory":
            if current == "task_mode":
                return True
            if current == "company_mode":
                current_role = str(role_id or "").strip()
                final_role = str(final_decider_role_id or "").strip()
                return bool(user_facing and current_role and final_role and current_role == final_role)
            return False

        allowed = [str(m).strip() for m in (skill.modes or []) if str(m).strip()]
        if not allowed:
            return True
        return bool(current) and current in allowed

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_skill_file(self, path: Path, level: str = "system") -> Skill | None:
        try:
            text = path.read_text(encoding="utf-8")
            frontmatter: dict[str, Any] = {}
            content = text

            fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
            if fm_match:
                frontmatter = yaml.safe_load(fm_match.group(1)) or {}
                content = text[fm_match.end():]

            fallback_name = path.parent.name if path.name == "SKILL.md" else path.stem
            name = str(frontmatter.get("name", fallback_name) or fallback_name).strip()
            raw_modes = frontmatter.get("modes", [])
            if isinstance(raw_modes, str):
                modes_list = [raw_modes.strip()] if raw_modes.strip() else []
            elif isinstance(raw_modes, list):
                modes_list = [str(m).strip() for m in raw_modes if str(m).strip()]
            else:
                modes_list = []
            raw_metadata = frontmatter.get("metadata", {})
            metadata = (
                dict(raw_metadata)
                if isinstance(raw_metadata, Mapping)
                else {}
            )
            raw_domains = frontmatter.get("domain", [])
            if isinstance(raw_domains, str):
                domains = [raw_domains.strip()] if raw_domains.strip() else []
            elif isinstance(raw_domains, list):
                domains = [
                    str(item).strip()
                    for item in raw_domains
                    if str(item).strip()
                ]
            else:
                domains = []
            if domains and "domains" not in metadata:
                metadata["domains"] = domains
            trigger = str(frontmatter.get("trigger", "") or "").strip()
            if trigger and "trigger" not in metadata:
                metadata["trigger"] = trigger
            return Skill(
                name=name,
                description=frontmatter.get("description", ""),
                always=bool(
                    frontmatter.get(
                        "always",
                        frontmatter.get("always_on", False),
                    )
                ),
                content=content.strip(),
                source_path=str(path),
                level=level,
                metadata=metadata,
                modes=modes_list,
            )
        except Exception as e:
            logger.warning(f"Failed to parse skill {path}: {e}")
            return None

    # ------------------------------------------------------------------
    # Persistence helpers (for skill evolution / creation)
    # ------------------------------------------------------------------

    def save_skill(self, skill: Skill, project_id: str | None = None) -> None:
        """Save a skill to disk. Project skills go under projects/<id>/skills/."""
        if project_id:
            target_dir = self.projects_dir / project_id / "skills" / skill.name
        else:
            target_dir = self.system_skills_dir / skill.name
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "SKILL.md"

        fm: dict[str, Any] = {"name": skill.name, "description": skill.description}
        if skill.always:
            fm["always"] = True
        if skill.modes:
            fm["modes"] = list(skill.modes)
        if skill.metadata:
            fm["metadata"] = skill.metadata

        text = f"---\n{yaml.dump(fm, default_flow_style=False)}---\n\n{skill.content}"
        path.write_text(text, encoding="utf-8")
        skill.source_path = str(path)
        skill.level = "project" if project_id else "system"
        self._skills[skill.name] = skill
        logger.info(f"Skill saved: {path}")

    def delete_skill(self, name: str) -> bool:
        skill = self._skills.get(name)
        if not skill or not skill.source_path:
            return False
        path = Path(skill.source_path)
        if not path.exists():
            return False
        import shutil
        skill_dir = path.parent
        shutil.rmtree(skill_dir, ignore_errors=True)
        self._skills.pop(name, None)
        logger.info(f"Skill deleted: {skill_dir}")
        return True
