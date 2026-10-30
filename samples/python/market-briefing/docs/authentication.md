# Microsoft Entra authentication for Market Briefing

[Back to the sample](../README.md)

Use this guide when running Market Briefing with a certificate-based application identity instead of a Web IQ API key. Complete the sample's installation and `config.json` setup first; run the commands below from the sample directory with its virtual environment active.

## Provision the application

The application identity must resolve to one provisioned Web IQ AppID with **both Web and News entitlements**. This sample does not select among multiple AppIDs. Obtaining a token alone does not prove endpoint access; for a 401 or 403, check the application mapping and both entitlements rather than repeatedly signing in.

Azure OpenAI remains separately API-key authenticated in either Web IQ mode.

## Configure certificate authentication

The sample explicitly uses Azure Identity's `CertificateCredential` and requests the scope `https://api.microsoft.ai/.default`. Entra mode reads the following environment variables:

| Variable | Purpose |
| --- | --- |
| `AZURE_TENANT_ID` | Directory (tenant) ID of the app registration |
| `AZURE_CLIENT_ID` | Application (client) ID, not the application's object ID |
| `AZURE_CLIENT_CERTIFICATE_PATH` | Path to a PEM or PKCS12/PFX file containing the RSA certificate and its private key |
| `AZURE_CLIENT_CERTIFICATE_PASSWORD` | Password for an encrypted certificate file; omit only when the file is not encrypted |

There is no credential-chain fallback: Azure CLI sign-in, managed identity, and `AZURE_CLIENT_SECRET` are not used. `WEBIQ_API_KEY` is not read in Entra mode. Missing required settings or an unreadable/invalid certificate stop the run rather than falling back to another identity.

## Register a certificate and run locally (Windows PowerShell)

Use your organization's approved certificate issuance process. For local testing only, if policy permits, this PowerShell example creates a self-signed RSA certificate valid for 90 days. It writes outside the repository and prompts for a new export password without placing it in command history:

```powershell
$certDir = Join-Path $env:USERPROFILE ".webiq-certs"
New-Item -ItemType Directory -Path $certDir -Force | Out-Null
$cert = New-SelfSignedCertificate `
  -Subject "CN=WebIQ-MarketBriefing-Dev" `
  -CertStoreLocation "Cert:\CurrentUser\My" `
  -KeyExportPolicy Exportable -KeySpec Signature `
  -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 `
  -NotAfter (Get-Date).AddDays(90)
$password = Read-Host "Choose a PFX export password" -AsSecureString
Export-PfxCertificate -Cert $cert `
  -FilePath "$certDir\webiq-dev.pfx" -Password $password | Out-Null
Export-Certificate -Cert $cert `
  -FilePath "$certDir\webiq-dev.cer" | Out-Null
```

In Microsoft Entra ID, open **App registrations > your application > Certificates & secrets > Certificates > Upload certificate** and upload only `webiq-dev.cer`. Never upload or share the PFX or private key.

Set the following in the same PowerShell session that runs the sample. If using an existing PFX, enter its existing export password, not a new password:

```powershell
$env:AZURE_TENANT_ID = "<directory-tenant-id>"
$env:AZURE_CLIENT_ID = "<application-client-id>"
$env:AZURE_CLIENT_CERTIFICATE_PATH = Join-Path `
  $env:USERPROFILE ".webiq-certs\webiq-dev.pfx"
$password = Read-Host "Existing PFX password" -AsSecureString
$env:AZURE_CLIENT_CERTIFICATE_PASSWORD = (
  [System.Net.NetworkCredential]::new("", $password).Password
)
$env:AZURE_OPENAI_API_KEY = "<your Azure OpenAI API key>"
webiq-market-briefing "Energy market" --webiq-auth entra
```

The password decrypts the local private key; it is not your Microsoft account password or an Entra client secret. Azure Identity receives it explicitly from the sample and handles token acquisition and in-memory token caching. No interactive sign-in is required. After finishing all runs in this shell, clear the process-local password:

```powershell
Remove-Item Env:\AZURE_CLIENT_CERTIFICATE_PASSWORD -ErrorAction SilentlyContinue
```

Set it again before the next run if you cleared it or opened a new shell. `Failed to deserialize certificate in PEM or PKCS12 format` means the local file could not be loaded: verify the path points to the private-key PFX/PEM, not the public CER, and that the existing password is available in the process.

## Production certificate handling

- Use a dedicated, least-privileged application identity and an organization-approved certificate, not the development self-signed certificate.
- Deliver the private-key file through your deployment's secret mechanism, restrict file access to the workload identity, and supply its password through a protected runtime secret setting. Environment variables are a delivery mechanism, not a secret vault; do not print them, place secrets in command-line arguments, or bake credentials into images.
- Never put PFX/PEM private keys, passwords, tokens, or API keys in `config.json`, source control, reports, or logs. The sample does not retrieve certificates from a vault or renew them.
- Monitor expiration and rotate before expiry: register the new public certificate while the old one is still valid, deploy the corresponding private key/password, restart the process and verify both Web and News access, then remove the old credential after all callers have switched.
- Managed identity is a separate production authentication option, but this sample's `entra` mode intentionally supports certificate authentication only. It does not silently switch to a host identity. Azure OpenAI remains API-key authenticated.

See Microsoft's [CertificateCredential reference](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.certificatecredential), [development certificate creation guide](https://learn.microsoft.com/en-us/entra/identity-platform/howto-create-self-signed-certificate), and [application credential management guidance](https://learn.microsoft.com/en-us/entra/identity-platform/security-best-practices-for-app-registration#credentials-and-secrets).
