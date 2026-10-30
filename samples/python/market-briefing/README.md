# Web IQ Market Briefing

Generate a cited Markdown market briefing from current web and news passages. Web IQ supplies retrieved evidence rather than relying on the model's training knowledge alone; Azure OpenAI synthesizes it into a report with clickable citations and a numbered Sources list.

Azure OpenAI is the example model provider, not a Web IQ requirement. Other chat models require small changes to the model client and response handling, not just configuration. Model usage is a separate cost from Web IQ usage.

## Prerequisites

- Python 3.11 or later
- [A Web IQ API key](https://learn.microsoft.com/en-us/web-iq/get-started/quickstart) with both Web and News access, or a provisioned application using [certificate-based Microsoft Entra authentication](docs/authentication.md)
- An Azure OpenAI resource with a deployed model and an API key

## Get started

Run these commands from this sample's directory. API-key authentication is the default.

### Windows (PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
$env:WEBIQ_API_KEY = "<your Web IQ API key>"
$env:AZURE_OPENAI_API_KEY = "<your Azure OpenAI API key>"
```

### macOS/Linux (Bash)

Ensure `python3` is Python 3.11 or later.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
export WEBIQ_API_KEY="<your Web IQ API key>"
export AZURE_OPENAI_API_KEY="<your Azure OpenAI API key>"
```

### Configure and run

Replace the Azure OpenAI placeholders in `config.json`. Use the deployment name, which may differ from the underlying model name:

```json
{
  "azure_openai": {
    "endpoint": "https://<your-resource-name>.openai.azure.com/",
    "deployment": "<your-deployment-name>",
    "api_version": "<your-api-version>"
  }
}
```

Generate the report in PowerShell:

```powershell
webiq-market-briefing "North American energy market" --output reports\energy-briefing.md
```

Or in Bash:

```bash
webiq-market-briefing "North American energy market" --output reports/energy-briefing.md
```

Open the generated Markdown in your editor. Keep credentials out of `config.json` and source control. Azure OpenAI uses its API key in both Web IQ authentication modes.

## How it works

1. Builds date-bounded queries, unless you supply explicit queries.
2. Retrieves Web IQ web and news passages with a 3,000-character limit per result.
3. Selects up to 18 unique source URLs round-robin across each query's Web and News results, preserving rank within each group. Duplicate URLs are skipped and empty or exhausted groups contribute no further results, so other groups can fill the remaining slots. Fewer than 18 unique, usable sources means fewer are sent to the model.
4. Asks the model to synthesize only from that evidence and checks numeric citations and response completion.
5. Writes a Markdown briefing with linked citations and source titles, full URLs, and available publisher/last-updated metadata. The Sources list does not include passage excerpts.

## Important limitations

- Citation validation checks that references exist and identify supplied sources, **not** that every factual claim is cited or supported. Review the original sources before relying on a report.
- Passages are extracts, not full documents; the sample does not fetch full pages or infer missing content. Date-bounded queries do not guarantee every result was published within the reporting window.
- Retrieved content is untrusted evidence. Display escaping and rejection of inline links, HTML, and triple-backtick code fences in model output are not a general-purpose Markdown sanitizer.
- Diagnostics are enabled automatically on stderr; progress goes to stdout. **Logs contain search queries and full returned URLs**, potentially including sensitive terms or parameters. Review and redact logs and reports before sharing.
- Retries are bounded but a run can take several minutes. Retrieval or generation failures leave an existing output file unchanged.

See [Troubleshooting](docs/troubleshooting.md) for retry rules, log capture, and common failures.

## Options

Run `webiq-market-briefing --help` for the complete list.

| Option | Purpose | Default |
| --- | --- | --- |
| `--lookback-days N` | Reporting window | `7` |
| `--max-results N` | Results per Web IQ search | `5` |
| `--query TEXT` | Replace generated queries; repeat for multiple queries | Generated queries |
| `--config PATH` | Configuration file | `config.json` |
| `--region REGION` | Web IQ market region | `US` |
| `--webiq-auth MODE` | Web IQ authentication: `api-key` or `entra` (certificate) | `api-key` |
| `--output PATH` | Markdown output file | `market-briefing.md` |

For example, supply your own queries in either shell:

```text
webiq-market-briefing "Energy market" --query "North American natural gas price developments" --query "US electricity market reliability developments"
```

## Contribute

Install in editable mode so source changes take effect without reinstalling, then run the offline tests:

```text
python -m pip install -e ".[dev]"
pytest
```

Existing reports are not regenerated when code changes. If you used the regular install, reinstall before running updated code.

## Further guidance

- [Microsoft Entra authentication](docs/authentication.md): provisioning, certificate creation, passwords, and production credential handling.
- [Troubleshooting](docs/troubleshooting.md): log capture, support diagnostics, retries, and report failures.
