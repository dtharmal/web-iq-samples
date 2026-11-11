# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from unittest.mock import MagicMock

import httpx
import pytest
from azure.core.exceptions import ClientAuthenticationError

import webiq_scopes as sample

SITES = {"includeSites": ["example.com/docs"], "excludeSites": ["example.com/docs/archive"]}
ETAG = '"opaque-version-1"'
COLLECTION = "/v1/customer/billing-accounts/demo-account/scopes"


def test_create_get_replace_and_delete_use_exact_contract() -> None:
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                201, json={"name": "ProductDocs", **SITES},
                headers={"ETag": ETAG, "Location": COLLECTION + "/ProductDocs"},
            )
        if request.method == "GET":
            return httpx.Response(200, json={"name": "ProductDocs", **SITES}, headers={"ETag": ETAG})
        if request.method == "PUT":
            return httpx.Response(200, json=SITES, headers={"ETag": '"opaque-version-2"'})
        return httpx.Response(204, content=b"not-json")

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = sample.ScopesClient(http, "PRIVATE-TOKEN", "demo-account")
        assert client.create_scope("ProductDocs", SITES) == ETAG
        assert client.get_scope("ProductDocs") == (SITES, ETAG)
        assert client.replace_scope("ProductDocs", SITES, ETAG) == '"opaque-version-2"'
        client.delete_scope("ProductDocs", '"opaque-version-2"')

    assert [request.method for request in requests] == ["POST", "GET", "PUT", "DELETE"]
    assert [request.url.path for request in requests] == [
        COLLECTION, *[COLLECTION + "/ProductDocs"] * 3,
    ]
    assert json.loads(requests[0].content) == {"name": "ProductDocs", **SITES}
    assert "If-Match" not in requests[0].headers
    assert json.loads(requests[2].content) == SITES
    assert requests[2].headers["If-Match"] == ETAG
    assert requests[3].headers["If-Match"] == '"opaque-version-2"'
    assert requests[3].content == b""
    for request in requests:
        assert str(request.url).startswith("https://api.microsoft.ai/v1/customer/")
        assert request.headers["Authorization"] == "Bearer PRIVATE-TOKEN"
        assert "x-apikey" not in request.headers


def test_discovery_uses_canonical_route_and_empty_array_is_not_failure() -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        assert sample.discover(http, "PRIVATE-TOKEN") == []
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.microsoft.ai/v1/customer/authorized-resources"


def test_listing_follows_opaque_continuation_without_changing_token() -> None:
    token = "opaque+/= ?&page=2"
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={
                "items": [{"name": "One", "includeSiteCount": 1}],
                "nextContinuationToken": token,
            })
        return httpx.Response(200, json={"items": [{"name": "Two"}], "nextContinuationToken": None})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        items = sample.ScopesClient(http, "token", "demo-account").list_scopes(100)
    assert [item["name"] for item in items] == ["One", "Two"]
    assert dict(requests[0].url.params) == {"pageSize": "100"}
    assert dict(requests[1].url.params) == {"pageSize": "100", "continuationToken": token}


@pytest.mark.parametrize("page_size", [0, 101])
def test_invalid_page_size_does_not_send_request(page_size) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No request expected"))) as http:
        with pytest.raises(sample.SampleError, match="between 1 and 100"):
            sample.ScopesClient(http, "token", "demo-account").list_scopes(page_size)


def test_repeated_continuation_is_an_explicit_failure() -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [], "nextContinuationToken": "same"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(sample.SampleError, match="repeated continuation"):
            sample.ScopesClient(http, "token", "demo-account").list_scopes()
    assert len(requests) == 2


@pytest.mark.parametrize("billing_id", [".", "..", "account/with slash"])
def test_billing_identifier_cannot_change_url_structure(billing_id) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        sample.ScopesClient(http, "token", billing_id).list_scopes()
    segment = sample.path_segment(billing_id)
    assert requests[0].url.raw_path.startswith(
        f"/v1/customer/billing-accounts/{segment}/scopes?".encode(),
    )


