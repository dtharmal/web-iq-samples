// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

use clap::Parser;
use reqwest::{Request, header::IF_MATCH};
use serde_json::{Value, json};
use std::{cell::RefCell, collections::VecDeque, path::PathBuf};
use webiq_scopes::{
    Arguments, AuthSettings, Authentication, Command, Definition, ScopesClient, SearchOutcome,
    TOKEN_SCOPE, Transport, TransportError, WireResponse, execute, required, scope_name,
    strong_etag, validate_command,
};

const ACCOUNT: &str = "demo-account";
const COLLECTION: &str = "/v1/customer/billing-accounts/demo-account/scopes";
const ETAG: &str = "\"opaque-version\"";

#[derive(Default)]
struct Mock {
    requests: RefCell<Vec<Request>>,
    responses: RefCell<VecDeque<WireResponse>>,
}

impl Mock {
    fn new(responses: Vec<WireResponse>) -> Self {
        Self {
            responses: RefCell::new(responses.into()),
            ..Default::default()
        }
    }
}

impl Transport for &Mock {
    async fn send(&self, request: Request) -> Result<WireResponse, TransportError> {
        self.requests.borrow_mut().push(request);
        self.responses
            .borrow_mut()
            .pop_front()
            .ok_or(TransportError)
    }
}

fn response(status: u16, body: Value, etag: Option<&str>) -> WireResponse {
    WireResponse {
        status,
        etag: etag.map(str::to_owned),
        body: serde_json::to_vec(&body).unwrap(),
    }
}

fn definition() -> Definition {
    Definition {
        include_sites: vec!["example.com/docs".to_owned()],
        exclude_sites: vec!["example.com/docs/archive".to_owned()],
    }
}

fn body(request: &Request) -> Value {
    serde_json::from_slice(request.body().unwrap().as_bytes().unwrap()).unwrap()
}

#[tokio::test]
async fn management_lifecycle_preserves_canonical_contract() {
    let sites = definition();
    let mock = Mock::new(vec![
        response(201, json!({}), Some(ETAG)),
        response(200, serde_json::to_value(&sites).unwrap(), Some(ETAG)),
        response(200, json!({}), Some("\"new-version\"")),
        WireResponse {
            status: 204,
            etag: None,
            body: b"not-json".to_vec(),
        },
    ]);
    let client = ScopesClient::new(&mock, Authentication::Bearer("PRIVATE-TOKEN".to_owned()));
    assert_eq!(
        client.create(ACCOUNT, "ProductDocs", &sites).await.unwrap(),
        ETAG
    );
    assert_eq!(
        client.get(ACCOUNT, "ProductDocs").await.unwrap(),
        (sites.clone(), ETAG.to_owned())
    );
    assert_eq!(
        client
            .replace(ACCOUNT, "ProductDocs", &sites, ETAG)
            .await
            .unwrap(),
        "\"new-version\""
    );
    client
        .delete(ACCOUNT, "ProductDocs", "\"new-version\"")
        .await
        .unwrap();
    let requests = mock.requests.borrow();
    assert_eq!(
        requests
            .iter()
            .map(|r| r.method().as_str())
            .collect::<Vec<_>>(),
        ["POST", "GET", "PUT", "DELETE"]
    );
    assert_eq!(requests[0].url().path(), COLLECTION);
    for request in &requests[1..] {
        assert_eq!(request.url().path(), format!("{COLLECTION}/ProductDocs"));
    }
    for request in requests.iter() {
        assert_eq!(request.url().scheme(), "https");
        assert_eq!(request.url().host_str(), Some("api.microsoft.ai"));
        assert_eq!(request.headers()["authorization"], "Bearer PRIVATE-TOKEN");
        assert!(!request.headers().contains_key("x-apikey"));
    }
    assert_eq!(
        body(&requests[0]),
        json!({
            "name": "ProductDocs", "includeSites": sites.include_sites, "excludeSites": sites.exclude_sites,
        })
    );
    assert_eq!(body(&requests[2]), serde_json::to_value(sites).unwrap());
    assert_eq!(requests[2].headers()[IF_MATCH], ETAG);
    assert_eq!(requests[3].headers()[IF_MATCH], "\"new-version\"");
    assert!(requests[3].body().is_none());
}

