# Web IQ named scopes (.NET)

Manage named scopes through the Customer Management API, then use an API- or UI-authored
scope with the web endpoint. This self-contained console sample uses `HttpClient`
and Azure Identity, with explicit account/profile selection and no automatic
management retries or unscoped search fallback.

## Prerequisites and portal setup

- [.NET 10 SDK](https://dotnet.microsoft.com/download/dotnet/10.0).
- A provisioned Web IQ account.
- For management, an existing Entra application with an RSA private-key PFX and
  its public certificate registered in Entra. A human administrator must first
  register the workload and grant billing-account **CustomerApiAccess**.
- For search, a Web IQ API key for the profile in the scope's billing account,
  or a separately bound runtime Entra application with Web access.
- PowerShell 7.3 or later for the Windows examples, including quoted ETag arguments.

Use Learn's [Create scopes guide](https://learn.microsoft.com/en-us/web-iq/guides/create-scopes)
for portal authoring and workload authorization, and the
[Scopes reference](https://learn.microsoft.com/en-us/web-iq/reference/scopes)
for the Customer Management API. Runtime profile binding is described below.

CustomerApiAccess is broader than scopes and is not a runtime profile binding.
A runtime binding alone grants no management access. API-key search of a
UI-authored scope needs no management credential.

## Build and configure

Run from this sample directory:

```powershell
dotnet build src\WebIqScopes
$env:AZURE_TENANT_ID = "<directory-tenant-id>"
$env:AZURE_CLIENT_ID = "<application-client-id>"
$env:AZURE_CLIENT_CERTIFICATE_PATH = "<absolute-path-to-private-key.pfx>"
$password = Read-Host "Existing PFX password, if encrypted" -AsSecureString
$env:AZURE_CLIENT_CERTIFICATE_PASSWORD = (
  [System.Net.NetworkCredential]::new("", $password).Password
)
```

Omit the password variable for an unencrypted PFX. This .NET sample loads PKCS12/PFX
with an ephemeral key and explicitly uses `ClientCertificateCredential`, requesting
`https://api.microsoft.ai/.default`. It does not silently use developer sign-in,
client secrets, or managed identity. A successful token request is not proof of
service authorization. The private key must remain outside source control.

For macOS/Linux, use `/` in command paths and `export NAME=value` for environment
variables. The application arguments are otherwise identical; preserve the double
quotes inside ETags. Environment variables are a secret-delivery mechanism, not a vault.

## Scenario 1: create and manage scopes

```powershell
dotnet run --project src\WebIqScopes -- discover
$env:WEBIQ_BILLING_ACCOUNT_ID = "<billing-account-id-from-discovery>"
dotnet run --project src\WebIqScopes -- list --page-size 50
dotnet run --project src\WebIqScopes -- create ProductDocs --definition product-docs.json
dotnet run --project src\WebIqScopes -- get ProductDocs
```

Discovery calls `GET https://api.microsoft.ai/v1/customer/authorized-resources`.
It prints customers/accounts, including IDs for deliberate local selection, and
never chooses the first account. `200` with `[]` can indicate an unknown or
unassigned workload. Discovery's `permissions.canWrite` is a legacy billing-config
permission, not scope-write authority.

The collection is `/v1/customer/billing-accounts/{billingAccountId}/scopes`.
`list` follows every opaque continuation token unchanged, with a default page size
of 50 and maximum 100. It returns metadata/counts; `get` reads the site lists and
strong ETag. Create is a `POST`, not an upsert; duplicate names (case-insensitive)
or a full collection can return `409`.

The provided JSON includes `learn.microsoft.com/en-us/entra` and excludes
`learn.microsoft.com/en-us/entra/identity/monitoring-health`. Both `includeSites`
and `excludeSites` must be arrays. Edit both complete lists before replacing:

```powershell
$snapshot = dotnet run --no-build --project src\WebIqScopes -- get ProductDocs | ConvertFrom-Json
# Review the current definition and edit product-docs.json before continuing.
dotnet run --no-build --project src\WebIqScopes -- replace ProductDocs `
  --definition product-docs.json --if-match $snapshot.etag
```

Replace uses `PUT .../scopes/{name}`, with full replacement lists and the exact
strong quoted `If-Match`. It does not rename. Missing/null arrays are rejected;
explicit `[]` is intentional. Empty inclusions broaden to **all non-excluded
sources** and require `--allow-all-sites`; exclusions win.

On `412`, read the latest definition and ETag, reconcile competing changes, then
retry deliberately. Do not remove quotes, use `*`/weak tags, or blindly overwrite.
Create/update success and a new ETag acknowledge authoring, **not serving readiness**.
Changes should propagate in under 30 minutes, but there is no propagation SLA
and this timing is not guaranteed. There is no readiness endpoint or scope
versioning. Neither the authoring ETag nor runtime `scopeApplied: true` identifies
the version currently served. Verify intended source behavior rather than
treating a fixed wait as readiness. See [Search propagation](https://learn.microsoft.com/en-us/web-iq/reference/scopes#search-propagation).

For optional cleanup, review the scope and delete only your dedicated sample resource:

```powershell
$snapshot = dotnet run --no-build --project src\WebIqScopes -- get ProductDocs | ConvertFrom-Json
dotnet run --no-build --project src\WebIqScopes -- delete ProductDocs --if-match $snapshot.etag
```

Delete requires `If-Match` and accepts `204` without parsing JSON. It also propagates
asynchronously. A timeout/server failure can leave an unknown write outcome; read
and reconcile before retrying. This sample rejects dot-only (`.` or `..`) management
identifiers rather than letting URL normalization change their meaning.

## Scenario 2: search with an API- or UI-authored scope

If an authorized author created and published `ProductDocs` in the Scopes UI,
skip the management steps. Use the same runtime request either way:

```powershell
$env:WEBIQ_API_KEY = "<api-key-for-the-intended-webiq-profile>"
dotnet run --project src\WebIqScopes -- search ProductDocs "Microsoft Entra app authentication"
```

Search sends `POST https://api.microsoft.ai/v3/search/web` with `x-apikey` and:

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

`scopeName` is one string, never a selector array. Names use 1-128 ASCII letters,
digits, periods, underscores, and hyphens; lookup is case-insensitive.
`failOnMissingScope` stays `true`. A missing/unavailable scope fails rather than
silently searching the unscoped web. The CLI reports a result count and the
service's optional `scopeApplied` outcome, not the query, URLs, or retrieved content.
`true` is reported as `scopeApplied: true`; it is not evidence that the latest
revision is serving. `false` exits nonzero without an unscoped retry. A missing
or `null` field is explicitly reported as unknown, not as scoped success.
HTTP `200` and a result count alone do not prove scope application.

For runtime Entra, open **Profile Management** in the
[Web IQ portal](https://webiq.microsoft.ai), select the profile in the scope's
billing account, and use **Application (Client) IDs > Bind Application (Client) ID**
to bind the runtime Entra application/client ID. Confirm Web access; this binding
is separate from CustomerApiAccess. Configure the certificate variables for that
application and run:

```powershell
dotnet run --project src\WebIqScopes -- search ProductDocs "Microsoft Entra app authentication" --auth entra
```

If bound to multiple profiles, append `--app-id "<webiq-appid-for-the-intended-profile>"`.
This sends `?APPID=...`, the **Web IQ AppID**, not the Entra application/client ID.
API-key search must not include APPID. Management never uses APPID.

## Errors, privacy, and validation

Errors exit nonzero with status/fixed guidance, not raw server bodies or credential
details. Check input/availability on `400`, separate management/runtime grants on
`401`/`403`, duplicate/capacity conflicts on `409`, current ETags and reconciliation
on `412`/`428`, and service retry guidance on `429`. Requests time out after 30 seconds;
redirects are not followed. No management writes retry automatically.

Discovery/list/get intentionally print management data; discovery includes account
IDs. Review/redact stdout before sharing, avoid secret-bearing shell transcripts,
and clear secret environment variables after use. This sample adds no custom
telemetry or response files. Service-side handling is separate; see the
[Web IQ privacy guidance](https://learn.microsoft.com/en-us/web-iq/resources/faq#privacy-and-data-handling).
Live search can incur usage charges; manually running management commands mutates
real resources. No grants, credentials, or live scopes are created by the tests.

```powershell
dotnet test tests\WebIqScopes.Tests
dotnet run --project src\WebIqScopes -- --help
```

Tests use a fake `HttpMessageHandler` and token-acquisition delegate, never service
access. They cover wire contracts, authentication separation, pagination, ETags,
conflicts, missing-scope failure, explicit empty lists, CLI safety, true/false/unknown
scope-application outcomes, and sanitized diagnostics. See
[Create scopes](https://learn.microsoft.com/en-us/web-iq/guides/create-scopes)
and the [Scopes reference](https://learn.microsoft.com/en-us/web-iq/reference/scopes)
for portal authoring and service semantics.
