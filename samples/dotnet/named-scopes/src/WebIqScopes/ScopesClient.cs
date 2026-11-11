// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace WebIqScopes;

public sealed class SampleException(string message) : Exception(message);

public sealed record SearchOutcome(int ResultCount, bool? ScopeApplied);

public sealed record ScopeDefinition(
    [property: JsonPropertyName("includeSites")] string[] IncludeSites,
    [property: JsonPropertyName("excludeSites")] string[] ExcludeSites)
{
    public static ScopeDefinition Parse(JsonElement body, bool allowMetadata = false)
    {
        if (body.ValueKind != JsonValueKind.Object ||
            (!allowMetadata && (body.EnumerateObject().Count() != 2 ||
                body.EnumerateObject().Any(p => p.Name is not ("includeSites" or "excludeSites")))))
            throw new SampleException("Definition must contain exactly includeSites and excludeSites.");

        string[] Sites(string field)
        {
            if (!body.TryGetProperty(field, out var array) || array.ValueKind != JsonValueKind.Array ||
                array.EnumerateArray().Any(item => item.ValueKind != JsonValueKind.String ||
                    string.IsNullOrWhiteSpace(item.GetString())))
                throw new SampleException($"{field} must be an array of nonempty site strings; [] is valid.");
            return array.EnumerateArray().Select(item => item.GetString()!).ToArray();
        }
        return new(Sites("includeSites"), Sites("excludeSites"));
    }

    public void Validate()
    {
        foreach (var sites in new[] { IncludeSites, ExcludeSites })
            if (sites is null || sites.Any(string.IsNullOrWhiteSpace))
                throw new SampleException("Both site arrays are required and must contain nonempty strings.");
    }
}

public sealed class ScopesClient(HttpClient http, string credential, bool apiKey = false)
{
    public const string BaseUrl = "https://api.microsoft.ai";
    public const string TokenScope = "https://api.microsoft.ai/.default";

    public static string ScopeName(string value)
    {
        if (value.Length is < 1 or > 128 ||
            value.Any(c => !char.IsAsciiLetterOrDigit(c) && c is not ('.' or '_' or '-')))
            throw new SampleException("Scope name requires 1-128 ASCII letters, digits, periods, underscores, or hyphens.");
        return value;
    }

    public static string StrongEtag(string? value)
    {
        if (value is null || value.Length < 2 || value[0] != '"' || value[^1] != '"' ||
            value[1..^1].Any(c => c < 0x21 || c > 0x7e || c == '"'))
            throw new SampleException("Read the scope and supply its exact strong quoted ETag; weak tags and * are invalid.");
        return value;
    }

    private static string Segment(string value)
    {
        if (string.IsNullOrWhiteSpace(value) || value is "." or "..")
            throw new SampleException("Management identifiers must be nonempty and cannot be dot-only URL segments.");
        return Uri.EscapeDataString(value);
    }

    private static string Collection(string account) => $"/v1/customer/billing-accounts/{Segment(account)}/scopes";
    private static string ScopePath(string account, string name) => $"{Collection(account)}/{Segment(ScopeName(name))}";

    private async Task<HttpResponseMessage> SendAsync(
        HttpMethod method, string path, int expected, object? body = null, string? etag = null)
    {
        bool writing = method != HttpMethod.Get && path.StartsWith("/v1/customer/", StringComparison.Ordinal);
        string uncertainty = writing
            ? " The write outcome may be unknown; read and reconcile before retrying."
            : " Resolve the failure before retrying explicitly.";
        if (apiKey && path.StartsWith("/v1/customer/", StringComparison.Ordinal))
            throw new SampleException("Management requires app-only Entra authentication, not an API key.");
        if (string.IsNullOrEmpty(credential) || credential.Any(c => c < 0x21 || c > 0x7e))
            throw new SampleException("Credential is not a valid HTTP header value.");
        using var request = new HttpRequestMessage(method, BaseUrl + path);
        if (apiKey)
            request.Headers.Add("x-apikey", credential);
        else
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", credential);
        if (method == HttpMethod.Put || method == HttpMethod.Delete)
            request.Headers.IfMatch.Add(new EntityTagHeaderValue(StrongEtag(etag)));
        if (body is not null)
            request.Content = JsonContent.Create(body);
        HttpResponseMessage response;
        try
        {
            response = await http.SendAsync(request);
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException)
        {
            throw new SampleException("Network request failed." + uncertainty);
        }
        if ((int)response.StatusCode == expected)
            return response;
        int status = (int)response.StatusCode;
        response.Dispose();
        string guidance = status switch
        {
            400 => "Check input, scope availability, and selected profile. Search never falls back.",
            401 => "Check credentials, tenant, and token audience.",
            403 => "Check management CustomerApiAccess or runtime profile binding and Web access.",
            404 => "Check the billing account, profile, and scope name.",
            409 => "Read the collection; a duplicate name or full collection may conflict.",
            412 => "Read the latest definition and ETag, reconcile changes, then retry deliberately.",
            428 => "Supply the current strong ETag in If-Match.",
            429 => "Respect service retry guidance before retrying explicitly.",
            _ => "Check service access and availability."
        };
        if (status == 408 || status >= 500 || status is >= 300 and < 400)
            guidance += uncertainty;
        throw new SampleException($"HTTP {status}. {guidance} Response body and credentials were not logged.");
    }

