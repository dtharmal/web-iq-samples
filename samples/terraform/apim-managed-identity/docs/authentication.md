# Gateway authentication

```text
Desktop/public client -- delegated access token --> APIM
APIM -- managed identity token --> Web IQ
```

These are separate authentication hops, not an on-behalf-of flow. Web IQ sees
APIM's identity, not the desktop user. The sample deploys the gateway, not a
desktop/MSAL implementation or Entra application registrations.

## Frontend authentication

1. Use a single-tenant Entra API registration for the gateway unless an approved registration already exists.
2. Expose a delegated scope such as `access_as_user`.
3. Grant the existing desktop/public client permission to request that scope and obtain the required consent or preauthorization.
4. Configure the sample's `terraform.tfvars` with the tenant, gateway audience, scope, and approved callers.

| Variable | Value |
| --- | --- |
| `entra_tenant_id` | The single tenant's GUID, not `common` or `organizations` |
| `api_audience` | The gateway API audience expected in the access token's `aud` claim |
| `api_required_scope` | The delegated scope name in `scp`, such as `access_as_user` |
| `api_allowed_client_ids` | Application (client) IDs of the approved callers, not the gateway API |

For `api_audience`, v2 access tokens use the gateway API's application (client)
ID; v1 tokens commonly use its Application ID URI. Match the access token
format configured for your API rather than assuming it equals the scope URI.

A desktop client can keep its MSAL/native-broker sign-in and request a token for
the gateway scope, such as `api://<gateway-api-client-id>/access_as_user`.
Use an access token, not an ID token or a token intended for Microsoft Graph.
Use a public-client flow with PKCE; do not embed a shared secret.

The policy in [web-search.xml.tftpl](../policies/web-search.xml.tftpl) uses
`validate-azure-ad-token` to enforce the tenant, audience, allowed client, and
delegated `scp` claim. Application-only tokens do not satisfy that scope check.
APIM's allowlist and Entra permission/consent are separate requirements; neither
replaces user authorization.

### Azure CLI development calls

The [README examples](../README.md#call-the-api) acquire a token with Azure CLI,
which is a different client application from your desktop app. The API owner
must grant/consent to its delegated permission and explicitly include the CLI
client's application ID in `api_allowed_client_ids`. Do not broaden a production
allowlist just to enable a development test.

## Managed identity binding

After deployment, bind the APIM identity's **Application (Client) ID** to your
provisioned Web IQ **AppID** in the Web IQ Portal. The
[lookup command](../README.md#bind-the-managed-identity) converts Terraform's
object/principal ID to that application ID; directory read permissions may be
required.

Terraform does not automate the binding. Azure RBAC does not grant Web IQ
access; the sample's role assignment is for Application Insights logging only.
Use a dedicated AppID with one mapping for APIM's identity. This sample does not
select among multiple AppIDs.

APIM replaces the user's authorization header with its system-assigned managed
identity token for `https://api.microsoft.ai`. Token acquisition failures do not
fall back to caller credentials.

## Authentication failures

- **Token acquisition fails:** check the tenant, requested gateway scope, client permissions, and consent/preauthorization.
- **APIM rejects the token:** check tenant, audience, allowed client application ID, and delegated scope. A Graph token, ID token, or application-only token is not sufficient.
- **Web IQ returns 401/403:** check the managed identity's application ID, Portal binding, AppID provisioning, and Web IQ entitlements. Repeated desktop sign-ins do not fix backend provisioning.

## References

- [Expose a delegated API scope](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-configure-app-expose-web-apis)
- [APIM Entra token validation policy](https://learn.microsoft.com/en-us/azure/api-management/validate-azure-ad-token-policy)
- [Web IQ authentication guidance](https://learn.microsoft.com/en-us/web-iq/get-started/authentication)
