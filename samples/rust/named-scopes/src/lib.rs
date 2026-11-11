// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

#[cfg(feature = "certificate-auth")]
use azure_core::credentials::TokenCredential;
#[cfg(feature = "certificate-auth")]
use azure_identity::{ClientCertificateCredential, ClientCertificateCredentialOptions};
use clap::{Parser, Subcommand};
use reqwest::{
    Method, Request, Url,
    header::{AUTHORIZATION, CONTENT_TYPE, HeaderValue, IF_MATCH},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{collections::HashSet, fmt, future::Future, path::PathBuf, time::Duration};

pub const BASE_URL: &str = "https://api.microsoft.ai";
pub const TOKEN_SCOPE: &str = "https://api.microsoft.ai/.default";
pub type Result<T> = std::result::Result<T, SampleError>;

#[derive(Debug)]
pub struct SampleError(pub String);

impl fmt::Display for SampleError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for SampleError {}

fn fail(message: &str) -> SampleError {
    SampleError(message.to_owned())
}

pub fn scope_name(name: &str) -> Result<&str> {
    if name.is_empty()
        || name.len() > 128
        || !name
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
    {
        return Err(fail(
            "Scope name requires 1-128 ASCII letters, digits, periods, underscores, or hyphens.",
        ));
    }
    Ok(name)
}

pub fn strong_etag(value: Option<&str>) -> Result<&str> {
    let tag =
        value.ok_or_else(|| fail("Read the scope and supply its exact strong quoted ETag."))?;
    if tag.len() < 2
        || !tag.starts_with('"')
        || !tag.ends_with('"')
        || !tag.as_bytes()[1..tag.len() - 1]
            .iter()
            .all(|&b| (0x21..=0x7e).contains(&b) && b != b'"')
    {
        return Err(fail(
            "Read the scope and supply its exact strong quoted ETag; weak tags and * are invalid.",
        ));
    }
    Ok(tag)
}

fn management_segment(value: &str) -> Result<&str> {
    if value.trim().is_empty() || matches!(value, "." | "..") {
        return Err(fail(
            "Management identifiers must be nonempty and cannot be dot-only URL segments.",
        ));
    }
    Ok(value)
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Definition {
    pub include_sites: Vec<String>,
    pub exclude_sites: Vec<String>,
}

impl Definition {
    pub fn parse(body: Value) -> Result<Self> {
        let definition: Self = serde_json::from_value(body).map_err(|_| {
            fail("Definition requires exactly includeSites and excludeSites arrays; neither may be omitted or null.")
        })?;
        definition.validate()?;
        Ok(definition)
    }

    pub fn validate(&self) -> Result<()> {
        if self
            .include_sites
            .iter()
            .chain(&self.exclude_sites)
            .any(|site| site.trim().is_empty())
        {
            return Err(fail(
                "Site arrays must contain nonempty strings; [] is valid.",
            ));
        }
        Ok(())
    }
}

pub enum Authentication {
    ApiKey(String),
    Bearer(String),
}

#[derive(Debug, PartialEq)]
pub struct SearchOutcome {
    pub result_count: usize,
    pub scope_applied: Option<bool>,
}

pub struct WireResponse {
    pub status: u16,
    pub etag: Option<String>,
    pub body: Vec<u8>,
}

#[derive(Debug)]
pub struct TransportError;

pub trait Transport {
    fn send(
        &self,
        request: Request,
    ) -> impl Future<Output = std::result::Result<WireResponse, TransportError>>;
}

pub struct HttpTransport(reqwest::Client);

impl HttpTransport {
    pub fn new() -> Result<Self> {
        reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .timeout(Duration::from_secs(30))
            .build()
            .map(Self)
            .map_err(|_| fail("Cannot initialize the HTTPS client."))
    }
}

impl Transport for HttpTransport {
    async fn send(&self, request: Request) -> std::result::Result<WireResponse, TransportError> {
        let response = self.0.execute(request).await.map_err(|_| TransportError)?;
        let status = response.status().as_u16();
        let etag = response
            .headers()
            .get("ETag")
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
        let body = response.bytes().await.map_err(|_| TransportError)?.to_vec();
        Ok(WireResponse { status, etag, body })
    }
}

pub struct ScopesClient<T> {
    transport: T,
    auth: Authentication,
}

impl<T: Transport> ScopesClient<T> {
    pub fn new(transport: T, auth: Authentication) -> Self {
        Self { transport, auth }
    }

    fn collection(account: &str) -> Result<Url> {
        let mut url = Url::parse(BASE_URL).map_err(|_| fail("Invalid fixed API origin."))?;
        url.path_segments_mut()
            .map_err(|_| fail("Invalid fixed API origin."))?
            .extend([
                "v1",
                "customer",
                "billing-accounts",
                management_segment(account)?,
                "scopes",
            ]);
        Ok(url)
    }

    fn scope_url(account: &str, name: &str) -> Result<Url> {
        let mut url = Self::collection(account)?;
        url.path_segments_mut()
            .map_err(|_| fail("Invalid fixed API origin."))?
            .push(management_segment(scope_name(name)?)?);
        Ok(url)
    }

    async fn send(
        &self,
        method: Method,
        url: Url,
        expected: u16,
        body: Option<Value>,
        etag: Option<&str>,
    ) -> Result<WireResponse> {
        let management = url.path().starts_with("/v1/customer/");
        let writing = management && method != Method::GET;
        let uncertainty = if writing {
            " The write outcome may be unknown; read and reconcile before retrying."
        } else {
            " Resolve the failure before retrying explicitly."
        };
        let mut request = Request::new(method, url);
        let (header, value) = match &self.auth {
            Authentication::ApiKey(_) if management => {
                return Err(fail(
                    "Management requires app-only Entra authentication, not an API key.",
                ));
            }
            Authentication::ApiKey(key) => ("x-apikey", key.clone()),
            Authentication::Bearer(token) => ("authorization", format!("Bearer {token}")),
        };
        let mut value = HeaderValue::from_str(&value)
            .map_err(|_| fail("Credential is not a valid HTTP header value."))?;
        value.set_sensitive(true);
        request.headers_mut().insert(
            if header == "authorization" {
                AUTHORIZATION
            } else {
                reqwest::header::HeaderName::from_static("x-apikey")
            },
            value,
        );
        if request.method() == Method::PUT || request.method() == Method::DELETE {
            request.headers_mut().insert(
                IF_MATCH,
                HeaderValue::from_str(strong_etag(etag)?)
                    .map_err(|_| fail("Invalid If-Match header."))?,
            );
        }
        if let Some(body) = body {
            *request.body_mut() = Some(
                serde_json::to_vec(&body)
                    .map_err(|_| fail("Cannot serialize request JSON."))?
                    .into(),
            );
            request
                .headers_mut()
                .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        }
        let response = self
            .transport
            .send(request)
            .await
            .map_err(|_| SampleError(format!("Network request failed.{uncertainty}")))?;
        if response.status == expected {
            return Ok(response);
        }
        let guidance = match response.status {
            400 => {
                "Check input, scope availability, and selected profile. Search never falls back."
            }
            401 => "Check credentials, tenant, and token audience.",
            403 => "Check management CustomerApiAccess or runtime profile binding and Web access.",
            404 => "Check the billing account, profile, and scope name.",
            409 => "Read the collection; a duplicate name or full collection may conflict.",
            412 => {
                "Read the latest definition and ETag, reconcile changes, then retry deliberately."
            }
            428 => "Supply the current strong ETag in If-Match.",
            429 => "Respect service retry guidance before retrying explicitly.",
            _ => "Check service access and availability.",
        };
        Err(SampleError(format!(
            "HTTP {}. {guidance}{} Response body and credentials were not logged.",
            response.status,
            if response.status == 408
                || response.status >= 500
                || (300..400).contains(&response.status)
            {
                uncertainty
            } else {
                ""
            },
        )))
    }

    fn json(response: &WireResponse) -> Result<Value> {
        serde_json::from_slice(&response.body)
            .map_err(|_| fail("Service returned invalid JSON; no response body was logged."))
    }

    pub async fn discover(&self) -> Result<Value> {
        let url = Url::parse(&format!("{BASE_URL}/v1/customer/authorized-resources"))
            .map_err(|_| fail("Invalid fixed API origin."))?;
        let body = Self::json(&self.send(Method::GET, url, 200, None, None).await?)?;
        if !body
            .as_array()
            .is_some_and(|items| items.iter().all(Value::is_object))
        {
            return Err(fail(
                "Authorized-resource discovery returned an unexpected JSON shape.",
            ));
        }
        Ok(body)
    }

    pub async fn list(&self, account: &str, page_size: u32) -> Result<Vec<Value>> {
        if !(1..=100).contains(&page_size) {
            return Err(fail("Page size must be between 1 and 100."));
        }
        let mut items = Vec::new();
        let mut token: Option<String> = None;
        let mut seen = HashSet::new();
        loop {
            let mut url = Self::collection(account)?;
            url.query_pairs_mut()
                .append_pair("pageSize", &page_size.to_string());
            if let Some(token) = &token {
                url.query_pairs_mut()
                    .append_pair("continuationToken", token);
            }
            let page = Self::json(&self.send(Method::GET, url, 200, None, None).await?)?;
            let page_items = page
                .get("items")
                .and_then(Value::as_array)
                .filter(|items| items.iter().all(Value::is_object))
                .ok_or_else(|| fail("Scope list returned an unexpected items shape."))?;
            items.extend(page_items.iter().cloned());
            match page.get("nextContinuationToken") {
                None | Some(Value::Null) => return Ok(items),
                Some(Value::String(next)) if !next.is_empty() && seen.insert(next.clone()) => {
                    token = Some(next.clone());
                }
                _ => {
                    return Err(fail(
                        "Scope list returned an invalid or repeated continuation token.",
                    ));
                }
            }
        }
    }

    pub async fn get(&self, account: &str, name: &str) -> Result<(Definition, String)> {
        let response = self
            .send(
                Method::GET,
                Self::scope_url(account, name)?,
                200,
                None,
                None,
            )
            .await?;
        let body = Self::json(&response)?;
        let definition = Definition::parse(json!({
            "includeSites": body.get("includeSites"),
            "excludeSites": body.get("excludeSites"),
        }))?;
        Ok((
            definition,
            strong_etag(response.etag.as_deref())?.to_owned(),
        ))
    }

    pub async fn create(
        &self,
        account: &str,
        name: &str,
        definition: &Definition,
    ) -> Result<String> {
        Self::scope_url(account, name)?;
        definition.validate()?;
        let response = self.send(Method::POST, Self::collection(account)?, 201, Some(json!({
            "name": name, "includeSites": definition.include_sites, "excludeSites": definition.exclude_sites,
        })), None).await?;
        Ok(strong_etag(response.etag.as_deref())?.to_owned())
    }

    pub async fn replace(
        &self,
        account: &str,
        name: &str,
        definition: &Definition,
        etag: &str,
    ) -> Result<String> {
        definition.validate()?;
        let body =
            serde_json::to_value(definition).map_err(|_| fail("Cannot serialize definition."))?;
        let response = self
            .send(
                Method::PUT,
                Self::scope_url(account, name)?,
                200,
                Some(body),
                Some(etag),
            )
            .await?;
        Ok(strong_etag(response.etag.as_deref())?.to_owned())
    }

    pub async fn delete(&self, account: &str, name: &str, etag: &str) -> Result<()> {
        self.send(
            Method::DELETE,
            Self::scope_url(account, name)?,
            204,
            None,
            Some(etag),
        )
        .await?;
        Ok(())
    }

    pub async fn search(
        &self,
        name: &str,
        query: &str,
        app_id: Option<&str>,
    ) -> Result<SearchOutcome> {
        scope_name(name)?;
        if query.trim().is_empty() {
            return Err(fail("Search query cannot be empty."));
        }
        if app_id.is_some_and(|id| {
            id.trim().is_empty() || matches!(self.auth, Authentication::ApiKey(_))
        }) {
            return Err(fail(
                "APPID is only for Entra search and must be a nonempty Web IQ AppID.",
            ));
        }
        let mut url = Url::parse(&format!("{BASE_URL}/v3/search/web"))
            .map_err(|_| fail("Invalid fixed API origin."))?;
        if let Some(id) = app_id {
            url.query_pairs_mut().append_pair("APPID", id);
        }
        let response = self
            .send(
                Method::POST,
                url,
                200,
                Some(json!({
                    "query": query, "scopeName": name, "failOnMissingScope": true, "maxResults": 5,
                    "language": "en", "region": "US", "contentFormat": "passage", "maxLength": 3000,
                })),
                None,
            )
            .await?;
        let body = Self::json(&response)?;
        let result_count = body
            .get("webResults")
            .and_then(Value::as_array)
            .filter(|items| items.iter().all(Value::is_object))
            .map(Vec::len)
            .ok_or_else(|| fail("Search response did not contain a webResults array."))?;
        let scope_applied = match body.get("scopeApplied") {
            None | Some(Value::Null) => None,
            Some(Value::Bool(value)) => Some(*value),
            _ => {
                return Err(fail(
                    "Search response contained an invalid scopeApplied value.",
                ));
            }
        };
        if scope_applied == Some(false) {
            return Err(fail(
                "Search returned scopeApplied: false; the named scope was not applied. Stopped without retrying unscoped.",
            ));
        }
        Ok(SearchOutcome {
            result_count,
            scope_applied,
        })
    }
}

#[derive(Parser)]
#[command(
    name = "webiq-scopes",
    about = "Manage named Web IQ scopes or search with one explicit scope."
)]
pub struct Arguments {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Subcommand)]
pub enum Command {
    Discover,
    List {
        #[arg(long, default_value_t = 50)]
        page_size: u32,
    },
    Get {
        name: String,
    },
    Create {
        name: String,
        #[arg(long)]
        definition: PathBuf,
        #[arg(long)]
        allow_all_sites: bool,
    },
    Replace {
        name: String,
        #[arg(long)]
        definition: PathBuf,
        #[arg(long)]
        if_match: String,
        #[arg(long)]
        allow_all_sites: bool,
    },
    Delete {
        name: String,
        #[arg(long)]
        if_match: String,
    },
    Search {
        name: String,
        query: String,
        #[arg(long, default_value = "api-key", value_parser = ["api-key", "entra"])]
        auth: String,
        #[arg(long)]
        app_id: Option<String>,
    },
}

