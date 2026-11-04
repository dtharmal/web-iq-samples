# Web IQ Samples

Runnable examples for building applications with Microsoft Web IQ.

## Project status

This repository includes the runnable Market briefing, Foundry Voice Agent, and
APIM managed identity gateway samples listed below. These are educational samples,
not production-ready reference architectures. Review each sample's security, cost, and operational
requirements before use or deployment. No third-party source code is vendored;
the samples install their dependencies separately.

## Samples

Samples are organized by implementation language or infrastructure technology:

```text
samples/
  <language-or-technology>/
    <sample-name>/
```

Each sample is self-contained and includes its own prerequisites, configuration,
run instructions, and validation steps.

| Sample | Language | Description | Telemetry and notices |
|---|---|---|---|
| [Web IQ + Foundry Voice Agent](samples/python/foundry-voice-agent/) | Python, TypeScript | Native Microsoft Foundry voice agent that decides when to search Web IQ news and shows cited sources after speaking. | Bounded local timing/count logs are enabled by default; set `PERF_LOG_ENABLED=0` to disable. [Privacy and retention guidance](samples/python/foundry-voice-agent/README.md#telemetry). No telemetry is sent to repository maintainers. |
| [Market briefing](samples/python/market-briefing/) | Python | Uses Web IQ web and news passages to generate a cited Markdown briefing. Azure OpenAI is the example model provider; other chat models require small integration changes. Model usage is a separate cost. Supports API-key or certificate-based Microsoft Entra authentication for Web IQ. | Local stderr diagnostics include search queries and returned URLs; [review and redact before sharing](samples/python/market-briefing/docs/troubleshooting.md#share-diagnostics-safely). |
| [APIM managed identity gateway](samples/terraform/apim-managed-identity/README.md) | Terraform | Protect Web IQ with Entra authentication, query redaction, and optional operational telemetry. | Azure Monitor telemetry stays in your subscription and is disabled by default. [Collection, retention, and opt-out guidance](samples/terraform/apim-managed-identity/docs/security-and-logging.md#logging). |

Web IQ service-side data handling is separate from sample diagnostics; see the
[Web IQ privacy guidance](https://learn.microsoft.com/en-us/web-iq/resources/faq#privacy-and-data-handling).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for repository structure, sample
requirements, validation expectations, and public-content restrictions.

This project has adopted the [Microsoft Open Source Code of Conduct](CODE_OF_CONDUCT.md).

## Support

See [SUPPORT.md](SUPPORT.md) for questions, bug reports, and feature requests.

## Security

Do not report security vulnerabilities through public GitHub issues. See
[SECURITY.md](SECURITY.md) for reporting guidance.

## License

This project is licensed under the [MIT License](LICENSE.txt).

## Trademarks

This project may contain trademarks or logos for projects, products, or services.
Authorized use of Microsoft trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks).
Use of Microsoft trademarks or logos in modified versions of this project must
not cause confusion or imply Microsoft sponsorship. Any use of third-party
trademarks or logos are subject to those third-party's policies.
