# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from webiq import WebIQClient
from webiq.errors import APIConnectionError, APIStatusError

from webiq_market_briefing import retrieval


@pytest.mark.parametrize(
    ("auth_mode", "password"),
    [("api-key", None), ("entra", None), ("entra", "PRIVATE-PFX-PASSWORD")],
)
def test_passage_search_and_authentication(monkeypatch, caplog, auth_mode, password) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, headers={"x-traceid": "trace-success"},
            json={"webResults": [{
                "title": "Title", "url": "https://example.com", "content": "Passage",
            }]} if request.url.path.endswith("/web") else {"newsResults": []},
        )

    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value = SimpleNamespace(token="test-token")
    credential_factory = MagicMock(return_value=credential)
    monkeypatch.setattr(retrieval, "CertificateCredential", credential_factory)
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-id")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AZURE_CLIENT_CERTIFICATE_PATH", "certificate.pfx")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "IGNORED-CLIENT-SECRET")
    if password is None:
        monkeypatch.delenv("AZURE_CLIENT_CERTIFICATE_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("AZURE_CLIENT_CERTIFICATE_PASSWORD", password)
    with httpx.Client(base_url="https://api.microsoft.ai/v3", transport=httpx.MockTransport(respond)) as http_client:
        monkeypatch.setattr(
            retrieval, "WebIQClient",
            lambda **kwargs: WebIQClient(http_client=http_client, **kwargs),
        )
        with caplog.at_level(logging.INFO, logger="webiq_market_briefing"):
            evidence = retrieval.retrieve_evidence(
                "test-key" if auth_mode == "api-key" else None,
                ["query"], 5, "US", auth_mode=auth_mode,
            )
    assert len(evidence) == 1
    assert len(requests) == 2
    for request in requests:
        body = json.loads(request.content)
        assert body["contentFormat"] == "passage"
        assert body["maxLength"] == 3000
        if auth_mode == "api-key":
            assert request.headers["x-apikey"] == "test-key"
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == "Bearer test-token"
            assert "x-apikey" not in request.headers
    assert "trace-success" in caplog.text
    assert "test-key" not in caplog.text
    assert "test-token" not in caplog.text
    assert "PRIVATE-PFX-PASSWORD" not in caplog.text
    assert "IGNORED-CLIENT-SECRET" not in caplog.text
    if auth_mode == "entra":
        credential_factory.assert_called_once_with(
            tenant_id="tenant-id", client_id="client-id",
            certificate_path="certificate.pfx", password=password,
        )
        credential.get_token.assert_called_with("https://api.microsoft.ai/.default")
        credential.__exit__.assert_called_once()
    else:
        credential_factory.assert_not_called()


@pytest.mark.parametrize(
    "variable", ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_CERTIFICATE_PATH"],
)
@pytest.mark.parametrize("value", [None, "   "])
def test_entra_requires_certificate_settings_before_creating_client(monkeypatch, variable, value) -> None:
    for name in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_CERTIFICATE_PATH"):
        monkeypatch.setenv(name, "configured")
    if value is None:
        monkeypatch.delenv(variable, raising=False)
    else:
        monkeypatch.setenv(variable, value)
    client = MagicMock()
    credential = MagicMock()
    monkeypatch.setattr(retrieval, "WebIQClient", client)
    monkeypatch.setattr(retrieval, "CertificateCredential", credential)

    with pytest.raises(ValueError, match=variable):
        retrieval.retrieve_evidence(None, ["query"], 5, "US", auth_mode="entra")

    credential.assert_not_called()
    client.assert_not_called()


@pytest.mark.parametrize("password", [None, "PRIVATE-PFX-PASSWORD"])
def test_entra_loads_real_pfx_and_rejects_wrong_password(monkeypatch, tmp_path, password) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "offline-test")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "test.pfx"
    encryption = (
        serialization.BestAvailableEncryption(password.encode())
        if password else serialization.NoEncryption()
    )
    path.write_bytes(pkcs12.serialize_key_and_certificates(
        b"offline-test", key, certificate, None, encryption,
    ))
    monkeypatch.setenv("AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("AZURE_CLIENT_ID", "00000000-0000-0000-0000-000000000002")
    monkeypatch.setenv("AZURE_CLIENT_CERTIFICATE_PATH", str(path))
    if password is None:
        monkeypatch.delenv("AZURE_CLIENT_CERTIFICATE_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("AZURE_CLIENT_CERTIFICATE_PASSWORD", password)

    with retrieval._certificate_credential() as credential:
        assert isinstance(credential, retrieval.CertificateCredential)

    monkeypatch.setenv("AZURE_CLIENT_CERTIFICATE_PASSWORD", "wrong-password")
    client = MagicMock()
    monkeypatch.setattr(retrieval, "WebIQClient", client)
    with pytest.raises(ValueError, match="certificate"):
        retrieval.retrieve_evidence(None, ["query"], 5, "US", auth_mode="entra")
    client.assert_not_called()


@pytest.mark.parametrize(
    ("statuses", "guidance", "expected_calls"),
    [([503, 200], None, 2), ([429, 200], "3s", 2),
     ([430, 200], None, 2), ([401], None, 1), ([403], None, 1),
     ([400], None, 1), ([503, 503, 503], None, 3),
     ([429], "120s", 1), ([503], "120s", 1)],
)
def test_bounded_retries_and_sanitized_diagnostics(
    monkeypatch, caplog, statuses, guidance, expected_calls,
) -> None:
    requests = []
    sleeps = []

    def respond(request):
        status = statuses[len(requests)]
        requests.append(request)
        return httpx.Response(status, headers={"x-traceid": f"trace-{len(requests)}"}, json={
            "errorCode": "TransientError",
            "requestId": "request-123",
            "userMessage": "DO-NOT-LOG-SECRET",
            "retryAfter": guidance,
        })

    monkeypatch.setattr(retrieval.time, "sleep", sleeps.append)
    monkeypatch.setattr(retrieval.random, "uniform", lambda *args: 1.0)
    with httpx.Client(base_url="https://api.microsoft.ai/v3", transport=httpx.MockTransport(respond)) as http_client:
        with WebIQClient(
            api_key="test-key", http_client=http_client,
            retry=retrieval.RetryPolicy(max_retries=0),
            telemetry_hook=retrieval.log_webiq_attempt,
        ) as client:
            call = lambda: client.web.search("private query")
            if statuses[-1] == 200:
                retrieval._search_with_retry(call)
            else:
                with pytest.raises(APIStatusError):
                    retrieval._search_with_retry(call)
    assert len(requests) == expected_calls
    assert len(sleeps) == expected_calls - 1
    if guidance == "3s":
        assert sleeps == [3.0]
    if statuses == [503, 503, 503]:
        assert sleeps == [1.0, 2.0]
    assert "trace-1" in caplog.text
    assert "request-123" in caplog.text
    assert "TransientError" in caplog.text
    assert "DO-NOT-LOG-SECRET" not in caplog.text
    assert "private query" not in caplog.text


def test_connection_failure_retries_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(retrieval.time, "sleep", lambda _: None)
    call = MagicMock(side_effect=[APIConnectionError("private detail"), "result"])
    assert retrieval._search_with_retry(call) == "result"
    assert call.call_count == 2


@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        (
            [[f"{group}-{rank}" for rank in range(5)] for group in range(6)],
            [f"{group}-{rank}" for rank in range(3) for group in range(6)],
        ),
        (
            [
                [],
                ["shared", "shared", "a", "b", "c"],
                ["shared", "d", "e", "f", "g"],
                ["h", "i", "j", "k", "l"],
                ["m", "n", "o", "p", "q"],
                ["r", "s", "t", "u", "v"],
            ],
            [
                "shared", "h", "m", "r", "d", "i", "n", "s", "a",
                "e", "j", "o", "t", "b", "f", "k", "p", "u",
            ],
        ),
        ([None, ["shared", "shared"], [], ["shared", "other"], [], None], ["shared", "other"]),
        ([None, [], None, [], None, []], []),
    ],
    ids=["all-groups-represented", "duplicates-and-empty-group", "fewer-than-cap", "all-empty"],
)
def test_round_robin_evidence_selection(monkeypatch, groups, expected) -> None:
    def results(group):
        return [
            SimpleNamespace(
                title=key, url=f"https://example.com/{key}", content="Passage",
                lastUpdatedAt=None, source=None,
            )
            for key in group
        ] if group is not None else None

    client = MagicMock()
    client.__enter__.return_value = client
    client.web.search.side_effect = [
        SimpleNamespace(webResults=results(group)) for group in groups[::2]
    ]
    client.news.search.side_effect = [
        SimpleNamespace(newsResults=results(group)) for group in groups[1::2]
    ]
    monkeypatch.setattr(retrieval, "WebIQClient", lambda **kwargs: client)

    evidence = retrieval.retrieve_evidence("test-key", ["q1", "q2", "q3"], 5, "US")

    assert [item.url for item in evidence] == [f"https://example.com/{key}" for key in expected]
    assert [call.args for call in client.web.search.call_args_list] == [("q1",), ("q2",), ("q3",)]
    assert [call.args for call in client.news.search.call_args_list] == [("q1",), ("q2",), ("q3",)]