    private static async Task<JsonElement> ReadJsonAsync(HttpResponseMessage response)
    {
        try
        {
            using var document = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            return document.RootElement.Clone();
        }
        catch (JsonException)
        {
            throw new SampleException("Service returned invalid JSON; no response body was logged.");
        }
    }

    private static string Etag(HttpResponseMessage response)
    {
        string[] tags = response.Headers.TryGetValues("ETag", out var values) ? values.ToArray() : [];
        return StrongEtag(tags.Length == 1 ? tags[0] : null);
    }

    public async Task<JsonElement> DiscoverAsync()
    {
        using var response = await SendAsync(HttpMethod.Get, "/v1/customer/authorized-resources", 200);
        var body = await ReadJsonAsync(response);
        if (body.ValueKind != JsonValueKind.Array || body.EnumerateArray().Any(item => item.ValueKind != JsonValueKind.Object))
            throw new SampleException("Authorized-resource discovery returned an unexpected JSON shape.");
        return body;
    }

    public async Task<List<JsonElement>> ListAsync(string account, int pageSize = 50)
    {
        if (pageSize is < 1 or > 100)
            throw new SampleException("Page size must be between 1 and 100.");
        List<JsonElement> items = [];
        HashSet<string> seen = [];
        string? token = null;
        do
        {
            string path = $"{Collection(account)}?pageSize={pageSize}";
            if (token is not null)
                path += "&continuationToken=" + Uri.EscapeDataString(token);
            using var response = await SendAsync(HttpMethod.Get, path, 200);
            var page = await ReadJsonAsync(response);
            if (page.ValueKind != JsonValueKind.Object || !page.TryGetProperty("items", out var pageItems) ||
                pageItems.ValueKind != JsonValueKind.Array ||
                pageItems.EnumerateArray().Any(item => item.ValueKind != JsonValueKind.Object))
                throw new SampleException("Scope list returned an unexpected items shape.");
            items.AddRange(pageItems.EnumerateArray());
            if (!page.TryGetProperty("nextContinuationToken", out var next) || next.ValueKind == JsonValueKind.Null)
                break;
            if (next.ValueKind != JsonValueKind.String || string.IsNullOrEmpty(next.GetString()) || !seen.Add(next.GetString()!))
                throw new SampleException("Scope list returned an invalid or repeated continuation token.");
            token = next.GetString();
        } while (true);
        return items;
    }

    public async Task<(ScopeDefinition Definition, string Etag)> GetAsync(string account, string name)
    {
        using var response = await SendAsync(HttpMethod.Get, ScopePath(account, name), 200);
        return (ScopeDefinition.Parse(await ReadJsonAsync(response), allowMetadata: true), Etag(response));
    }

    public async Task<string> CreateAsync(string account, string name, ScopeDefinition definition)
    {
        _ = ScopePath(account, name);
        definition.Validate();
        using var response = await SendAsync(HttpMethod.Post, Collection(account), 201,
            new { name, includeSites = definition.IncludeSites, excludeSites = definition.ExcludeSites });
        return Etag(response);
    }

    public async Task<string> ReplaceAsync(string account, string name, ScopeDefinition definition, string etag)
    {
        definition.Validate();
        using var response = await SendAsync(HttpMethod.Put, ScopePath(account, name), 200, definition, etag);
        return Etag(response);
    }

    public async Task DeleteAsync(string account, string name, string etag)
    {
        using var response = await SendAsync(HttpMethod.Delete, ScopePath(account, name), 204, etag: etag);
    }

    public async Task<SearchOutcome> SearchAsync(string name, string query, string? appId = null)
    {
        ScopeName(name);
        if (string.IsNullOrWhiteSpace(query))
            throw new SampleException("Search query cannot be empty.");
        if (appId is not null && (apiKey || string.IsNullOrWhiteSpace(appId)))
            throw new SampleException("APPID is only for Entra search and must be a nonempty Web IQ AppID.");
        string path = "/v3/search/web" + (appId is null ? "" : "?APPID=" + Uri.EscapeDataString(appId));
        using var response = await SendAsync(HttpMethod.Post, path, 200, new
        {
            query, scopeName = name, failOnMissingScope = true, maxResults = 5,
            language = "en", region = "US", contentFormat = "passage", maxLength = 3000
        });
        var body = await ReadJsonAsync(response);
        if (body.ValueKind != JsonValueKind.Object || !body.TryGetProperty("webResults", out var results) ||
            results.ValueKind != JsonValueKind.Array || results.EnumerateArray().Any(item => item.ValueKind != JsonValueKind.Object))
            throw new SampleException("Search response did not contain a webResults array.");
        bool? applied = null;
        if (body.TryGetProperty("scopeApplied", out var scopeApplied))
        {
            applied = scopeApplied.ValueKind switch
            {
                JsonValueKind.True => true,
                JsonValueKind.False => false,
                JsonValueKind.Null => null,
                _ => throw new SampleException("Search response contained an invalid scopeApplied value.")
            };
        }
        if (applied is false)
            throw new SampleException("Search returned scopeApplied: false; the named scope was not applied. Stopped without retrying unscoped.");
        return new SearchOutcome(results.GetArrayLength(), applied);
    }
}
