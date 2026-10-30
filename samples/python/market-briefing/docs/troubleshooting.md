# Troubleshooting Market Briefing

[Back to the sample](../README.md)

Retrieval or generation failures stop the run and leave an existing output file unchanged. Check the service, status, error code, and correlation IDs in stderr before retrying.

## Common failures

| Symptom | Action |
| --- | --- |
| HTTP 400 `AuthAppIdSelectionRequired` | The application identity maps to more than one Web IQ AppID. This sample does not select among AppIDs; use an application identity mapped to one provisioned AppID with both Web and News entitlements. See [Web IQ error handling](https://learn.microsoft.com/en-us/web-iq/reference/error-handling) for the service's AppID-selection guidance. |
| HTTP 401 or 403 | Check credentials and service provisioning. For certificate mode, verify the application maps to one Web IQ AppID with both Web and News entitlements; see [authentication guidance](authentication.md). Azure OpenAI uses its own API key. |
| Certificate deserialization failure | Verify that the path identifies the private-key PFX/PEM, not the public CER, and that its existing password is available in this process. See [certificate setup](authentication.md#register-a-certificate-and-run-locally-windows-powershell). |
| HTTP 429 or 430 | Wait for service retry guidance before rerunning. |
| Exhausted transient failures | Retain diagnostic IDs and retry history for support; do not repeatedly restart the same failing request. |
| Citation validation or incomplete generation | No new report is written. Review the failure and rerun generation as appropriate; an older report at the same path has not been refreshed. |

The prompt asks for a citation immediately after every factual claim, separate citation tokens for multiple sources, and explicit uncertainty. Validation rejects missing or out-of-range numeric citations and incomplete model responses, but does not prove claim-to-source support. Review the original sources before relying on a generated report.

## Capture diagnostics

Logs are enabled automatically; no logging switch is needed. Timestamped diagnostics go to stderr and progress messages go to stdout. These examples use PowerShell from the sample directory with its virtual environment active.

Create the destination directory **before** redirecting stderr to a file there:

```powershell
New-Item -ItemType Directory -Path reports -Force | Out-Null
webiq-market-briefing "Energy market" --output reports\briefing.md 2> reports\diagnostics.log
```

To display and save both progress and diagnostics:

```powershell
webiq-market-briefing "Energy market" --output reports\briefing.md 2>&1 |
  Tee-Object -FilePath market-briefing.log
```

### What the logs contain

- Web IQ logs each submitted web/news query and every returned source URL, including duplicates and results later excluded from model evidence. The `query` and `source_url` fields use JSON quoting to escape line breaks and other control characters.
- Each Web IQ attempt logs its operation, status, trace ID, request ID when supplied, and machine-readable error-response code. Retry entries include the retry number and delay.
- Azure OpenAI logs the final response request ID, or the final failure's status, request ID, and error code. Missing IDs appear as `-`.
- Codes and identifiers are bounded and restricted to single-line characters.

### Share diagnostics safely

Include the service, UTC failure time, HTTP status, error code, correlation IDs, and retry history when contacting support.

**Queries and full URLs can contain sensitive search terms or URL parameters.** Review and redact logs before sharing. Reports also contain the topic and source references and need the same review.

Error messages, arbitrary response bodies, model prompts, passages, API keys, tokens, and authentication headers are deliberately excluded from logs. Do not enable HTTP wire logging or share credentials to obtain more detail.

## Retry and timeout reference

Web IQ calls have a 30-second request timeout and at most two retries, managed by the sample without stacking SDK retries. Connection failures, timeouts, and HTTP 408, 429, 430, 500, 502, 503, and 504 use exponential backoff with jitter (0.5-1 seconds, then 1-2 seconds). A valid response-body `retryAfter` duration such as `3s` increases that delay. Guidance longer than 60 seconds fails immediately instead of retrying too early. Invalid input, authentication, and permission errors are not retried. Exhausting retries stops generation rather than silently omitting a failed search.

Azure OpenAI uses its SDK's two retries with exponential backoff for connection failures, timeouts, HTTP 408, 409, 429, and 5xx errors, with a 60-second request timeout. See the [OpenAI Python SDK retry documentation](https://github.com/openai/openai-python#retries).

These are per-request timeouts, not an overall briefing deadline; multiple queries and retries can take several minutes.