#[tokio::test]
async fn discovery_never_auto_selects_or_treats_legacy_permissions_as_scope_authority() {
    let customers = json!([{
        "customerId": "demo-customer", "permissions": {"canWrite": false},
        "billingAccounts": [{"billingAccountId": ACCOUNT, "permissions": {"canWrite": false}}],
    }]);
    let mock = Mock::new(vec![
        response(200, customers.clone(), None),
        response(200, json!([]), None),
    ]);
    let client = ScopesClient::new(&mock, Authentication::Bearer("token".to_owned()));
    let output = execute(&client, &Command::Discover, None, None)
        .await
        .unwrap();
    assert_eq!(serde_json::from_str::<Value>(&output).unwrap(), customers);
    let empty = execute(&client, &Command::Discover, None, None)
        .await
        .unwrap();
    assert!(empty.contains("No authorized resources"));
    let requests = mock.requests.borrow();
    assert_eq!(requests.len(), 2);
    assert_eq!(
        requests[0].url().as_str(),
        "https://api.microsoft.ai/v1/customer/authorized-resources"
    );
}

#[tokio::test]
async fn pagination_round_trips_opaque_tokens_and_metadata() {
    let token = "opaque+/= ?&page=2";
    let mock = Mock::new(vec![
        response(
            200,
            json!({"items": [{"name": "One"}], "nextContinuationToken": token}),
            None,
        ),
        response(
            200,
            json!({"items": [{"name": "Two"}], "nextContinuationToken": null}),
            None,
        ),
    ]);
    let client = ScopesClient::new(&mock, Authentication::Bearer("token".to_owned()));
    assert_eq!(
        client.list(ACCOUNT, 100).await.unwrap(),
        vec![json!({"name":"One"}), json!({"name":"Two"})]
    );
    let requests = mock.requests.borrow();
    let params = requests[1].url().query_pairs().collect::<Vec<_>>();
    assert_eq!(
        params,
        [
            ("pageSize".into(), "100".into()),
            ("continuationToken".into(), token.into())
        ]
    );
}

#[tokio::test]
async fn pagination_rejects_repeated_empty_or_invalid_tokens() {
    for token in [json!("same"), json!(""), json!(42)] {
        let page = json!({"items": [], "nextContinuationToken": token});
        let mock = Mock::new(vec![
            response(200, page.clone(), None),
            response(200, page, None),
        ]);
        let client = ScopesClient::new(&mock, Authentication::Bearer("token".to_owned()));
        assert!(
            client
                .list(ACCOUNT, 50)
                .await
                .unwrap_err()
                .to_string()
                .contains("continuation token")
        );
        assert!(mock.requests.borrow().len() <= 2);
    }
}

#[tokio::test]
async fn malformed_pages_and_invalid_page_sizes_do_not_succeed() {
    let mock = Mock::new(vec![response(200, json!({"items": [null]}), None)]);
    let client = ScopesClient::new(&mock, Authentication::Bearer("token".to_owned()));
    for size in [0, 101] {
        assert!(client.list(ACCOUNT, size).await.is_err());
    }
    assert!(mock.requests.borrow().is_empty());
    assert!(client.list(ACCOUNT, 50).await.is_err());
}

#[tokio::test]
async fn conditional_writes_require_exact_strong_etags_before_network() {
    let mock = Mock::default();
    let client = ScopesClient::new(&mock, Authentication::Bearer("token".to_owned()));
    for tag in ["", "*", "W/\"weak\"", "unquoted", "\"a\"\r\nInjected: true"] {
        assert!(
            client
                .replace(ACCOUNT, "ProductDocs", &definition(), tag)
                .await
                .is_err()
        );
        assert!(client.delete(ACCOUNT, "ProductDocs", tag).await.is_err());
    }
    assert!(mock.requests.borrow().is_empty());
    assert_eq!(strong_etag(Some(ETAG)).unwrap(), ETAG);
    assert!(strong_etag(None).is_err());
}

