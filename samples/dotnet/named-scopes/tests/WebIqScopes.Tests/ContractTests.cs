// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

using System.Net;
using System.Text;
using System.Text.Json;
using WebIqScopes;
using Xunit;

namespace WebIqScopes.Tests;

public sealed class ContractTests
{
    private const string Account = "demo-account";
    private const string Collection = "/v1/customer/billing-accounts/demo-account/scopes";
    private const string Etag = "\"opaque-version\"";
    private static readonly ScopeDefinition Sites = new(["example.com/docs"], ["example.com/docs/archive"]);

    private sealed record Captured(string Method, Uri Url, Dictionary<string, string> Headers, string Body);
    private sealed class Handler(Func<Captured, HttpResponseMessage> respond) : HttpMessageHandler
    {
        public List<Captured> Requests { get; } = [];
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var captured = new Captured(request.Method.Method, request.RequestUri!,
                request.Headers.ToDictionary(h => h.Key, h => string.Join(",", h.Value), StringComparer.OrdinalIgnoreCase),
                request.Content is null ? "" : await request.Content.ReadAsStringAsync(cancellationToken));
            Requests.Add(captured);
            return respond(captured);
        }
    }
    private static HttpResponseMessage Response(int status, string body = "{}", string? etag = null)
    {
        var response = new HttpResponseMessage((HttpStatusCode)status)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json")
        };
        if (etag is not null)
            response.Headers.TryAddWithoutValidation("ETag", etag);
        return response;
    }

    [Fact]
    public async Task ManagementLifecyclePreservesExactRoutesBodiesAndEtags()
    {
        using var handler = new Handler(r => r.Method switch
        {
            "POST" => Response(201, etag: Etag),
            "GET" => Response(200, JsonSerializer.Serialize(Sites), Etag),
            "PUT" => Response(200, etag: "\"new-version\""),
            _ => Response(204, "not-json")
        });
        using var http = new HttpClient(handler);
        var client = new ScopesClient(http, "PRIVATE-TOKEN");
        Assert.Equal(Etag, await client.CreateAsync(Account, "ProductDocs", Sites));
        var snapshot = await client.GetAsync(Account, "ProductDocs");
        Assert.Equal(Etag, snapshot.Etag);
        Assert.Equal(Sites.IncludeSites, snapshot.Definition.IncludeSites);
        Assert.Equal("\"new-version\"", await client.ReplaceAsync(Account, "ProductDocs", Sites, Etag));
        await client.DeleteAsync(Account, "ProductDocs", "\"new-version\"");
        Assert.Equal(["POST", "GET", "PUT", "DELETE"], handler.Requests.Select(r => r.Method));
        Assert.Equal(Collection, handler.Requests[0].Url.AbsolutePath);
        Assert.All(handler.Requests.Skip(1), r => Assert.Equal(Collection + "/ProductDocs", r.Url.AbsolutePath));
        Assert.All(handler.Requests, r =>
        {
            Assert.Equal("https", r.Url.Scheme);
            Assert.Equal("api.microsoft.ai", r.Url.Host);
            Assert.Equal("Bearer PRIVATE-TOKEN", r.Headers["Authorization"]);
            Assert.False(r.Headers.ContainsKey("x-apikey"));
        });
        using var create = JsonDocument.Parse(handler.Requests[0].Body);
        Assert.Equal("ProductDocs", create.RootElement.GetProperty("name").GetString());
        Assert.Equal(3, create.RootElement.EnumerateObject().Count());
        using var replace = JsonDocument.Parse(handler.Requests[2].Body);
        Assert.Equal(2, replace.RootElement.EnumerateObject().Count());
        Assert.Equal(Etag, handler.Requests[2].Headers["If-Match"]);
        Assert.Equal("\"new-version\"", handler.Requests[3].Headers["If-Match"]);
        Assert.Empty(handler.Requests[3].Body);
    }

    [Fact]
    public async Task DiscoveryDoesNotSelectOrInterpretLegacyPermissions()
    {
        const string body = """[{"customerId":"demo-customer","permissions":{"canWrite":false},"billingAccounts":[{"billingAccountId":"demo-account","permissions":{"canWrite":false}}]}]""";
        using var handler = new Handler(_ => Response(200, body));
        using var http = new HttpClient(handler);
        using var output = new StringWriter();
        using var errors = new StringWriter();
        Assert.Equal(0, await Cli.RunAsync(["discover"], output, errors, _ => null, http, () => Task.FromResult("token")));
        using var document = JsonDocument.Parse(output.ToString());
        Assert.Equal("demo-account", document.RootElement[0].GetProperty("billingAccounts")[0].GetProperty("billingAccountId").GetString());
        Assert.Equal("/v1/customer/authorized-resources", Assert.Single(handler.Requests).Url.AbsolutePath);
        Assert.Empty(errors.ToString());
    }

    [Fact]
    public async Task EmptyDiscoveryIsReported()
    {
        using var handler = new Handler(_ => Response(200, "[]"));
        using var http = new HttpClient(handler);
        using var output = new StringWriter();
        Assert.Equal(0, await Cli.RunAsync(["discover"], output, TextWriter.Null, _ => null, http, () => Task.FromResult("token")));
        Assert.Contains("No authorized resources", output.ToString());
    }

    [Fact]
    public async Task PaginationRoundTripsOpaqueTokenAndMetadata()
    {
        const string token = "opaque+/= ?&page=2";
        int calls = 0;
        using var handler = new Handler(_ => ++calls == 1
            ? Response(200, JsonSerializer.Serialize(new { items = new[] { new { name = "One" } }, nextContinuationToken = token }))
            : Response(200, """{"items":[{"name":"Two"}],"nextContinuationToken":null}"""));
        using var http = new HttpClient(handler);
        var items = await new ScopesClient(http, "token").ListAsync(Account, 100);
        Assert.Equal(2, items.Count);
        Assert.Equal("?pageSize=100", handler.Requests[0].Url.Query);
        Assert.Equal("?pageSize=100&continuationToken=" + Uri.EscapeDataString(token), handler.Requests[1].Url.Query);
    }

    [Fact]
    public async Task RepeatedContinuationFailsExplicitly()
    {
        using var handler = new Handler(_ => Response(200, """{"items":[],"nextContinuationToken":"same"}"""));
        using var http = new HttpClient(handler);
        await Assert.ThrowsAsync<SampleException>(() => new ScopesClient(http, "token").ListAsync(Account));
        Assert.Equal(2, handler.Requests.Count);
    }

    [Theory]
    [InlineData("*")]
    [InlineData("W/\"weak\"")]
    [InlineData("unquoted")]
    [InlineData("\"a\"\r\nInjected: true")]
    public async Task InvalidEtagsFailBeforeNetwork(string etag)
    {
        using var handler = new Handler(_ => throw new InvalidOperationException("No request expected"));
        using var http = new HttpClient(handler);
        var client = new ScopesClient(http, "token");
        await Assert.ThrowsAsync<SampleException>(() => client.ReplaceAsync(Account, "ProductDocs", Sites, etag));
        await Assert.ThrowsAsync<SampleException>(() => client.DeleteAsync(Account, "ProductDocs", etag));
        Assert.Empty(handler.Requests);
    }

    [Theory]
    [InlineData(400)]
    [InlineData(401)]
    [InlineData(403)]
    [InlineData(408)]
    [InlineData(409)]
    [InlineData(412)]
    [InlineData(428)]
    [InlineData(429)]
    [InlineData(503)]
    [InlineData(302)]
    public async Task ServiceErrorsAreSanitizedAndNeverRetried(int status)
    {
        using var handler = new Handler(_ => Response(status, """{"message":"PRIVATE-SERVER-BODY"}"""));
        using var http = new HttpClient(handler);
        var error = await Assert.ThrowsAsync<SampleException>(() =>
            new ScopesClient(http, "PRIVATE-TOKEN").ReplaceAsync(Account, "ProductDocs", Sites, Etag));
        Assert.Contains($"HTTP {status}", error.Message);
        Assert.DoesNotContain("PRIVATE", error.Message);
        Assert.Single(handler.Requests);
        if (status == 412) Assert.Contains("reconcile", error.Message);
        if (status == 408 || status >= 500 || status == 302) Assert.Contains("outcome may be unknown", error.Message);
    }

    [Fact]
    public async Task NetworkFailureRequiresReconciliationWithoutRetry()
    {
        using var handler = new Handler(_ => throw new HttpRequestException("PRIVATE-CONNECTION"));
        using var http = new HttpClient(handler);
        var error = await Assert.ThrowsAsync<SampleException>(() =>
            new ScopesClient(http, "PRIVATE-TOKEN").CreateAsync(Account, "ProductDocs", Sites));
        Assert.Contains("read and reconcile", error.Message);
        Assert.DoesNotContain("PRIVATE", error.Message);
        Assert.Single(handler.Requests);
    }

    [Theory]
    [InlineData("""{}""")]
    [InlineData("""{"includeSites":[]}""")]
    [InlineData("""{"includeSites":null,"excludeSites":[]}""")]
    [InlineData("""{"includeSites":[],"excludeSites":[null]}""")]
    [InlineData("""{"includeSites":[" "],"excludeSites":[]}""")]
    [InlineData("""{"includeSites":[],"excludeSites":[],"name":"extra"}""")]
    public void InvalidDefinitionsAreRejected(string json)
    {
        using var document = JsonDocument.Parse(json);
        Assert.Throws<SampleException>(() => ScopeDefinition.Parse(document.RootElement));
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task SearchUsesSingleNamedScopeFailClosedAndSeparateAuth(bool apiKey)
    {
        using var handler = new Handler(_ => Response(200, """{"webResults":[{"content":"PRIVATE-CONTENT"}]}"""));
        using var http = new HttpClient(handler);
        Assert.Equal(new SearchOutcome(1, null), await new ScopesClient(http, "PRIVATE-CREDENTIAL", apiKey).SearchAsync(
            "ProductDocs", "PRIVATE-QUERY", apiKey ? null : "demo-webiq-appid"));
        var request = Assert.Single(handler.Requests);
        Assert.Equal("/v3/search/web", request.Url.AbsolutePath);
        Assert.Equal(apiKey ? "" : "?APPID=demo-webiq-appid", request.Url.Query);
        Assert.True(request.Headers.ContainsKey(apiKey ? "x-apikey" : "Authorization"));
        Assert.False(request.Headers.ContainsKey(apiKey ? "Authorization" : "x-apikey"));
        using var body = JsonDocument.Parse(request.Body);
        Assert.Equal("ProductDocs", body.RootElement.GetProperty("scopeName").GetString());
        Assert.True(body.RootElement.GetProperty("failOnMissingScope").GetBoolean());
        Assert.Equal(5, body.RootElement.GetProperty("maxResults").GetInt32());
        Assert.Equal(3000, body.RootElement.GetProperty("maxLength").GetInt32());
        Assert.Equal(8, body.RootElement.EnumerateObject().Count());
    }

    [Fact]
    public async Task MissingScopeNeverFallsBack()
    {
        using var handler = new Handler(_ => Response(400));
        using var http = new HttpClient(handler);
        var error = await Assert.ThrowsAsync<SampleException>(() =>
            new ScopesClient(http, "key", true).SearchAsync("Missing", "query"));
        Assert.Contains("never falls back", error.Message);
        Assert.Single(handler.Requests);
    }

    [Fact]
    public async Task ApiKeyDoesNotAuthenticateManagementOrAcceptAppId()
    {
        using var handler = new Handler(_ => throw new InvalidOperationException("No request expected"));
        using var http = new HttpClient(handler);
        var client = new ScopesClient(http, "key", true);
        await Assert.ThrowsAsync<SampleException>(() => client.DiscoverAsync());
        await Assert.ThrowsAsync<SampleException>(() => client.SearchAsync("ProductDocs", "query", "app"));
        Assert.Empty(handler.Requests);
    }

    [Fact]
    public async Task CliRequiresAccountButApiKeySearchNeedsNoCertificate()
    {
        using var handler = new Handler(_ => Response(200, """{"webResults":[]}"""));
        using var http = new HttpClient(handler);
        using var output = new StringWriter();
        using var errors = new StringWriter();
        Task<string> NoToken() => throw new InvalidOperationException("Token acquisition not expected");
        string? Environment(string key) => key == "WEBIQ_API_KEY" ? "PRIVATE-KEY" : null;
        Assert.Equal(1, await Cli.RunAsync(["list"], output, errors, Environment, http, NoToken));
        Assert.Contains("WEBIQ_BILLING_ACCOUNT_ID", errors.ToString());
        Assert.Equal(0, await Cli.RunAsync(["search", "ProductDocs", "PRIVATE-QUERY"], output, errors, Environment, http, NoToken));
        Assert.Contains("the service did not report whether the scope was applied", output.ToString());
        Assert.DoesNotContain("Scoped search succeeded", output.ToString());
        Assert.DoesNotContain("PRIVATE", output.ToString() + errors);
        Assert.Single(handler.Requests);
    }

    [Fact]
    public async Task EmptyIncludesNeedAcknowledgmentBeforeAuth()
    {
        string path = Path.GetTempFileName();
        try
        {
            await File.WriteAllTextAsync(path, """{"includeSites":[],"excludeSites":[]}""");
            using var handler = new Handler(_ => Response(201, etag: Etag));
            using var http = new HttpClient(handler);
            using var output = new StringWriter();
            using var errors = new StringWriter();
            string? Environment(string key) => key == "WEBIQ_BILLING_ACCOUNT_ID" ? Account : null;
            string[] args = ["create", "AllExceptExcluded", "--definition", path];
            Assert.Equal(1, await Cli.RunAsync(args, output, errors, Environment, http,
                () => throw new InvalidOperationException("No token expected")));
            Assert.Contains("--allow-all-sites", errors.ToString());
            Assert.Empty(handler.Requests);
            Assert.Equal(0, await Cli.RunAsync([.. args, "--allow-all-sites"], output, errors, Environment, http,
                () => Task.FromResult("token")));
            Assert.Contains("not a readiness signal", output.ToString());
        }
        finally { File.Delete(path); }
    }

    [Theory]
    [InlineData("invalid-json")]
    [InlineData("[]")]
    [InlineData("""{"webResults":null}""")]
    public async Task MalformedSearchResponseIsNotSuccess(string body)
    {
        using var handler = new Handler(_ => Response(200, body));
        using var http = new HttpClient(handler);
        await Assert.ThrowsAsync<SampleException>(() => new ScopesClient(http, "key", true).SearchAsync("ProductDocs", "query"));
    }

    [Theory]
    [InlineData("true", "scopeApplied: true")]
    [InlineData("null", "the service did not report whether the scope was applied")]
    [InlineData(null, "the service did not report whether the scope was applied")]
    public async Task CliReportsScopeApplicationWithoutInference(string? value, string expected)
    {
        string applied = value is null ? "" : ",\"scopeApplied\":" + value;
        using var handler = new Handler(_ => Response(200, "{\"webResults\":[{\"content\":\"PRIVATE-CONTENT\"}]" + applied + "}"));
        using var http = new HttpClient(handler);
        using var output = new StringWriter();
        using var errors = new StringWriter();
        Assert.Equal(0, await Cli.RunAsync(["search", "ProductDocs", "PRIVATE-QUERY"], output, errors,
            key => key == "WEBIQ_API_KEY" ? "PRIVATE-KEY" : null, http));
        Assert.Contains(expected, output.ToString());
        Assert.DoesNotContain("Scoped search succeeded", output.ToString());
        Assert.DoesNotContain("PRIVATE", output.ToString() + errors);
        Assert.Single(handler.Requests);
    }

    [Fact]
    public async Task ScopeNotAppliedExitsNonzeroWithoutRetryOrContent()
    {
        using var handler = new Handler(_ => Response(200, """{"webResults":[{"content":"PRIVATE-CONTENT"}],"scopeApplied":false}"""));
        using var http = new HttpClient(handler);
        using var output = new StringWriter();
        using var errors = new StringWriter();
        Assert.Equal(1, await Cli.RunAsync(["search", "ProductDocs", "PRIVATE-QUERY"], output, errors,
            key => key == "WEBIQ_API_KEY" ? "PRIVATE-KEY" : null, http));
        Assert.Empty(output.ToString());
        Assert.Contains("scopeApplied: false", errors.ToString());
        Assert.DoesNotContain("PRIVATE", errors.ToString());
        Assert.Single(handler.Requests);
    }

    [Theory]
    [InlineData("\"true\"")]
    [InlineData("\"false\"")]
    [InlineData("0")]
    [InlineData("1")]
    [InlineData("[]")]
    [InlineData("{}")]
    public async Task InvalidScopeApplicationTypeFailsExplicitly(string value)
    {
        using var handler = new Handler(_ => Response(200, "{\"webResults\":[],\"scopeApplied\":" + value + "}"));
        using var http = new HttpClient(handler);
        var error = await Assert.ThrowsAsync<SampleException>(() =>
            new ScopesClient(http, "key", true).SearchAsync("ProductDocs", "query"));
        Assert.Contains("invalid scopeApplied", error.Message);
        Assert.Single(handler.Requests);
    }

    [Theory]
    [InlineData("")]
    [InlineData("with space")]
    [InlineData("name/segment")]
    [InlineData("non-ascii-\u00e9")]
    public void InvalidNamesAreRejected(string name) =>
        Assert.Throws<SampleException>(() => ScopesClient.ScopeName(name));

    [Fact]
    public void NameBoundariesAndMissingSettingsAreChecked()
    {
        Assert.Equal(new string('a', 128), ScopesClient.ScopeName(new string('a', 128)));
        Assert.Throws<SampleException>(() => ScopesClient.ScopeName(new string('a', 129)));
        Assert.Equal(Etag, ScopesClient.StrongEtag(Etag));
        Assert.Throws<SampleException>(() => Cli.Required(_ => "<placeholder>", "AZURE_TENANT_ID"));
        Assert.Equal("https://api.microsoft.ai/.default", ScopesClient.TokenScope);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(101)]
    public async Task InvalidPageSizesDoNotSend(int pageSize)
    {
        using var handler = new Handler(_ => throw new InvalidOperationException("No request expected"));
        using var http = new HttpClient(handler);
        await Assert.ThrowsAsync<SampleException>(() => new ScopesClient(http, "token").ListAsync(Account, pageSize));
        Assert.Empty(handler.Requests);
    }

    [Fact]
    public async Task ManagementPathsEncodeIdentifiersAndRejectDotNormalization()
    {
        using var handler = new Handler(_ => Response(200, """{"items":[]}"""));
        using var http = new HttpClient(handler);
        var client = new ScopesClient(http, "token");
        foreach (string value in new[] { ".", ".." })
        {
            await Assert.ThrowsAsync<SampleException>(() => client.CreateAsync(Account, value, Sites));
            await Assert.ThrowsAsync<SampleException>(() => client.ListAsync(value));
        }
        Assert.Empty(handler.Requests);
        await client.ListAsync("account/with slash");
        Assert.Equal("/v1/customer/billing-accounts/account%2Fwith%20slash/scopes",
            Assert.Single(handler.Requests).Url.AbsolutePath);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("W/\"weak\"")]
    public async Task CreateRequiresUsableResponseEtag(string? etag)
    {
        using var handler = new Handler(_ => Response(201, etag: etag));
        using var http = new HttpClient(handler);
        await Assert.ThrowsAsync<SampleException>(() => new ScopesClient(http, "token").CreateAsync(Account, "ProductDocs", Sites));
        Assert.Single(handler.Requests);
    }

    [Fact]
    public async Task CertificateConfigurationErrorsAreSanitizedBeforeTokenRequests()
    {
        string? Environment(string key) => key switch
        {
            "AZURE_TENANT_ID" => "PRIVATE-INVALID-TENANT",
            "AZURE_CLIENT_ID" => "PRIVATE-INVALID-CLIENT",
            "AZURE_CLIENT_CERTIFICATE_PATH" => "PRIVATE-PATH.pfx",
            _ => null
        };
        using var output = new StringWriter();
        using var errors = new StringWriter();
        Assert.Equal(1, await Cli.RunAsync(["discover"], output, errors, Environment));
        Assert.Contains("GUIDs", errors.ToString());
        Assert.DoesNotContain("PRIVATE", errors.ToString());
        Assert.Empty(output.ToString());
    }

    [Theory]
    [InlineData("PRIVATE\r\nINJECTED")]
    [InlineData("")]
    public async Task InvalidCredentialHeadersFailWithoutLoggingOrNetwork(string credential)
    {
        using var handler = new Handler(_ => throw new InvalidOperationException("No request expected"));
        using var http = new HttpClient(handler);
        var error = await Assert.ThrowsAsync<SampleException>(() =>
            new ScopesClient(http, credential).DiscoverAsync());
        Assert.DoesNotContain("PRIVATE", error.Message);
        Assert.Empty(handler.Requests);
    }
}