def test_queries_and_all_received_urls_are_logged_as_single_line_json(monkeypatch, caplog) -> None:
    query = "energy \"outlook\"\r\nfor 2026"
    url = "https://example.com/report?q=energy&year=2026"
    skipped_url = "https://example.com/no-content\nforged-entry"
    result = SimpleNamespace(
        title="Title", url=url, content="PRIVATE-PASSAGE",
        lastUpdatedAt=None, source=None,
    )
    skipped = SimpleNamespace(
        title="Title", url=skipped_url, content=None, lastUpdatedAt=None,
    )
    client = MagicMock()
    client.__enter__.return_value = client
    client.web.search.return_value = SimpleNamespace(webResults=[result, result, skipped])
    client.news.search.return_value = SimpleNamespace(newsResults=[result])
    monkeypatch.setattr(retrieval, "WebIQClient", lambda **kwargs: client)

    with caplog.at_level(logging.INFO, logger="webiq_market_briefing"):
        evidence = retrieval.retrieve_evidence("PRIVATE-KEY", [query], 5, "US")

    assert len(evidence) == 1
    assert client.web.search.call_args.args == (query,)
    assert client.news.search.call_args.args == (query,)
    messages = [
        record.getMessage() for record in caplog.records
        if record.name == "webiq_market_briefing"
    ]
    for operation in ("_search_web", "_search_news"):
        assert f"service=webiq operation={operation} query={json.dumps(query)}" in messages
    assert sum(f"source_url={json.dumps(url)}" in message for message in messages) == 3
    assert any(f"source_url={json.dumps(skipped_url)}" in message for message in messages)
    assert all("\r" not in message and "\n" not in message for message in messages)
    assert "PRIVATE-KEY" not in caplog.text
    assert "PRIVATE-PASSAGE" not in caplog.text


@pytest.mark.parametrize("url,content", [("javascript:alert(1)", "data"), ("https://example.com", " ")])
def test_unusable_evidence_is_not_sent_to_model(url, content) -> None:
    assert retrieval._to_evidence(
        title="Title", url=url, content=content, query="query", result_type="web",
    ) is None