#[tokio::test]
async fn missing_or_weak_response_etag_is_not_usable() {
    for etag in [None, Some("W/\"weak\"")] {
        let mock = Mock::new(vec![response(201, json!({}), etag)]);
        let client = ScopesClient::new(&mock, Authentication::Bearer("token".to_owned()));
        assert!(
            client
                .create(ACCOUNT, "ProductDocs", &definition())
                .await
                .unwrap_err()
                .to_string()
                .contains("Read the scope")
        );
        assert_eq!(mock.requests.borrow().len(), 1);
    }
}

#[tokio::test]
async fn errors_are_sanitized_and_writes_never_retry() {
    for status in [400, 401, 403, 404, 408, 409, 412, 428, 429, 500, 503, 302] {
        let mock = Mock::new(vec![response(
            status,
            json!({"message":"PRIVATE-SERVER-BODY"}),
            None,
        )]);
        let client = ScopesClient::new(&mock, Authentication::Bearer("PRIVATE-TOKEN".to_owned()));
        let error = client
            .replace(ACCOUNT, "ProductDocs", &definition(), ETAG)
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains(&format!("HTTP {status}")));
        assert!(!error.contains("PRIVATE"));
        assert_eq!(mock.requests.borrow().len(), 1);
        if status == 412 {
            assert!(error.contains("reconcile"));
        }
        if status == 408 || status >= 500 || status == 302 {
            assert!(error.contains("outcome may be unknown"));
        }
    }
}

#[tokio::test]
async fn uncertain_network_failure_requires_reconciliation_without_retry() {
    let mock = Mock::default();
    let client = ScopesClient::new(&mock, Authentication::Bearer("PRIVATE-TOKEN".to_owned()));
    let error = client
        .create(ACCOUNT, "ProductDocs", &definition())
        .await
        .unwrap_err()
        .to_string();
    assert!(error.contains("read and reconcile"));
    assert!(!error.contains("PRIVATE"));
    assert_eq!(mock.requests.borrow().len(), 1);
}

#[test]
fn definition_requires_exactly_both_arrays_and_preserves_explicit_empty_arrays() {
    for value in [
        json!({}),
        json!({"includeSites":[]}),
        json!({"includeSites":null,"excludeSites":[]}),
        json!({"includeSites":[],"excludeSites":null}),
        json!({"includeSites":[null],"excludeSites":[]}),
        json!({"includeSites":[" "],"excludeSites":[]}),
        json!({"includeSites":[],"excludeSites":[],"name":"extra"}),
    ] {
        assert!(Definition::parse(value).is_err());
    }
    let empty = Definition::parse(json!({"includeSites":[],"excludeSites":[]})).unwrap();
    assert!(empty.include_sites.is_empty() && empty.exclude_sites.is_empty());
}

#[test]
fn names_enforce_ascii_and_length_boundaries() {
    for name in ["", "with space", "slash/name", "non-ascii-\u{e9}"] {
        assert!(scope_name(name).is_err());
    }
    assert!(scope_name(&"a".repeat(129)).is_err());
    assert!(scope_name(&"a".repeat(128)).is_ok());
    assert!(scope_name("Product.Docs_1-2").is_ok());
}

#[tokio::test]
async fn management_paths_encode_segments_and_reject_dot_normalization() {
    let mock = Mock::new(vec![response(200, json!({"items":[]}), None)]);
    let client = ScopesClient::new(&mock, Authentication::Bearer("token".to_owned()));
    for name in [".", ".."] {
        assert!(client.create(ACCOUNT, name, &definition()).await.is_err());
        assert!(client.get(ACCOUNT, name).await.is_err());
        assert!(client.list(name, 50).await.is_err());
    }
    assert!(mock.requests.borrow().is_empty());
    client.list("account/with slash", 50).await.unwrap();
    assert_eq!(
        mock.requests.borrow()[0].url().path(),
        "/v1/customer/billing-accounts/account%2Fwith%20slash/scopes"
    );
}

