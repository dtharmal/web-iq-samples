# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import os
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from webiq.errors import APIStatusError

from webiq_market_briefing import cli
from webiq_market_briefing.models import Evidence


_OFFLINE_CLI_SCRIPT = """
import runpy
import sys
from unittest.mock import patch

import httpx
from openai import AzureOpenAI
from webiq import WebIQClient
from webiq_market_briefing import report, retrieval

scenario = sys.argv.pop(1)
attempts = 0

def respond(request):
    global attempts
    if request.url.host == "example.openai.azure.com":
        return httpx.Response(200, headers={"x-request-id": "model-request-123"}, json={
            "id": "completion", "object": "chat.completion", "created": 0,
            "model": "deployment",
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": "Finding.[1]",
            }, "finish_reason": "stop"}],
        })
    attempts += 1
    if scenario == "failure" or (scenario == "retry" and attempts == 1):
        return httpx.Response(503, headers={"x-traceid": "trace-failure"}, json={
            "errorCode": "TransientError", "requestId": "web-request-123",
            "userMessage": "PRIVATE-ERROR-BODY",
        })
    return httpx.Response(200, headers={"x-traceid": "trace-success"}, json={
        "webResults": [{
            "title": "Title", "url": "https://example.com",
            "content": "PRIVATE-PASSAGE",
        }],
    } if request.url.path.endswith("/web") else {"newsResults": []})

def web_client(**kwargs):
    return WebIQClient(http_client=httpx.Client(
        base_url="https://api.microsoft.ai/v3",
        transport=httpx.MockTransport(respond),
    ), **kwargs)

def model_client(**kwargs):
    return AzureOpenAI(http_client=httpx.Client(
        transport=httpx.MockTransport(respond),
    ), **kwargs)

with patch.object(retrieval, "WebIQClient", web_client), \\
     patch.object(report, "AzureOpenAI", model_client), \\
     patch.object(retrieval.time, "sleep", lambda _: None):
    runpy.run_module("webiq_market_briefing", run_name="__main__")
"""


@pytest.mark.parametrize("scenario", ["success", "retry", "failure"])
def test_cli_emits_sanitized_diagnostics_to_stderr_in_clean_process(tmp_path, scenario) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"azure_openai": {
        "endpoint": "https://example.openai.azure.com",
        "api_version": "version", "deployment": "deployment",
    }}), encoding="utf-8")
    output = tmp_path / "report.md"
    output.write_text("Previous valid report", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, "-c", _OFFLINE_CLI_SCRIPT, scenario, "PRIVATE-TOPIC",
            "--query", "SEARCH-QUERY", "--config", str(config), "--output", str(output),
        ],
        cwd=tmp_path,
        env=os.environ | {
            "WEBIQ_API_KEY": "PRIVATE-WEB-KEY",
            "AZURE_OPENAI_API_KEY": "PRIVATE-MODEL-KEY",
        },
        capture_output=True, text=True, timeout=30,
    )
    assert "Retrieving evidence for 1 queries..." in result.stdout
    assert "service=" not in result.stdout
    assert re.search(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} "
        r"(?:INFO|WARNING) webiq_market_briefing service=webiq", result.stderr,
    )
    assert "service=webiq" in result.stderr
    assert 'operation=_search_web query="SEARCH-QUERY"' in result.stderr
    if scenario == "failure":
        assert result.returncode == 1, result.stderr
        assert result.stderr.count("trace_id=trace-failure") == 3
        assert "Request failed: exception=APIStatusError" in result.stderr
        assert "service=azure-openai" not in result.stderr
        assert "source_url=" not in result.stderr
        assert output.read_text(encoding="utf-8") == "Previous valid report"
    else:
        assert result.returncode == 0, result.stderr
        assert len(result.stdout.splitlines()) == 3
        assert "trace_id=trace-success" in result.stderr
        assert "service=azure-openai request_id=model-request-123" in result.stderr
        assert 'operation=_search_news query="SEARCH-QUERY"' in result.stderr
        assert 'source_url="https://example.com"' in result.stderr
        assert "Finding. [[1]](#source-1)" in output.read_text(encoding="utf-8")
    if scenario != "success":
        assert "retry=1 delay_seconds=" in result.stderr
        assert "request_id=web-request-123" in result.stderr
        assert "error_code=TransientError" in result.stderr
    for private_value in (
        "PRIVATE-TOPIC", "PRIVATE-PASSAGE",
        "PRIVATE-WEB-KEY", "PRIVATE-MODEL-KEY", "PRIVATE-ERROR-BODY",
    ):
        assert private_value not in result.stderr


@pytest.mark.parametrize("auth_mode", ["api-key", "entra"])
def test_cli_auth_and_source_output(monkeypatch, tmp_path, auth_mode) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "model-key")
    monkeypatch.delenv("WEBIQ_API_KEY", raising=False)
    if auth_mode == "api-key":
        monkeypatch.setenv("WEBIQ_API_KEY", "web-key")
    monkeypatch.setattr(cli, "load_azure_openai_settings", lambda _: SimpleNamespace(
        endpoint="endpoint", api_version="version", deployment="deployment",
    ))
    retrieve = MagicMock(return_value=[
        Evidence("Title", "https://example.com", "Retrieved passage", "query", "web"),
    ])
    monkeypatch.setattr(cli, "retrieve_evidence", retrieve)
    monkeypatch.setattr(cli, "generate_report_body", lambda **kwargs: "Finding [1].")
    output = tmp_path / "report.md"
    assert cli.main(["Topic", "--webiq-auth", auth_mode, "--output", str(output)]) == 0
    assert retrieve.call_args.kwargs["auth_mode"] == auth_mode
    assert retrieve.call_args.kwargs["api_key"] == ("web-key" if auth_mode == "api-key" else None)
    assert "[[1]](#source-1)" in output.read_text(encoding="utf-8")
    assert "[https://example\\.com](https://example.com)" in output.read_text(encoding="utf-8")
    assert "Retrieved passage" not in output.read_text(encoding="utf-8")


def test_cli_requires_api_key_by_default(monkeypatch) -> None:
    monkeypatch.delenv("WEBIQ_API_KEY", raising=False)
    with pytest.raises(ValueError, match="WEBIQ_API_KEY"):
        cli.main([])


def test_entrypoint_does_not_print_raw_service_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "main", MagicMock(side_effect=APIStatusError(
        400, "private response containing credentials",
    )))
    with pytest.raises(SystemExit) as error:
        cli.entrypoint()
    assert error.value.code == 1
    assert "private response" not in capsys.readouterr().err


def test_invalid_model_report_preserves_existing_output(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WEBIQ_API_KEY", "web-key")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "model-key")
    monkeypatch.setattr(cli, "load_azure_openai_settings", lambda _: SimpleNamespace(
        endpoint="endpoint", api_version="version", deployment="deployment",
    ))
    monkeypatch.setattr(cli, "retrieve_evidence", lambda **kwargs: [
        Evidence("Title", "https://example.com", "Passage", "query", "web"),
    ])
    monkeypatch.setattr(cli, "generate_report_body", lambda **kwargs: "Uncited report")
    output = tmp_path / "existing.md"
    output.write_text("Previous valid report", encoding="utf-8")
    with pytest.raises(RuntimeError, match="No report was written"):
        cli.main(["Topic", "--output", str(output)])
    assert output.read_text(encoding="utf-8") == "Previous valid report"
