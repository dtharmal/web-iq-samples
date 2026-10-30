# Web IQ API Management Gateway

Deploy an optional Azure API Management (APIM) gateway in front of Web IQ.
The gateway validates a delegated Entra access token, checks the approved client
and scope, redacts common sensitive-number patterns, and calls Web IQ with its
system-assigned managed identity.

This is an educational sample, not a production reference architecture or a
runnable desktop/MSAL client. APIM and monitoring costs are separate from Web IQ
usage. The default `Developer_1` tier is billed while provisioned and has no
production SLA; review [APIM pricing](https://azure.microsoft.com/en-us/pricing/details/api-management/)
before deploying. Other tiers, including Consumption, require compatibility review.

## Prerequisites

- Terraform 1.9 or later and Azure CLI; macOS/Linux examples also use Bash and `curl`
- An Azure subscription with permission to create a resource group, APIM, Log Analytics, Application Insights, and role assignments
- The `Microsoft.ApiManagement`, `Microsoft.OperationalInsights`, and `Microsoft.Insights` resource providers registered
- An Entra gateway API registration, delegated scope, and approved public-client application; see [Frontend authentication](docs/authentication.md#frontend-authentication)
- Web IQ access and permission to bind a managed identity to your Web IQ AppID in the Portal

## Get started

Run all commands from this sample's directory. The deployment creates a **new
resource group and APIM instance**; it does not configure a shared gateway.

### Windows (PowerShell)

```powershell
Copy-Item terraform.tfvars.example terraform.tfvars
az login --tenant "<microsoft-entra-tenant-id>"
```

### macOS/Linux (Bash)

```bash
cp terraform.tfvars.example terraform.tfvars
az login --tenant "<microsoft-entra-tenant-id>"
```

### Configure and deploy

Replace every angle-bracket placeholder in `terraform.tfvars`: subscription,
publisher, tenant, gateway audience, and approved frontend client IDs. Ensure
`api_required_scope` matches your delegated scope. The gateway API's audience
and the caller's application ID are **different values**; see the
[authentication guidance](docs/authentication.md).

Keep configured variables, state, and saved plans out of source control; the
repository ignores them. Request telemetry is disabled by default.

Run in either shell:

```text
terraform init -input=false -lockfile=readonly
terraform plan -out main.tfplan
terraform apply main.tfplan
```

APIM provisioning can take considerably longer than the other resources.

### Bind the managed identity

After deployment, look up the APIM identity's Application (Client) ID. This
command works in PowerShell and Bash:

```text
az ad sp show --id "$(terraform output -raw apim_managed_identity_principal_id)" --query appId --output tsv
```

Bind the returned **Application (Client) ID**, not the object/principal ID, to
your Web IQ AppID in the Portal. Terraform does not create this binding, and
Azure RBAC does not grant Web IQ access. Use one dedicated AppID mapping for
this sample; see [Managed identity binding](docs/authentication.md#managed-identity-binding).

## Call the API

Use your approved client's delegated access token. The development-only Azure
CLI examples below work **only if the API owner has granted/consented to the
CLI client's delegated permission and added its application ID to
`api_allowed_client_ids`**. Do not add a test client to a production allowlist
just to run the example. Replace the example scope if yours differs.

### Windows (PowerShell)

```powershell
$token = az account get-access-token `
  --scope "api://<frontend-api-application-client-id>/access_as_user" `
  --query accessToken --output tsv
$gatewayUrl = terraform output -raw gateway_url

Invoke-RestMethod `
  -Method Post `
  -Uri $gatewayUrl `
  -Headers @{ Authorization = "Bearer $token"; deptId = "finance" } `
  -ContentType "application/json" `
  -Body '{"query":"Recent developments for customer 123-45-6789","maxResults":5,"contentFormat":"passage"}'

Remove-Variable token
```

### macOS/Linux (Bash)

```bash
token="$(az account get-access-token \
  --scope "api://<frontend-api-application-client-id>/access_as_user" \
  --query accessToken --output tsv)"
gateway_url="$(terraform output -raw gateway_url)"

curl --silent --show-error --fail \
  --request POST "$gateway_url" \
  --header "Authorization: Bearer $token" \
  --header "deptId: finance" \
  --header "Content-Type: application/json" \
  --data '{"query":"Recent developments for customer 123-45-6789","maxResults":5,"contentFormat":"passage"}'

unset token
```

The endpoint forwards to `https://api.microsoft.ai/v3/search/web`. The synthetic
SSN-like value is replaced with `[REDACTED-SSN]` before forwarding. Redaction
inspects only `query` and is **not complete DLP**. The caller-supplied `deptId`
is attribution, not authorization.

The examples use `passage`, which works for every Web IQ application. Full
document formats (`text`, `html`, and `markdown`) require full document access;
without it, Web IQ returns HTTP 403 `AuthContentFormatForbidden`. See the
[content-format FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#content-formats).

Use synthetic or approved public queries. Do not print tokens, put credentials
or query text in URLs, or share unreviewed responses. See
[Security and logging](docs/security-and-logging.md) for limitations and production controls.

## Logging

Set `enable_telemetry = true` and apply to enable Application Insights request,
dependency, error, and custom-trace telemetry. Set it back to `false` and apply
to stop this sample's collection. Body logging is disabled, but URLs and other
metadata can still contain sensitive data. See
[collected data, retention, and opt-out details](docs/security-and-logging.md#logging).
Disabling collection does not delete existing data or monitoring resources.

## Validation

Run in either shell without provisioning Azure resources:

```text
terraform fmt -check -recursive
terraform init -backend=false -input=false -lockfile=readonly
terraform validate
terraform test
```

Tests use mocked providers; they do not compile gateway policies or prove live
Web IQ authentication. Complete the
[post-deployment checks](docs/security-and-logging.md#post-deployment-checks)
before relying on a deployed gateway.

## Clean up

Run `terraform destroy` in either shell. This deletes the sample's resource
group and **all its resources**. Remove the managed-identity binding from Web IQ
as well. Do not use this sample to manage an existing/shared resource group.

## Further guidance

- [Authentication](docs/authentication.md): API registration, approved clients, token audiences, Portal binding, and authentication failures.
- [Security and logging](docs/security-and-logging.md): redaction limits, telemetry, production controls, and live validation.