#[tokio::test]
async fn both_search_auth_modes_use_one_scope_string_and_fail_closed_body() {
    for api_key in [true, false] {
        let mock = Mock::new(vec![response(
            200,
            json!({"webResults":[{"content":"PRIVATE-CONTENT"}]}),
            None,
        )]);
        let auth = if api_key {
            Authentication::ApiKey("PRIVATE-KEY".to_owned())
        } else {
            Authentication::Bearer("PRIVATE-TOKEN".to_owned())
        };
        let client = ScopesClient::new(&mock, auth);
        let app_id = if api_key {
            None
        } else {
            Some("demo-webiq-appid")
        };
        assert_eq!(
            client
                .search("ProductDocs", "PRIVATE-QUERY", app_id)
                .await
                .unwrap(),
            SearchOutcome {
                result_count: 1,
                scope_applied: None
            }
        );
        let requests = mock.requests.borrow();
        assert_eq!(requests.len(), 1);
        let request = &requests[0];
        assert_eq!(request.method(), "POST");
        assert_eq!(request.url().path(), "/v3/search/web");
        assert_eq!(request.url().host_str(), Some("api.microsoft.ai"));
        assert_eq!(
            request.url().query(),
            if api_key {
                None
            } else {
                Some("APPID=demo-webiq-appid")
            }
        );
        assert_eq!(request.headers().contains_key("x-apikey"), api_key);
        assert_eq!(request.headers().contains_key("authorization"), !api_key);
        assert_eq!(
            body(request),
            json!({
                "query":"PRIVATE-QUERY","scopeName":"ProductDocs","failOnMissingScope":true,"maxResults":5,
                "language":"en","region":"US","contentFormat":"passage","maxLength":3000,
            })
        );
    }
}

#[tokio::test]
async fn missing_scope_fails_without_fallback_and_invalid_appid_never_sends() {
    let mock = Mock::new(vec![response(
        400,
        json!({"errorCode":"HandlerInvalidInput"}),
        None,
    )]);
    let client = ScopesClient::new(&mock, Authentication::ApiKey("key".to_owned()));
    assert!(
        client
            .search("ProductDocs", "query", Some("app"))
            .await
            .is_err()
    );
    assert!(client.search("ProductDocs", " ", None).await.is_err());
    assert!(client.discover().await.is_err());
    assert!(mock.requests.borrow().is_empty());
    let error = client
        .search("Missing", "query", None)
        .await
        .unwrap_err()
        .to_string();
    assert!(error.contains("never falls back"));
    assert_eq!(mock.requests.borrow().len(), 1);
    assert_eq!(body(&mock.requests.borrow()[0])["failOnMissingScope"], true);
}

#[tokio::test]
async fn malformed_search_and_discovery_json_are_explicit_errors() {
    for bytes in [
        b"invalid-json".to_vec(),
        b"[]".to_vec(),
        br#"{"webResults":null}"#.to_vec(),
    ] {
        let mock = Mock::new(vec![WireResponse {
            status: 200,
            etag: None,
            body: bytes,
        }]);
        let client = ScopesClient::new(&mock, Authentication::ApiKey("key".to_owned()));
        assert!(client.search("ProductDocs", "query", None).await.is_err());
    }
    let mock = Mock::new(vec![response(200, json!({}), None)]);
    let client = ScopesClient::new(&mock, Authentication::Bearer("token".to_owned()));
    assert!(client.discover().await.is_err());
}

#[test]
fn api_key_search_requires_neither_management_settings_nor_certificate() {
    let args =
        Arguments::try_parse_from(["webiq-scopes", "search", "ProductDocs", "query"]).unwrap();
    let settings = AuthSettings::read(&args.command, &|key| {
        assert_eq!(key, "WEBIQ_API_KEY");
        Some("PRIVATE-KEY".to_owned())
    })
    .unwrap();
    assert!(matches!(settings, AuthSettings::ApiKey(_)));
    assert!(validate_command(&args.command).unwrap().is_none());
    assert_eq!(TOKEN_SCOPE, "https://api.microsoft.ai/.default");
}

#[cfg(feature = "certificate-auth")]
#[test]
fn certificate_auth_requires_explicit_settings_without_chain_fallback() {
    for missing in [
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_CERTIFICATE_PATH",
    ] {
        let error = AuthSettings::read(&Command::Discover, &|key| {
            if key == missing {
                None
            } else {
                Some("configured".to_owned())
            }
        })
        .err()
        .unwrap()
        .to_string();
        assert!(error.contains(missing));
    }
    for value in ["", " ", "<placeholder>"] {
        assert!(required(&|_| Some(value.to_owned()), "SETTING").is_err());
    }
}