@pytest.mark.parametrize("etag", [None, "", "*", 'W/"weak"', "unquoted", '"a"\r\nInjected: true'])
@pytest.mark.parametrize("command", ["replace", "delete"])
def test_writes_require_exact_strong_etag_before_sending(etag, command) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No request expected"))) as http:
        client = sample.ScopesClient(http, "token", "demo-account")
        with pytest.raises(sample.SampleError, match="strong, quoted ETag"):
            if command == "replace":
                client.replace_scope("ProductDocs", SITES, etag)
            else:
                client.delete_scope("ProductDocs", etag)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 412, 428, 429, 500, 503, 302])
def test_service_errors_are_sanitized_and_writes_never_retry(status, capsys) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status, json={"userMessage": "PRIVATE-SERVICE-BODY", "token": "PRIVATE-TOKEN"},
            headers={"Location": "https://example.com/do-not-follow"},
        )

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False) as http:
        with pytest.raises(sample.SampleError, match=f"HTTP {status}") as caught:
            sample.ScopesClient(http, "PRIVATE-TOKEN", "demo-account").replace_scope(
                "ProductDocs", SITES, ETAG,
            )
    assert len(requests) == 1
    assert "PRIVATE" not in str(caught.value)
    assert capsys.readouterr() == ("", "")
    if status == 412:
        assert "reconcile" in str(caught.value)
    if status >= 500 or status == 302:
        assert "outcome may be unknown" in str(caught.value)


def test_uncertain_write_connection_failure_does_not_retry_or_log_details() -> None:
    requests = []

    def fail(request):
        requests.append(request)
        raise httpx.ReadTimeout("PRIVATE-CONNECTION-DETAIL", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as http:
        with pytest.raises(sample.SampleError, match="read and reconcile") as caught:
            sample.ScopesClient(http, "PRIVATE-TOKEN", "demo-account").create_scope("ProductDocs", SITES)
    assert len(requests) == 1
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("value", [
    {}, {"includeSites": []}, {"includeSites": None, "excludeSites": []},
    {"includeSites": [], "excludeSites": None},
    {"includeSites": [None], "excludeSites": []},
    {"includeSites": ["   "], "excludeSites": []},
    {"includeSites": [], "excludeSites": [], "name": "Unexpected"},
])
def test_definition_requires_both_arrays_and_rejects_omitted_or_null(value) -> None:
    with pytest.raises(sample.SampleError):
        sample.definition(value)


def test_explicit_empty_arrays_are_preserved() -> None:
    assert sample.definition({"includeSites": [], "excludeSites": []}) == {
        "includeSites": [], "excludeSites": [],
    }


@pytest.mark.parametrize("name", ["", "a" * 129, "with space", "slash/name", "non-ascii-\u00e9"])
def test_invalid_scope_names_fail_before_network(name) -> None:
    with pytest.raises(sample.SampleError, match="1-128 ASCII"):
        sample.scope_name(name)


@pytest.mark.parametrize("name", ["a", "a" * 128, "Product.Docs_1-2", ".", ".."])
def test_scope_name_limits_and_path_encoding(name) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=SITES, headers={"ETag": ETAG})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        sample.ScopesClient(http, "token", "demo-account").get_scope(name)
    expected = name.replace(".", "%2E") if name in {".", ".."} else name
    assert requests[0].url.raw_path == (COLLECTION + "/" + expected).encode()


@pytest.mark.parametrize("auth", ["api-key", "entra"])
def test_search_uses_one_scope_string_and_fail_closed_request(auth) -> None:
    requests = []
    headers = {"x-apikey": "PRIVATE-KEY"} if auth == "api-key" else {
        "Authorization": "Bearer PRIVATE-TOKEN",
    }

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "webResults": [{"title": "PRIVATE-TITLE", "content": "PRIVATE-CONTENT"}],
            "traceId": "demo-trace",
            "scopeApplied": True,
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        assert sample.search(
            http, "ProductDocs", "Microsoft Entra app authentication", headers=headers,
            app_id="demo-webiq-appid" if auth == "entra" else None,
        ) == sample.SearchOutcome(1, True)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v3/search/web"
    assert request.url.host == "api.microsoft.ai"
    assert json.loads(request.content) == {
        "query": "Microsoft Entra app authentication",
        "scopeName": "ProductDocs", "failOnMissingScope": True,
        "maxResults": 5, "language": "en", "region": "US",
        "contentFormat": "passage", "maxLength": 3000,
    }
    if auth == "api-key":
        assert dict(request.url.params) == {}
        assert "Authorization" not in request.headers
        assert request.headers["x-apikey"] == "PRIVATE-KEY"
    else:
        assert dict(request.url.params) == {"APPID": "demo-webiq-appid"}
        assert "x-apikey" not in request.headers


