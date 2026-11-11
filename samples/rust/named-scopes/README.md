# Web IQ named scopes (Rust)

A self-contained Rust CLI for two scenarios: create/manage named scopes through
the Customer Management API, and call the web endpoint with a scope authored
through either the API or the Scopes UI. Uses `reqwest` with explicit account/profile
selection, strong conditional writes, and no automatic Web IQ retries or unscoped
fallback. API-key search is the default build; certificate authentication is opt-in.

## Prerequisites and portal setup

- A current stable [Rust toolchain](https://www.rust-lang.org/tools/install) with
  Cargo, Rustfmt, and Clippy; use Rust 1.99 or later with the included lockfile.
- A C/C++ toolchain and CMake for the Rustls TLS provider. On Windows, use
  **Developer PowerShell for Visual Studio** with C++ Build Tools installed;
  macOS/Linux need the corresponding platform build tools.
- A provisioned Web IQ account.
- For search, an API key for the profile in the scope's billing account, or an
  Entra application separately bound to that profile with Web access.
- PowerShell 7.3 or later for the Windows commands, including quoted ETag arguments.

Follow Learn's [Create scopes guide](https://learn.microsoft.com/en-us/web-iq/guides/create-scopes)
for portal authoring and workload authorization, and the
[Scopes reference](https://learn.microsoft.com/en-us/web-iq/reference/scopes)
for the Customer Management API. Runtime profile binding is described below.

Management grants are not runtime bindings; runtime bindings are not management
grants. CustomerApiAccess is broader than scope management. API-key search with a
UI-authored scope needs no management credential.

## Quickstart: API-key search without OpenSSL

An authorized author can create and publish `ProductDocs` in the Scopes UI before
you run the sample. With that scope in the intended profile's billing account,
run from this sample directory:

```powershell
# Keep dependency build paths below Windows toolchain limits.
$env:CARGO_TARGET_DIR = Join-Path $env:TEMP "webiq-scopes-target"
cargo build --locked
$env:WEBIQ_API_KEY = "<api-key-for-the-intended-webiq-profile>"
cargo run --locked -- search ProductDocs "Microsoft Entra app authentication"
```

The default build does not include Azure Identity or OpenSSL and needs no Perl.
The Rustls TLS provider still needs the build tools above; this is not a
native-dependency-free build. Management and Entra search require the optional
certificate feature below and fail explicitly if it was not compiled.

The short target directory is a Windows build setting, not a credential; keep it
set for subsequent Cargo commands in this shell. On macOS/Linux it is optional.
Use `export NAME=value` for environment assignments; the Cargo commands are the
same. Preserve the double quotes inside ETag arguments. Environment variables
are a secret-delivery mechanism, not a vault.

## Certificate configuration for management and Entra search

For management, use an existing Entra application with an RSA private-key
PKCS12/PFX and its public certificate registered in Entra. A human administrator
must register the workload and grant **CustomerApiAccess** on the billing account.

The optional `certificate-auth` Cargo feature uses Azure Identity's
`ClientCertificateCredential`, which depends on OpenSSL. Its source-build
(`vendored`) dependency needs native Perl in addition to the build tools above.
On Windows, install a native distribution such as
[Strawberry Perl](https://strawberryperl.com) and put `perl` on PATH in Developer
PowerShell; Git's reduced Perl distribution is not sufficient. macOS/Linux need
Perl and Make. A separate OpenSSL installation is not required, and the first
certificate-enabled build can take several minutes.

```powershell
cargo build --locked --features certificate-auth
$env:AZURE_TENANT_ID = "<directory-tenant-id>"
$env:AZURE_CLIENT_ID = "<application-client-id>"
$env:AZURE_CLIENT_CERTIFICATE_PATH = "<absolute-path-to-private-key.pfx>"
$password = Read-Host "Existing PFX password, if encrypted" -AsSecureString
$env:AZURE_CLIENT_CERTIFICATE_PASSWORD = (
  [System.Net.NetworkCredential]::new("", $password).Password
)
```

Omit the password variable for an unencrypted PFX. This example explicitly uses
Azure Identity's `ClientCertificateCredential` and requests
`https://api.microsoft.ai/.default`. It does not use a credential chain,
developer sign-in, client secrets, or managed identity as a fallback. Rust's
certificate mode requires PKCS12/PFX with the RSA private key, not PEM or a
public-only certificate. Keep it outside source control.

## Scenario 1: create and manage scopes

```powershell
cargo run --locked --features certificate-auth -- discover
$env:WEBIQ_BILLING_ACCOUNT_ID = "<billing-account-id-from-discovery>"
cargo run --locked --features certificate-auth -- list --page-size 50
cargo run --locked --features certificate-auth -- create ProductDocs --definition product-docs.json
cargo run --locked --features certificate-auth -- get ProductDocs
```

Discovery calls `GET https://api.microsoft.ai/v1/customer/authorized-resources`.
It prints customers/accounts, including IDs for deliberate local selection, and
never picks the first account. `200` with `[]` can indicate an unknown or unassigned
workload. An Entra token does not establish service authorization.
`permissions.canWrite` is a legacy billing-configuration permission, not scope-write
authority.

The collection route is `/v1/customer/billing-accounts/{billingAccountId}/scopes`.
`list` follows all pages with an opaque, unchanged continuation token (default
page size 50, maximum 100). Entries are metadata/counts; `get` reads the full
definition and strong ETag. Create uses `POST`, not an upsert; case-insensitive
duplicates or collection capacity can return `409`.

The example JSON includes `learn.microsoft.com/en-us/entra` and excludes
`learn.microsoft.com/en-us/entra/identity/monitoring-health`. Both `includeSites`
and `excludeSites` must be arrays. Read/reconcile your desired changes with the
current scope, edit both complete lists in the file, and then replace:

```powershell
$snapshot = cargo run --quiet --locked --features certificate-auth -- get ProductDocs | ConvertFrom-Json
# Review the current definition and edit product-docs.json before continuing.
cargo run --locked --features certificate-auth -- replace ProductDocs --definition product-docs.json --if-match $snapshot.etag
```

Replace sends `PUT .../scopes/{name}` with the complete definition and exact strong
quoted `If-Match`. It does not rename. Missing/null arrays are rejected; `[]` is
intentional. Empty inclusions broaden to **all non-excluded sources** and require
`--allow-all-sites`; exclusions win.

On `412`, read the latest scope/ETag, reconcile changes, and retry deliberately.
Never use `*`, weak tags, stripped quotes, or a fresh ETag to blindly overwrite.
Authoring success and new ETags are **not serving-readiness signals**. Changes
should propagate in under 30 minutes, but there is no propagation SLA and this
timing is not guaranteed. There is no readiness endpoint or scope versioning.
Neither the authoring ETag nor runtime `scopeApplied: true` identifies the version
currently served. Verify intended source behavior rather than treating a fixed
wait as readiness. See [Search propagation](https://learn.microsoft.com/en-us/web-iq/reference/scopes#search-propagation).

Optional cleanup, only after reviewing your dedicated sample scope:

```powershell
$snapshot = cargo run --quiet --locked --features certificate-auth -- get ProductDocs | ConvertFrom-Json
cargo run --locked --features certificate-auth -- delete ProductDocs --if-match $snapshot.etag
```

Delete requires the current ETag and accepts `204` without parsing JSON. It also
propagates asynchronously. On uncertain write outcomes, read/reconcile before
retrying; writes never retry automatically. This sample rejects dot-only (`.` or
`..`) management identifiers rather than letting URL normalization change them.

## Scenario 2: search with an API- or UI-authored scope

Skip management if an authorized author created and published `ProductDocs` in
the Scopes UI. Use the same search command for either authoring method:

```powershell
$env:WEBIQ_API_KEY = "<api-key-for-the-intended-webiq-profile>"
cargo run --locked -- search ProductDocs "Microsoft Entra app authentication"
```

This sends `POST https://api.microsoft.ai/v3/search/web` with `x-apikey` and:

```json
{
  "query": "Microsoft Entra app authentication",
  "scopeName": "ProductDocs",
  "failOnMissingScope": true,
  "maxResults": 5,
  "language": "en",
  "region": "US",
  "contentFormat": "passage",
  "maxLength": 3000
}
```

`scopeName` is one string, not an array. Names use 1-128 ASCII letters, digits,
periods, underscores, and hyphens; lookup is case-insensitive. The sample always
sends `failOnMissingScope: true` and never falls back to unscoped search. Result
counts alone do not prove that the intended revision has propagated.
The CLI reports the count and service's optional `scopeApplied` outcome, not the
query, result URLs, or retrieved content. `true` is reported as `scopeApplied: true`;
it is not evidence that the latest revision is serving. `false` exits nonzero
without an unscoped retry. A missing or `null` field is explicitly reported as
unknown, not as scoped success. HTTP `200` and a result count alone do not prove
scope application.

For Entra search, open **Profile Management** in the
[Web IQ portal](https://webiq.microsoft.ai), select the profile in the scope's
billing account, and use **Application (Client) IDs > Bind Application (Client) ID**
to bind the runtime Entra application/client ID. Confirm Web access; this binding
is separate from CustomerApiAccess. Configure the certificate variables for that
application:

```powershell
cargo run --locked --features certificate-auth -- search ProductDocs "Microsoft Entra app authentication" --auth entra
```

If bound to multiple profiles, append `--app-id "<webiq-appid-for-the-intended-profile>"`.
This is the **Web IQ AppID**, not the Entra application/client ID, and sends
`?APPID=...` only for runtime Entra requests. API-key search rejects APPID;
management does not use it.

## Errors, privacy, and validation

Errors exit nonzero with status/fixed guidance, not raw server bodies or credential
details. Check input/availability on `400`, separate management/runtime grants on
`401`/`403`, duplicate/capacity conflicts on `409`, current ETags and reconciliation
on `412`/`428`, and service retry guidance on `429`. Web IQ requests time out after
30 seconds; redirects and automatic retries are disabled.

Discovery/list/get intentionally print management data; discovery includes account
IDs. Review/redact stdout before sharing. Avoid secret-bearing transcripts and
clear secret environment variables after use. This example adds no custom telemetry,
logging subscribers, or response files. Service-side handling is separate; see the
[Web IQ privacy guidance](https://learn.microsoft.com/en-us/web-iq/resources/faq#privacy-and-data-handling).
Live search can incur usage charges; manually running management commands mutates
real resources.

```powershell
cargo test --locked
cargo test --locked --features certificate-auth
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo clippy --locked --all-targets --features certificate-auth -- -D warnings
cargo run --locked -- --help
```

Tests use an in-memory implementation of the HTTP transport and do not request
tokens, contact Web IQ, or create grants/scopes. They cover wire contracts,
management/runtime auth separation, pagination, ETags, conflicts, missing-scope
failure, CLI safety, explicit empty lists, propagation messaging, true/false/unknown
scope-application outcomes, feature selection, and sanitized diagnostics. The
certificate-enabled checks require the optional native tools above. See
[Create scopes](https://learn.microsoft.com/en-us/web-iq/guides/create-scopes)
and the [Scopes reference](https://learn.microsoft.com/en-us/web-iq/reference/scopes)
for portal authoring and service semantics.