#[test]
fn cli_validation_rejects_unsafe_options_without_auth_or_network() {
    assert!(
        Arguments::try_parse_from([
            "webiq-scopes",
            "search",
            "ProductDocs",
            "query",
            "--auth",
            "unknown"
        ])
        .is_err()
    );
    assert!(Arguments::try_parse_from(["webiq-scopes", "delete", "ProductDocs"]).is_err());
    assert!(Arguments::try_parse_from(["webiq-scopes", "list", "--unknown"]).is_err());
    for args in [
        vec![
            "webiq-scopes",
            "search",
            "ProductDocs",
            "query",
            "--app-id",
            "demo-appid",
        ],
        vec!["webiq-scopes", "list", "--page-size", "101"],
        vec!["webiq-scopes", "delete", "ProductDocs", "--if-match", "*"],
    ] {
        let args = Arguments::try_parse_from(args).unwrap();
        assert!(validate_command(&args.command).is_err());
    }
}

#[tokio::test]
async fn cli_outputs_preserve_etags_account_selection_and_search_privacy() {
    let mock = Mock::new(vec![
        response(200, serde_json::to_value(definition()).unwrap(), Some(ETAG)),
        response(
            200,
            json!({"webResults":[{"content":"PRIVATE-CONTENT"}]}),
            None,
        ),
    ]);
    let client = ScopesClient::new(&mock, Authentication::Bearer("PRIVATE-TOKEN".to_owned()));
    let command = Command::Get {
        name: "ProductDocs".to_owned(),
    };
    assert!(execute(&client, &command, None, None).await.is_err());
    assert!(mock.requests.borrow().is_empty());
    let output = execute(&client, &command, Some(ACCOUNT), None)
        .await
        .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&output).unwrap()["etag"],
        ETAG
    );
    let search = Command::Search {
        name: "ProductDocs".to_owned(),
        query: "PRIVATE-QUERY".to_owned(),
        auth: "entra".to_owned(),
        app_id: None,
    };
    let output = execute(&client, &search, None, None).await.unwrap();
    assert!(output.contains("1 web results") && !output.contains("PRIVATE"));
    assert!(output.contains("the service did not report whether the scope was applied"));
    assert!(!output.contains("Scoped search succeeded"));
}