pub fn required(getenv: &impl Fn(&str) -> Option<String>, name: &str) -> Result<String> {
    getenv(name)
        .filter(|value| !value.trim().is_empty() && !value.starts_with('<'))
        .ok_or_else(|| {
            SampleError(format!(
                "Set {name} to a non-placeholder value before running."
            ))
        })
}

pub enum AuthSettings {
    ApiKey(String),
    #[cfg(feature = "certificate-auth")]
    Certificate {
        tenant: String,
        client: String,
        path: String,
        password: Option<String>,
    },
}

impl AuthSettings {
    pub fn read(command: &Command, getenv: &impl Fn(&str) -> Option<String>) -> Result<Self> {
        if matches!(command, Command::Search { auth, .. } if auth == "api-key") {
            return Ok(Self::ApiKey(required(getenv, "WEBIQ_API_KEY")?));
        }
        #[cfg(feature = "certificate-auth")]
        {
            Ok(Self::Certificate {
                tenant: required(getenv, "AZURE_TENANT_ID")?,
                client: required(getenv, "AZURE_CLIENT_ID")?,
                path: required(getenv, "AZURE_CLIENT_CERTIFICATE_PATH")?,
                password: getenv("AZURE_CLIENT_CERTIFICATE_PASSWORD"),
            })
        }
        #[cfg(not(feature = "certificate-auth"))]
        Err(fail(
            "Management and Entra search require rebuilding with --features certificate-auth.",
        ))
    }