def test_missing_scope_fails_without_unscoped_fallback() -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(400, json={"errorCode": "HandlerInvalidInput", "userMessage": "PRIVATE"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(sample.SampleError, match="Search never falls back"):
            sample.search(http, "Missing", "query", headers={"x-apikey": "key"})
    assert len(requests) == 1
    assert json.loads(requests[0].content)["failOnMissingScope"] is True


@pytest.mark.parametrize("headers,app_id", [
    ({"x-apikey": "key"}, "demo-appid"), ({"Authorization": "Bearer token"}, " "),
])
def test_invalid_appid_configuration_does_not_send(headers, app_id) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No request expected"))) as http:
        with pytest.raises(sample.SampleError, match="APPID"):
            sample.search(http, "ProductDocs", "query", headers=headers, app_id=app_id)


@pytest.mark.parametrize("payload", [b"invalid-json", b"[]"])
def test_invalid_service_json_is_not_success(payload) -> None:
    with httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=payload),
    )) as http:
        with pytest.raises(sample.SampleError):
            sample.search(http, "ProductDocs", "query", headers={"x-apikey": "key"})


def install_fake_auth_and_http(monkeypatch, respond):
    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value.token = "PRIVATE-TOKEN"
    factory = MagicMock(return_value=credential)
    monkeypatch.setattr(sample, "certificate_credential", factory)
    real_client = httpx.Client
    monkeypatch.setattr(
        sample.httpx, "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    return factory, credential


def test_cli_discovery_does_not_select_account_or_interpret_legacy_canwrite(monkeypatch, capsys) -> None:
    customers = [{
        "customerId": "demo-customer", "permissions": {"canWrite": False},
        "billingAccounts": [{"billingAccountId": "demo-account", "permissions": {"canWrite": False}}],
    }]
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=customers)

    _, credential = install_fake_auth_and_http(monkeypatch, respond)
    monkeypatch.delenv("WEBIQ_BILLING_ACCOUNT_ID", raising=False)
    assert sample.main(["discover"]) == 0
    assert json.loads(capsys.readouterr().out) == customers
    assert len(requests) == 1
    credential.get_token.assert_called_once_with("https://api.microsoft.ai/.default")
    credential.__exit__.assert_called_once()


def test_cli_management_requires_explicit_account_before_auth(monkeypatch) -> None:
    factory = MagicMock()
    monkeypatch.setattr(sample, "certificate_credential", factory)
    monkeypatch.delenv("WEBIQ_BILLING_ACCOUNT_ID", raising=False)
    with pytest.raises(sample.SampleError, match="WEBIQ_BILLING_ACCOUNT_ID"):
        sample.main(["list"])
    factory.assert_not_called()


def test_cli_search_api_key_does_not_require_management_or_certificate(monkeypatch, capsys) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"webResults": [{"content": "PRIVATE-CONTENT"}]})

    factory, _ = install_fake_auth_and_http(monkeypatch, respond)
    monkeypatch.setenv("WEBIQ_API_KEY", "PRIVATE-KEY")
    monkeypatch.delenv("WEBIQ_BILLING_ACCOUNT_ID", raising=False)
    assert sample.main(["search", "ProductDocs", "PRIVATE-QUERY"]) == 0
    factory.assert_not_called()
    assert len(requests) == 1
    captured = capsys.readouterr()
    assert "1 web results" in captured.out
    assert "the service did not report whether the scope was applied" in captured.out
    assert "Scoped search succeeded" not in captured.out
    assert "PRIVATE" not in captured.out + captured.err