#[test]
fn empty_includes_need_deliberate_cli_acknowledgment() {
    let file = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(file.path(), r#"{"includeSites":[],"excludeSites":[]}"#).unwrap();
    let command = Command::Create {
        name: "AllExceptExcluded".to_owned(),
        definition: file.path().into(),
        allow_all_sites: false,
    };
    assert!(
        validate_command(&command)
            .unwrap_err()
            .to_string()
            .contains("--allow-all-sites")
    );
    let command = Command::Create {
        name: "AllExceptExcluded".to_owned(),
        definition: file.path().into(),
        allow_all_sites: true,
    };
    assert!(
        validate_command(&command)
            .unwrap()
            .unwrap()
            .include_sites
            .is_empty()
    );
    std::fs::write(file.path(), "PRIVATE-INVALID-JSON").unwrap();
    assert!(
        !validate_command(&command)
            .unwrap_err()
            .to_string()
            .contains("PRIVATE")
    );
}

#[tokio::test]
async fn authoring_success_is_not_serving_readiness() {
    let mock = Mock::new(vec![response(201, json!({}), Some(ETAG))]);
    let client = ScopesClient::new(&mock, Authentication::Bearer("token".to_owned()));
    let command = Command::Create {
        name: "ProductDocs".to_owned(),
        definition: PathBuf::new(),
        allow_all_sites: false,
    };
    let output = execute(&client, &command, Some(ACCOUNT), Some(&definition()))
        .await
        .unwrap();
    assert!(output.contains("asynchronous") && output.contains("not a readiness signal"));
}

#[cfg(feature = "certificate-auth")]
#[tokio::test]
async fn invalid_certificate_file_is_reported_without_token_request_or_private_details() {
    let file = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(file.path(), b"PRIVATE-INVALID-PFX").unwrap();
    let settings = AuthSettings::Certificate {
        tenant: "demo-tenant".to_owned(),
        client: "demo-client".to_owned(),
        path: file.path().to_str().unwrap().to_owned(),
        password: Some("PRIVATE-PASSWORD".to_owned()),
    };
    let error = settings.authenticate().await.err().unwrap().to_string();
    assert!(error.contains("Cannot load certificate credentials"));
    assert!(!error.contains("PRIVATE"));
}

#[cfg(not(feature = "certificate-auth"))]
#[test]
fn certificate_commands_require_explicit_build_feature_before_reading_credentials() {
    let search = Command::Search {
        name: "ProductDocs".to_owned(),
        query: "query".to_owned(),
        auth: "entra".to_owned(),
        app_id: None,
    };
    for command in [Command::Discover, search] {
        let error = AuthSettings::read(&command, &|_| panic!("No credential reads expected"))
            .err()
            .unwrap()
            .to_string();
        assert!(error.contains("--features certificate-auth"));
    }
}

#[test]
fn required_settings_reject_empty_and_placeholder_values() {
    for value in ["", " ", "<placeholder>"] {
        assert!(required(&|_| Some(value.to_owned()), "SETTING").is_err());
    }
}

#[tokio::test]
async fn cli_reports_scope_application_without_inference() {
    for value in [Some(json!(true)), Some(Value::Null), None] {
        let mut body = json!({"webResults":[{"content":"PRIVATE-CONTENT"}]});
        if let Some(value) = &value {
            body["scopeApplied"] = value.clone();
        }
        let mock = Mock::new(vec![response(200, body, None)]);
        let client = ScopesClient::new(&mock, Authentication::ApiKey("PRIVATE-KEY".to_owned()));
        let command = Command::Search {
            name: "ProductDocs".to_owned(),
            query: "PRIVATE-QUERY".to_owned(),
            auth: "api-key".to_owned(),
            app_id: None,
        };
        let output = execute(&client, &command, None, None).await.unwrap();
        let expected = if value == Some(json!(true)) {
            "scopeApplied: true"
        } else {
            "the service did not report whether the scope was applied"
        };
        assert!(output.contains(expected));
        assert!(!output.contains("PRIVATE") && !output.contains("Scoped search succeeded"));
        assert_eq!(mock.requests.borrow().len(), 1);
    }
}

#[tokio::test]
async fn scope_not_applied_fails_without_retry_or_content() {
    let mock = Mock::new(vec![response(
        200,
        json!({
            "webResults":[{"content":"PRIVATE-CONTENT"}], "scopeApplied": false,
        }),
        None,
    )]);
    let client = ScopesClient::new(&mock, Authentication::ApiKey("PRIVATE-KEY".to_owned()));
    let command = Command::Search {
        name: "ProductDocs".to_owned(),
        query: "PRIVATE-QUERY".to_owned(),
        auth: "api-key".to_owned(),
        app_id: None,
    };
    let error = execute(&client, &command, None, None)
        .await
        .unwrap_err()
        .to_string();
    assert!(error.contains("scopeApplied: false"));
    assert!(!error.contains("PRIVATE"));
    assert_eq!(mock.requests.borrow().len(), 1);
    assert_eq!(body(&mock.requests.borrow()[0])["failOnMissingScope"], true);
}

#[tokio::test]
async fn invalid_scope_application_types_fail_explicitly() {
    for value in [
        json!("true"),
        json!("false"),
        json!(0),
        json!(1),
        json!([]),
        json!({}),
    ] {
        let mock = Mock::new(vec![response(
            200,
            json!({
                "webResults": [], "scopeApplied": value,
            }),
            None,
        )]);
        let client = ScopesClient::new(&mock, Authentication::ApiKey("key".to_owned()));
        let error = client
            .search("ProductDocs", "query", None)
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("invalid scopeApplied"));
        assert_eq!(mock.requests.borrow().len(), 1);
    }
}
