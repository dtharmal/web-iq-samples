// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;
using Azure.Core;
using Azure.Identity;

namespace WebIqScopes;

public static class Cli
{
    public const string Help = """
        Web IQ named scopes (run from the sample directory)
        dotnet run --project src/WebIqScopes -- <command>
          discover
          list [--page-size 1..100]
          get <name>
          create <name> --definition <json-file> [--allow-all-sites]
          replace <name> --definition <json-file> --if-match <quoted-etag> [--allow-all-sites]
          delete <name> --if-match <quoted-etag>
          search <name> <query> [--auth api-key|entra] [--app-id <webiq-appid>]
        Management uses certificate Entra auth and an explicit WEBIQ_BILLING_ACCOUNT_ID.
        Search defaults to WEBIQ_API_KEY; --app-id is valid only for Entra search.
        """;

    public static string Required(Func<string, string?> environment, string name)
    {
        string? value = environment(name);
        if (string.IsNullOrWhiteSpace(value) || value.StartsWith('<'))
            throw new SampleException($"Set {name} to a non-placeholder value before running.");
        return value;
    }

    public static async Task<int> RunAsync(string[] args, TextWriter output, TextWriter errors,
        Func<string, string?>? environment = null, HttpClient? http = null,
        Func<Task<string>>? acquireToken = null)
    {
        environment ??= Environment.GetEnvironmentVariable;
        try
        {
            if (args.Length == 0 || args.SequenceEqual(["--help"]) ||
                (args.Length == 2 && args[1] == "--help"))
            {
                await output.WriteLineAsync(Help);
                return 0;
            }
            string command = args[0];
            string[] allowed = command switch
            {
                "discover" or "get" => [],
                "list" => ["--page-size"],
                "create" => ["--definition", "--allow-all-sites"],
                "replace" => ["--definition", "--if-match", "--allow-all-sites"],
                "delete" => ["--if-match"],
                "search" => ["--auth", "--app-id"],
                _ => throw new SampleException("Unknown command. Run with --help.")
            };
            List<string> positional = [];
            Dictionary<string, string> options = [];
            for (int i = 1; i < args.Length; i++)
            {
                string arg = args[i];
                if (!arg.StartsWith("--", StringComparison.Ordinal))
                {
                    positional.Add(arg);
                    continue;
                }
                if (!allowed.Contains(arg) || options.ContainsKey(arg))
                    throw new SampleException("Unknown or duplicate option. Run with --help.");
                if (arg == "--allow-all-sites")
                    options.Add(arg, "true");
                else if (++i < args.Length && !args[i].StartsWith("--", StringComparison.Ordinal))
                    options.Add(arg, args[i]);
                else
                    throw new SampleException("An option value is missing. Run with --help.");
            }
            int count = command switch { "discover" or "list" => 0, "search" => 2, _ => 1 };
            if (positional.Count != count)
                throw new SampleException("Unexpected positional arguments. Run with --help.");
            string name = count == 0 ? "" : ScopesClient.ScopeName(positional[0]);
            string Option(string key) => options.TryGetValue(key, out var value)
                ? value : throw new SampleException($"Missing {key}. Run with --help.");
            string? etag = command is "replace" or "delete" ? ScopesClient.StrongEtag(Option("--if-match")) : null;
            int pageSize = 50;
            if (options.TryGetValue("--page-size", out var size) && (!int.TryParse(size, out pageSize) || pageSize is < 1 or > 100))
                throw new SampleException("Page size must be between 1 and 100.");
            ScopeDefinition? definition = null;
            if (command is "create" or "replace")
            {
                try
                {
                    using var document = JsonDocument.Parse(await File.ReadAllTextAsync(Option("--definition")));
                    definition = ScopeDefinition.Parse(document.RootElement);
                }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or ArgumentException)
                {
                    throw new SampleException("Cannot read the definition file as JSON.");
                }
                if (definition.IncludeSites.Length == 0 && !options.ContainsKey("--allow-all-sites"))
                    throw new SampleException("Empty includeSites requires an explicit --allow-all-sites.");
            }
            string auth = options.GetValueOrDefault("--auth", "api-key");
            string? appId = options.GetValueOrDefault("--app-id");
            if (command == "search" && (auth is not ("api-key" or "entra") ||
                string.IsNullOrWhiteSpace(positional[1]) || (appId is not null && (auth == "api-key" || string.IsNullOrWhiteSpace(appId)))))
                throw new SampleException("Check the query, --auth, and Entra-only --app-id.");
            string? account = command is "discover" or "search" ? null : Required(environment, "WEBIQ_BILLING_ACCOUNT_ID");
            bool apiKey = command == "search" && auth == "api-key";
            string credential = apiKey ? Required(environment, "WEBIQ_API_KEY") :
                await (acquireToken is null ? CertificateTokenAsync(environment) : acquireToken());
            using var ownedHttp = http is null ? new HttpClient(new HttpClientHandler { AllowAutoRedirect = false })
                { Timeout = TimeSpan.FromSeconds(30) } : null;
            var client = new ScopesClient(http ?? ownedHttp!, credential, apiKey);
            var jsonOptions = new JsonSerializerOptions { WriteIndented = true };
            switch (command)
            {
                case "discover":
                    var customers = await client.DiscoverAsync();
                    await output.WriteLineAsync(JsonSerializer.Serialize(customers, jsonOptions));
                    if (customers.GetArrayLength() == 0)
                        await output.WriteLineAsync("No authorized resources. Check workload registration and CustomerApiAccess.");
                    break;
                case "list":
                    await output.WriteLineAsync(JsonSerializer.Serialize(await client.ListAsync(account!, pageSize), jsonOptions));
                    break;
                case "get":
                    var snapshot = await client.GetAsync(account!, name);
                    await output.WriteLineAsync(JsonSerializer.Serialize(new
                    {
                        name, includeSites = snapshot.Definition.IncludeSites,
                        excludeSites = snapshot.Definition.ExcludeSites, etag = snapshot.Etag
                    }, jsonOptions));
                    break;
                case "create":
                case "replace":
                    string nextEtag = command == "create"
                        ? await client.CreateAsync(account!, name, definition!)
                        : await client.ReplaceAsync(account!, name, definition!, etag!);
                    await output.WriteLineAsync($"Authoring write succeeded. ETag: {nextEtag}");
                    await output.WriteLineAsync("Serving propagation is asynchronous; this is not a readiness signal.");
                    break;
                case "delete":
                    await client.DeleteAsync(account!, name, etag!);
                    await output.WriteLineAsync("Scope deleted from authoring storage; serving propagation is asynchronous.");
                    break;
                case "search":
                    var outcome = await client.SearchAsync(name, positional[1], appId);
                    string status = outcome.ScopeApplied is true ? "scopeApplied: true" :
                        "the service did not report whether the scope was applied";
                    await output.WriteLineAsync($"Search returned {outcome.ResultCount} web results; {status}. Retrieved content was not printed.");
                    break;
            }
            return 0;
        }
        catch (SampleException error)
        {
            await errors.WriteLineAsync("Error: " + error.Message);
            return 1;
        }
        catch (Exception error) when (error is AuthenticationFailedException or CryptographicException)
        {
            await errors.WriteLineAsync("Error: Entra authentication failed. Check certificate settings and service access; credential details were not logged.");
            return 1;
        }
    }

    public static async Task<string> CertificateTokenAsync(Func<string, string?> environment)
    {
        string tenant = Required(environment, "AZURE_TENANT_ID");
        string client = Required(environment, "AZURE_CLIENT_ID");
        string path = Required(environment, "AZURE_CLIENT_CERTIFICATE_PATH");
        if (!Guid.TryParse(tenant, out _) || !Guid.TryParse(client, out _))
            throw new SampleException("AZURE_TENANT_ID and AZURE_CLIENT_ID must be directory/application GUIDs.");
        X509Certificate2 certificate;
        try
        {
            certificate = X509CertificateLoader.LoadPkcs12FromFile(path,
                environment("AZURE_CLIENT_CERTIFICATE_PASSWORD"), X509KeyStorageFlags.EphemeralKeySet);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or CryptographicException or ArgumentException)
        {
            throw new SampleException("Cannot load the private-key PFX. Check its path and existing password.");
        }
        using (certificate)
        {
            if (!certificate.HasPrivateKey)
                throw new SampleException("The PFX must contain the application's private key.");
            using var rsa = certificate.GetRSAPrivateKey();
            if (rsa is null)
                throw new SampleException("The PFX must contain an RSA private key.");
            var options = new ClientCertificateCredentialOptions();
            options.Diagnostics.IsLoggingContentEnabled = false;
            var credential = new ClientCertificateCredential(tenant, client, certificate, options);
            return (await credential.GetTokenAsync(new TokenRequestContext([ScopesClient.TokenScope]))).Token;
        }
    }
}