def test_cli_entra_search_selects_webiq_profile_and_requests_correct_token(monkeypatch, capsys) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"webResults": []})

    _, credential = install_fake_auth_and_http(monkeypatch, respond)
    monkeypatch.setenv("WEBIQ_API_KEY", "IGNORED-KEY")
    assert sample.main([
        "search", "ProductDocs", "query", "--auth", "entra", "--app-id", "demo-webiq-appid",
    ]) == 0
    credential.get_token.assert_called_once_with(sample.TOKEN_SCOPE)
    assert dict(requests[0].url.params) == {"APPID": "demo-webiq-appid"}
    assert "x-apikey" not in requests[0].headers
    assert "IGNORED-KEY" not in capsys.readouterr().out


@pytest.mark.parametrize("outcome", [True, None, "omitted"])
def test_cli_reports_scope_application_without_inference(monkeypatch, capsys, outcome) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        body = {"webResults": [{"content": "PRIVATE-CONTENT"}]}
        if outcome != "omitted":
            body["scopeApplied"] = outcome
        return httpx.Response(200, json=body)

    install_fake_auth_and_http(monkeypatch, respond)
    monkeypatch.setenv("WEBIQ_API_KEY", "PRIVATE-KEY")
    assert sample.main(["search", "ProductDocs", "PRIVATE-QUERY"]) == 0
    output = capsys.readouterr()
    expected = (
        "scopeApplied: true" if outcome is True
        else "the service did not report whether the scope was applied"
    )
    assert expected in output.out
    assert "PRIVATE" not in output.out + output.err
    assert "Scoped search succeeded" not in output.out
    assert len(requests) == 1


def test_scope_not_applied_exits_nonzero_without_retry_or_content(monkeypatch, capsys) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "webResults": [{"content": "PRIVATE-CONTENT"}], "scopeApplied": False,
        })

    install_fake_auth_and_http(monkeypatch, respond)
    monkeypatch.setenv("WEBIQ_API_KEY", "PRIVATE-KEY")
    monkeypatch.setattr("sys.argv", ["webiq-scopes", "search", "ProductDocs", "PRIVATE-QUERY"])
    with pytest.raises(SystemExit) as caught:
        sample.entrypoint()
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "scopeApplied: false" in output.err
    assert "PRIVATE" not in output.err
    assert len(requests) == 1
    assert json.loads(requests[0].content)["failOnMissingScope"] is True


@pytest.mark.parametrize("outcome", ["true", "false", 0, 1, [], {}])
def test_invalid_scope_application_type_is_an_explicit_failure(outcome) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"webResults": [], "scopeApplied": outcome},
    ))) as http:
        with pytest.raises(sample.SampleError, match="invalid scopeApplied"):
            sample.search(http, "ProductDocs", "query", headers={"x-apikey": "key"})


def test_cli_empty_includes_need_deliberate_acknowledgement(monkeypatch, tmp_path, capsys) -> None:
    path = tmp_path / "empty.json"
    path.write_text('{"includeSites":[],"excludeSites":[]}', encoding="utf-8")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(201, json={}, headers={"ETag": ETAG})

    factory, _ = install_fake_auth_and_http(monkeypatch, respond)
    monkeypatch.setenv("WEBIQ_BILLING_ACCOUNT_ID", "demo-account")
    args = ["create", "AllExceptExcluded", "--definition", str(path)]
    with pytest.raises(sample.SampleError, match="--allow-all-sites"):
        sample.main(args)
    factory.assert_not_called()
    assert sample.main([*args, "--allow-all-sites"]) == 0
    assert json.loads(requests[0].content) == {
        "name": "AllExceptExcluded", "includeSites": [], "excludeSites": [],
    }
    assert "not a readiness signal" in capsys.readouterr().out


