# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json

import pytest

from app import config
from app.config import Settings

ENDPOINT = "https://example.services.ai.azure.com/api/projects/example"


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    agent_file = tmp_path / ".foundry" / "agent.json"
    monkeypatch.setattr(config, "AGENT_FILE", agent_file)
    monkeypatch.setattr(config.os, "environ", {})
    (tmp_path / ".env").write_text(f"FOUNDRY_PROJECT_ENDPOINT={ENDPOINT}\nWEBIQ_API_KEY=FILE_SECRET\n")
    return agent_file


def test_process_environment_overrides_dotenv_without_rewriting_it(configured, monkeypatch, tmp_path):
    monkeypatch.setenv("WEBIQ_API_KEY", "PROCESS_SECRET")
    settings = Settings.load()
    assert settings.webiq_key == "PROCESS_SECRET"
    assert "FILE_SECRET" in (tmp_path / ".env").read_text()


@pytest.mark.parametrize("variable", ["FOUNDRY_VOICE_AGENT_NAME", "FOUNDRY_VOICE_AGENT_VERSION"])
def test_agent_overrides_must_be_a_pair(configured, monkeypatch, variable):
    monkeypatch.setenv(variable, "example")
    with pytest.raises(ValueError, match="together"):
        Settings.load()


def test_project_switch_does_not_silently_reuse_an_old_agent(configured, monkeypatch):
    configured.parent.mkdir()
    configured.write_text(json.dumps({
        "name": "example-agent", "version": "1", "project_endpoint": ENDPOINT,
    }))
    assert Settings.load().agent_name == "example-agent"
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENDPOINT + "-other")
    with pytest.raises(ValueError, match="different Foundry project"):
        Settings.load()
    monkeypatch.setenv("FOUNDRY_VOICE_AGENT_NAME", "other-agent")
    monkeypatch.setenv("FOUNDRY_VOICE_AGENT_VERSION", "2")
    settings = Settings.load()
    assert settings.agent_name == "other-agent"
    assert settings.agent_version == "2"
    assert json.loads(configured.read_text())["name"] == "example-agent"


@pytest.mark.parametrize("metadata", [[], {"name": 42}, {"version": None}, {"project_endpoint": []}])
def test_invalid_metadata_reports_configuration_error(configured, metadata):
    configured.parent.mkdir()
    configured.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="recorded agent metadata"):
        Settings.load()


@pytest.mark.parametrize("value,enabled", [("0", False), ("1", True)])
def test_local_validation_refresh_flag(configured, monkeypatch, value, enabled):
    monkeypatch.setenv("LOCAL_AGENT_VALIDATION_ENABLED", value)
    assert Settings.load().local_agent_validation_enabled is enabled


def test_local_validation_refresh_defaults_on_and_rejects_invalid_flag(configured, monkeypatch):
    assert Settings.load().local_agent_validation_enabled is True
    monkeypatch.setenv("LOCAL_AGENT_VALIDATION_ENABLED", "true")
    with pytest.raises(ValueError, match="LOCAL_AGENT_VALIDATION_ENABLED"):
        Settings.load()
