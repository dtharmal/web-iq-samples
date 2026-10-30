# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from pathlib import Path

import pytest

from webiq_market_briefing.config import load_azure_openai_settings


def test_load_azure_openai_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "azure_openai": {
                    "endpoint": "https://example.openai.azure.com/",
                    "deployment": "example-deployment",
                    "api_version": "example-version",
                }
            }
        ),
        encoding="utf-8",
    )

    settings = load_azure_openai_settings(config_path)

    assert settings.endpoint == "https://example.openai.azure.com/"
    assert settings.deployment == "example-deployment"
    assert settings.api_version == "example-version"


def test_load_azure_openai_settings_rejects_placeholders(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "azure_openai": {
                    "endpoint": "https://<your-resource>.openai.azure.com/",
                    "deployment": "example-deployment",
                    "api_version": "example-version",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="endpoint placeholder"):
        load_azure_openai_settings(config_path)