def test_cli_management_lifecycle_preserves_quoted_etags(monkeypatch, tmp_path, capsys) -> None:
    path = tmp_path / "scope.json"
    path.write_text(json.dumps(SITES), encoding="utf-8")
    requests = []
    responses = [
        httpx.Response(201, json=SITES, headers={"ETag": ETAG}),
        httpx.Response(200, json=SITES, headers={"ETag": ETAG}),
        httpx.Response(200, json=SITES, headers={"ETag": '"new-version"'}),
        httpx.Response(204),
    ]

    def respond(request):
        requests.append(request)
        return responses.pop(0)

    install_fake_auth_and_http(monkeypatch, respond)
    monkeypatch.setenv("WEBIQ_BILLING_ACCOUNT_ID", "demo-account")
    assert sample.main(["create", "ProductDocs", "--definition", str(path)]) == 0
    capsys.readouterr()
    assert sample.main(["get", "ProductDocs"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot == {"name": "ProductDocs", **SITES, "etag": ETAG}
    assert sample.main([
        "replace", "ProductDocs", "--definition", str(path), "--if-match", snapshot["etag"],
    ]) == 0
    assert sample.main(["delete", "ProductDocs", "--if-match", '"new-version"']) == 0
    assert requests[2].headers["If-Match"] == ETAG
    assert requests[3].headers["If-Match"] == '"new-version"'
    assert not responses
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err


@pytest.mark.parametrize("etag", [None, 'W/"weak"'])
def test_missing_or_weak_response_etag_is_not_usable_for_a_write(etag) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(201, json=SITES, headers={"ETag": etag} if etag else {})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(sample.SampleError, match="Read the scope before writing"):
            sample.ScopesClient(http, "token", "demo-account").create_scope("ProductDocs", SITES)
    assert len(requests) == 1


def test_invalid_definition_file_does_not_log_file_contents(tmp_path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text("PRIVATE-INVALID-JSON", encoding="utf-8")
    with pytest.raises(sample.SampleError, match="Cannot read") as caught:
        sample.load_definition(path)
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("variable", [
    "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_CERTIFICATE_PATH",
])
@pytest.mark.parametrize("value", [None, " ", "<placeholder>"])
def test_certificate_settings_required_before_loading(monkeypatch, variable, value) -> None:
    for setting in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_CERTIFICATE_PATH"):
        monkeypatch.setenv(setting, "configured")
    if value is None:
        monkeypatch.delenv(variable, raising=False)
    else:
        monkeypatch.setenv(variable, value)
    factory = MagicMock()
    monkeypatch.setattr(sample, "CertificateCredential", factory)
    with pytest.raises(sample.SampleError, match=variable):
        sample.certificate_credential()
    factory.assert_not_called()


def test_certificate_uses_explicit_settings_without_credential_chain(monkeypatch) -> None:
    for name, value in {
        "AZURE_TENANT_ID": "demo-tenant", "AZURE_CLIENT_ID": "demo-client",
        "AZURE_CLIENT_CERTIFICATE_PATH": "demo.pfx",
        "AZURE_CLIENT_CERTIFICATE_PASSWORD": "PRIVATE-PASSWORD",
        "AZURE_CLIENT_SECRET": "IGNORED-SECRET",
    }.items():
        monkeypatch.setenv(name, value)
    factory = MagicMock()
    monkeypatch.setattr(sample, "CertificateCredential", factory)
    assert sample.certificate_credential() is factory.return_value
    factory.assert_called_once_with(
        tenant_id="demo-tenant", client_id="demo-client", certificate_path="demo.pfx",
        password="PRIVATE-PASSWORD",
    )


@pytest.mark.parametrize("error", [
    sample.SampleError("HTTP 412. Read and reconcile."),
    ClientAuthenticationError(message="PRIVATE-AUTH-DETAIL"),
])
def test_entrypoint_has_nonzero_exit_and_sanitized_diagnostic(monkeypatch, capsys, error) -> None:
    monkeypatch.setattr(sample, "main", MagicMock(side_effect=error))
    with pytest.raises(SystemExit) as caught:
        sample.entrypoint()
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Error: ")
    assert "PRIVATE" not in captured.err