    pub async fn authenticate(self) -> Result<Authentication> {
        match self {
            Self::ApiKey(key) => Ok(Authentication::ApiKey(key)),
            #[cfg(feature = "certificate-auth")]
            Self::Certificate {
                tenant,
                client,
                path,
                password,
            } => {
                let bytes = std::fs::read(path)
                    .map_err(|_| fail("Cannot read the private-key PFX file."))?;
                let options = ClientCertificateCredentialOptions {
                    password: password.map(Into::into),
                    ..Default::default()
                };
                let credential = ClientCertificateCredential::new(tenant, client, bytes.into(), Some(options))
                    .map_err(|_| fail("Cannot load certificate credentials. Check tenant/client IDs, RSA private-key PFX, and existing password."))?;
                let token = credential.get_token(&[TOKEN_SCOPE], None).await
                    .map_err(|_| fail("Entra authentication failed. Check certificate settings and service access; credential details were not logged."))?;
                Ok(Authentication::Bearer(token.token.secret().to_owned()))
            }
        }
    }
}

pub fn validate_command(command: &Command) -> Result<Option<Definition>> {
    match command {
        Command::List { page_size } if !(1..=100).contains(page_size) => {
            return Err(fail("Page size must be between 1 and 100."));
        }
        Command::Get { name }
        | Command::Create { name, .. }
        | Command::Replace { name, .. }
        | Command::Delete { name, .. } => {
            management_segment(scope_name(name)?)?;
        }
        Command::Search {
            name,
            query,
            auth,
            app_id,
        } => {
            scope_name(name)?;
            if query.trim().is_empty()
                || !["api-key", "entra"].contains(&auth.as_str())
                || app_id
                    .as_ref()
                    .is_some_and(|id| id.trim().is_empty() || auth == "api-key")
            {
                return Err(fail("Check the query, --auth, and Entra-only --app-id."));
            }
        }
        _ => {}
    }
    if let Command::Replace { if_match, .. } | Command::Delete { if_match, .. } = command {
        strong_etag(Some(if_match))?;
    }
    if let Command::Create {
        definition,
        allow_all_sites,
        ..
    }
    | Command::Replace {
        definition,
        allow_all_sites,
        ..
    } = command
    {
        let text = std::fs::read_to_string(definition)
            .map_err(|_| fail("Cannot read the definition file as UTF-8 JSON."))?;
        let body = serde_json::from_str(&text)
            .map_err(|_| fail("Cannot read the definition file as UTF-8 JSON."))?;
        let definition = Definition::parse(body)?;
        if definition.include_sites.is_empty() && !allow_all_sites {
            return Err(fail(
                "Empty includeSites requires an explicit --allow-all-sites.",
            ));
        }
        return Ok(Some(definition));
    }
    Ok(None)
}

