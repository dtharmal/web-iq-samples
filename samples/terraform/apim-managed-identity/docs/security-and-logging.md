# Security and logging

This is an educational gateway sample. Validate its behavior and add
workload-specific production controls before using it with real data.

## Query redaction

The section marked `Sensitive query inspection and redaction` in the
[policy template](../policies/web-search.xml.tftpl) replaces:

- SSN-like values such as `123-45-6789`, `123 45 6789`, or `123456789`;
- payment-card-like sequences of 13-19 digits, optionally separated by spaces or hyphens.

Regex matching can produce false positives and cannot identify every sensitive
value. Only `query` is inspected; other JSON fields are forwarded unchanged.
Use synthetic or approved public queries. Use an approved DLP service and
organization-specific policies if sensitive data might be submitted.

## Production controls

- **Authorization:** `deptId` is caller-supplied attribution, not an authorization boundary. Derive or validate allowed departments from trusted claims or an authorization service. An approved client and delegated scope do not replace user access controls.
- **Credentials:** APIM replaces frontend authorization with its managed identity token and removes caller-provided `x-apikey`, `Ocp-Apim-Subscription-Key`, and `deptId` headers before forwarding.
- **Networking:** gateway and Web IQ endpoints are public HTTPS endpoints. The sample disables legacy SSL/TLS versions. Review private connectivity and tier compatibility for your workload.
- **Administration:** only trusted administrators should edit policies or backend URLs; they can act as the managed identity. Use only the required Web IQ binding and resource-scoped logging role.
- **Existing gateways:** inherited/global policies can independently log, cache, forward, or transform data. Review them before adapting this sample.
- **Operations:** add appropriate rate limits, quotas, alerts, and budgets. `Developer_1` has no production SLA; APIM, monitoring, and Web IQ can incur separate charges.
- **Terraform state:** configured variables, state, and plans can contain sensitive resource details. Keep them out of source control and use an access-controlled, encrypted remote backend with locking for shared deployments.
- **Retrieved content:** the sample does not cache responses. Respect the [Web IQ Terms of Use](https://www.microsoft.com/en-us/webiq/webiq-legal) and review responses before sharing.

## Logging

Terraform creates Log Analytics, workspace-based Application Insights, and an
APIM logger authenticated with the gateway's managed identity. API-level
telemetry is **disabled by default**.

Set `enable_telemetry = true` and apply to collect frontend requests, backend
dependencies, failures, and `webiq-gateway` traces. Collection includes:

- request/dependency URLs, status codes, timings, content types, and correlation IDs;
- custom-trace metadata: the validated caller-supplied department label, whether redaction occurred, and the APIM correlation ID.

All request/response body logging limits are zero. Only the `content-type`
header is logged; raw/sanitized query text is not included in the custom trace.
Nevertheless, request URLs and diagnostic errors can contain caller-controlled
data. **Never put credentials or query text in URLs**, and use non-sensitive
department codes rather than names, secrets, or personal information.

Telemetry stays in your subscription under Azure Monitor access controls.
Retention defaults to 30 days through `log_retention_days`.

To opt out, set `enable_telemetry = false` and apply. This removes the sample's
API diagnostic, including error and custom-trace collection. It does **not**:

- delete existing data or monitoring resources;
- disable independently configured Azure diagnostics; or
- disable Web IQ's own service-side logging.

Use your Azure Monitor retention/purge process to remove collected data when
required. Review the [Web IQ privacy guidance](https://learn.microsoft.com/en-us/web-iq/resources/faq#privacy-and-data-handling)
for service-side data handling.

For regulated or sensitive workloads, ask your Microsoft account team about
Web IQ Zero Query Logging, which suppresses query text from service telemetry.
It is not self-service and requires approval for each use case. This is separate
from APIM telemetry and is not enabled by this sample.

## Post-deployment checks

The [offline Terraform checks](../README.md#validation) use mocked providers;
they do not compile APIM policy expressions or validate live services.
Use synthetic inputs to verify that:

- missing/invalid tokens, wrong tenant/audience/client, missing delegated scope, and application-only tokens are rejected without reaching Web IQ;
- missing/invalid `deptId`, malformed JSON, empty queries, and oversized requests are rejected;
- a valid request succeeds after the managed-identity binding, while missing backend access fails without falling back to caller credentials; and
- redaction changes the forwarded query, and enabled diagnostics do not contain query, token, or response content.
