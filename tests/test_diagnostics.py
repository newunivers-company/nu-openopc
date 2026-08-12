from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

from typer.testing import CliRunner

from opc.cli.app import app
from opc.core.config import OPCConfig
from opc.diagnostics import (
    channel_diagnostics,
    external_agent_diagnostics,
    nu_llm_routing_diagnostics,
)


def test_channel_diagnostics_reports_missing_field_names_without_secret_values(
    tmp_path: Path,
) -> None:
    config = OPCConfig()
    config.channels.slack.enabled = True
    config.channels.slack.bot_token = "super-secret-bot-token"
    config.channels.slack.app_token = ""

    report = channel_diagnostics(config)
    slack = next(row for row in report["providers"] if row["name"] == "slack")

    assert slack["enabled"] is True
    assert slack["configured"] is False
    assert slack["missing_config_fields"] == ["app_token"]
    assert "super-secret-bot-token" not in json.dumps(report)


def test_external_agent_diagnostics_ignores_disabled_missing_binaries(
    tmp_path: Path,
) -> None:
    config = OPCConfig()
    for item in config.agents.agents.values():
        item.enabled = False

    report = external_agent_diagnostics(config, opc_home=tmp_path / ".opc")

    assert report["enabled"] == 0
    assert report["ready"] == 0
    assert report["issues"] == []


def test_nu_llm_routing_diagnostics_reports_disabled_as_ready(tmp_path: Path) -> None:
    report = nu_llm_routing_diagnostics(OPCConfig(), opc_home=tmp_path / ".opc")

    assert report["state"] == "disabled"
    assert report["ready"] is True
    assert report["route_planning_available"] is False
    assert report["fallback_active"] is False
    assert report["issues"] == []
    assert report["notices"] == []


def test_nu_llm_routing_diagnostics_surfaces_fail_open_fallback(tmp_path: Path) -> None:
    config = OPCConfig()
    config.llm.nu_routing.enabled = True

    with patch(
        "opc.integrations.nu_llm_routing.NULlmRoutingBridge._candidate_config_paths",
        return_value=[],
    ):
        report = nu_llm_routing_diagnostics(config, opc_home=tmp_path / ".opc")

    assert report["state"] == "fallback"
    assert report["ready"] is True
    assert report["route_planning_available"] is False
    assert report["fallback_active"] is True
    assert report["issues"] == []
    assert report["notices"] == [
        "NU LLM routing is enabled but no router config was found; set "
        "NU_LLM_ROUTER_CONFIG or llm.nu_routing.config_path"
    ]


def test_doctor_json_identifies_partial_initialization_and_strict_failure(
    tmp_path: Path,
) -> None:
    opc_home = tmp_path / ".opc"
    config_dir = opc_home / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "approval_allowlist.yaml").write_text(
        "version: 1\n", encoding="utf-8"
    )

    with (
        patch("opc.cli.app.get_opc_home", return_value=opc_home),
        patch(
            "opc.core.config.get_project_workplace",
            return_value=tmp_path / "workspace",
        ),
    ):
        result = CliRunner().invoke(app, ["doctor", "--json", "--strict"])

    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["ok"] is False
    assert report["initialization"]["state"] == "partial"
    assert "opc init --repair" in report["issues"][0]


def test_doctor_reports_enabled_channel_configuration_gap(tmp_path: Path) -> None:
    opc_home = tmp_path / ".opc"
    config = OPCConfig()
    for item in config.agents.agents.values():
        item.enabled = False
    config.channels.slack.enabled = True
    config.channels.slack.bot_token = "xoxb-secret"
    config.channels.slack.app_token = ""
    config.save(opc_home / "config")

    with (
        patch("opc.cli.app.get_opc_home", return_value=opc_home),
        patch(
            "opc.core.config.get_project_workplace",
            return_value=tmp_path / "workspace",
        ),
    ):
        result = CliRunner().invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert "xoxb-secret" not in result.output
    report = json.loads(result.output)
    assert report["initialization"]["ready"] is True
    assert report["channels"]["enabled"] == 1
    assert any(
        "slack: missing required config: app_token" in issue
        for issue in report["issues"]
    )


def test_doctor_surfaces_nu_routing_fallback_without_failing_strict(
    tmp_path: Path,
) -> None:
    opc_home = tmp_path / ".opc"
    config = OPCConfig()
    for item in config.agents.agents.values():
        item.enabled = False
    config.llm.nu_routing.enabled = True
    config.llm.nu_routing.fail_open = True
    config.save(opc_home / "config")

    with (
        patch("opc.cli.app.get_opc_home", return_value=opc_home),
        patch(
            "opc.core.config.get_project_workplace",
            return_value=tmp_path / "workspace",
        ),
        patch(
            "opc.integrations.nu_llm_routing.NULlmRoutingBridge._candidate_config_paths",
            return_value=[],
        ),
    ):
        result = CliRunner().invoke(app, ["doctor", "--json", "--strict"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is True
    assert report["llm_routing"]["state"] == "fallback"
    assert report["llm_routing"]["fallback_active"] is True
    assert report["llm_routing"]["notices"]
    assert report["issues"] == []


def test_doctor_fails_strict_when_nu_routing_is_fail_closed(tmp_path: Path) -> None:
    opc_home = tmp_path / ".opc"
    config = OPCConfig()
    for item in config.agents.agents.values():
        item.enabled = False
    config.llm.nu_routing.enabled = True
    config.llm.nu_routing.fail_open = False
    config.save(opc_home / "config")

    with (
        patch("opc.cli.app.get_opc_home", return_value=opc_home),
        patch(
            "opc.core.config.get_project_workplace",
            return_value=tmp_path / "workspace",
        ),
        patch(
            "opc.integrations.nu_llm_routing.NULlmRoutingBridge._candidate_config_paths",
            return_value=[],
        ),
    ):
        result = CliRunner().invoke(app, ["doctor", "--json", "--strict"])

    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["ok"] is False
    assert report["llm_routing"]["state"] == "blocked"
    assert report["llm_routing"]["ready"] is False
    assert report["llm_routing"]["issues"] == report["issues"]


def test_doctor_database_check_does_not_create_wal_sidecars(tmp_path: Path) -> None:
    opc_home = tmp_path / ".opc"
    config = OPCConfig()
    for item in config.agents.agents.values():
        item.enabled = False
    config.save(opc_home / "config")
    db_path = opc_home / "projects" / "example" / "tasks.db"
    db_path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE health_check (id INTEGER PRIMARY KEY)")
        connection.commit()
    sidecars = [Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
    assert not any(path.exists() for path in sidecars)

    with (
        patch("opc.cli.app.get_opc_home", return_value=opc_home),
        patch(
            "opc.core.config.get_project_workplace", return_value=tmp_path / "workspace"
        ),
    ):
        result = CliRunner().invoke(
            app,
            ["doctor", "--project", "example", "--json", "--strict"],
        )

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    project = next(
        row for row in report["databases"]["checks"] if row["name"] == "project"
    )
    assert project["quick_check"] == "ok"
    assert project["access"] == "read_only_immutable"
    assert not any(path.exists() for path in sidecars)