pub async fn execute<T: Transport>(
    client: &ScopesClient<T>,
    command: &Command,
    account: Option<&str>,
    definition: Option<&Definition>,
) -> Result<String> {
    let account = if matches!(command, Command::Discover | Command::Search { .. }) {
        ""
    } else {
        management_segment(
            account.ok_or_else(|| fail("Set WEBIQ_BILLING_ACCOUNT_ID before running."))?,
        )?
    };
    let pretty = |value: &Value| {
        serde_json::to_string_pretty(value).map_err(|_| fail("Cannot serialize management output."))
    };
    match command {
        Command::Discover => {
            let customers = client.discover().await?;
            let mut output = pretty(&customers)?;
            if customers.as_array().is_some_and(Vec::is_empty) {
                output.push_str(
                    "\nNo authorized resources. Check workload registration and CustomerApiAccess.",
                );
            }
            Ok(output)
        }
        Command::List { page_size } => pretty(&json!(client.list(account, *page_size).await?)),
        Command::Get { name } => {
            let (definition, etag) = client.get(account, name).await?;
            pretty(&json!({
                "name": name, "includeSites": definition.include_sites,
                "excludeSites": definition.exclude_sites, "etag": etag,
            }))
        }
        Command::Create { name, .. } | Command::Replace { name, .. } => {
            let definition =
                definition.ok_or_else(|| fail("A validated definition is required."))?;
            let etag = if let Command::Replace { if_match, .. } = command {
                client.replace(account, name, definition, if_match).await?
            } else {
                client.create(account, name, definition).await?
            };
            Ok(format!(
                "Authoring write succeeded. ETag: {etag}\nServing propagation is asynchronous; this is not a readiness signal."
            ))
        }
        Command::Delete { name, if_match } => {
            client.delete(account, name, if_match).await?;
            Ok(
                "Scope deleted from authoring storage; serving propagation is asynchronous."
                    .to_owned(),
            )
        }
        Command::Search {
            name,
            query,
            app_id,
            ..
        } => {
            let outcome = client.search(name, query, app_id.as_deref()).await?;
            let status = if outcome.scope_applied == Some(true) {
                "scopeApplied: true"
            } else {
                "the service did not report whether the scope was applied"
            };
            Ok(format!(
                "Search returned {} web results; {status}. Retrieved content was not printed.",
                outcome.result_count
            ))
        }
    }
}
