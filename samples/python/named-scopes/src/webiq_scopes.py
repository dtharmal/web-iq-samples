# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import argparse
import json
import os
import re
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

import httpx
from azure.core.exceptions import AzureError
from azure.identity import CertificateCredential

BASE_URL = "https://api.microsoft.ai"
TOKEN_SCOPE = "https://api.microsoft.ai/.default"


class SampleError(Exception):
    """A diagnostic that is safe to display without service or credential details."""


class SearchOutcome(NamedTuple):
    result_count: int
    scope_applied: bool | None


def required_setting(name: str) -> str:
    value = os.environ.get(name)
    if not value or not value.strip() or value.startswith("<"):
        raise SampleError(f"Set {name} to a non-placeholder value before running.")
    return value


def certificate_credential() -> CertificateCredential:
    settings = {
        "tenant_id": required_setting("AZURE_TENANT_ID"),
        "client_id": required_setting("AZURE_CLIENT_ID"),
        "certificate_path": required_setting("AZURE_CLIENT_CERTIFICATE_PATH"),
        "password": os.environ.get("AZURE_CLIENT_CERTIFICATE_PASSWORD"),
    }
    try:
        return CertificateCredential(**settings)
    except (ValueError, OSError) as error:
        raise SampleError(
            "Cannot load the certificate. Check the private-key file and its existing password."
        ) from error


def scope_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        raise SampleError(
            "Scope name must contain 1-128 ASCII letters, digits, periods, underscores, or hyphens."
        )
    return value


def strong_etag(value: str | None) -> str:
    if value is None or not re.fullmatch(r'"[\x21\x23-\x7e]*"', value):
        raise SampleError(
            'A current strong, quoted ETag is required (for example \'"opaque-version"\'); '
            "weak tags and * are not accepted. Read the scope before writing."
        )
    return value


def path_segment(value: str) -> str:
    encoded = quote(value, safe="")
    # Dot-only identifiers must not become URL navigation segments.
    return encoded.replace(".", "%2E") if encoded in {".", ".."} else encoded


def json_object(response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError as error:
        raise SampleError("The service returned invalid JSON; no response body was logged.") from error
    if not isinstance(body, dict):
        raise SampleError("The service returned an unexpected JSON shape.")
    return body


def definition(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict) or set(value) != {"includeSites", "excludeSites"}:
        raise SampleError("Definition must contain exactly includeSites and excludeSites.")
    result = {}
    for field in ("includeSites", "excludeSites"):
        sites = value[field]
        if not isinstance(sites, list) or not all(
            isinstance(site, str) and site.strip() for site in sites
        ):
            raise SampleError(f"{field} must be an array of nonempty site strings; [] is valid.")
        result[field] = sites
    return result


def load_definition(path: Path) -> dict[str, list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SampleError("Cannot read the definition file as UTF-8 JSON.") from error
    return definition(value)


def send(
    http: httpx.Client,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    expected: int,
    body: object = None,
    params: dict[str, str | int] | None = None,
) -> httpx.Response:
    writing = method in {"POST", "PUT", "DELETE"} and path.startswith("/v1/customer/")
    uncertain = (
        " The write outcome may be unknown; read and reconcile before retrying."
        if writing else " Retry explicitly only after resolving the failure."
    )
    try:
        response = http.request(
            method, BASE_URL + path, headers=headers, json=body, params=params,
        )
    except httpx.RequestError as error:
        raise SampleError("Network request failed." + uncertain) from error
    if response.status_code == expected:
        return response
    guidance = {
        400: "Check input, scope availability, and the selected profile. Search never falls back.",
        401: "Check the credential, tenant, and token audience.",
        403: "Check management CustomerApiAccess or, for search, profile binding and Web access.",
        404: "Check the selected billing account, profile, and scope name.",
        409: "Read the collection; the name may already exist or the collection may be full.",
        412: "Read the latest definition and ETag, reconcile changes, then retry deliberately.",
        428: "Supply the current strong ETag in If-Match.",
        429: "Respect service retry guidance before retrying explicitly.",
    }.get(response.status_code, "Check service access and availability.")
    if response.status_code >= 500 or 300 <= response.status_code < 400:
        guidance += uncertain
    raise SampleError(
        f"HTTP {response.status_code}. {guidance} Response body and credentials were not logged."
    )


class ScopesClient:
    def __init__(self, http: httpx.Client, token: str, billing_account_id: str) -> None:
        self.http = http
        self.headers = {"Authorization": f"Bearer {token}"}
        self.collection = (
            "/v1/customer/billing-accounts/" + path_segment(billing_account_id) + "/scopes"
        )

    def _path(self, name: str) -> str:
        return self.collection + "/" + path_segment(scope_name(name))

    def _request(
        self, method: str, path: str, expected: int, *,
        body: object = None, etag: str | None = None,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        headers = self.headers.copy()
        if method in {"PUT", "DELETE"}:
            headers["If-Match"] = strong_etag(etag)
        return send(
            self.http, method, path, headers=headers, expected=expected, body=body, params=params,
        )

    def list_scopes(self, page_size: int = 50) -> list[object]:
        if not 1 <= page_size <= 100:
            raise SampleError("Page size must be between 1 and 100.")
        params: dict[str, str | int] = {"pageSize": page_size}
        items: list[object] = []
        seen_tokens: set[str] = set()
        while True:
            page = json_object(self._request("GET", self.collection, 200, params=params))
            page_items = page.get("items")
            token = page.get("nextContinuationToken")
            if not isinstance(page_items, list) or not all(
                isinstance(item, dict) for item in page_items
            ):
                raise SampleError("The scope list returned an unexpected items shape.")
            items.extend(page_items)
            if token is None:
                return items
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise SampleError("The scope list returned an invalid or repeated continuation token.")
            seen_tokens.add(token)
            params["continuationToken"] = token

    def get_scope(self, name: str) -> tuple[dict[str, list[str]], str]:
        response = self._request("GET", self._path(name), 200)
        body = json_object(response)
        return definition({
            field: body.get(field) for field in ("includeSites", "excludeSites")
        }), strong_etag(response.headers.get("ETag"))

    def create_scope(self, name: str, sites: dict[str, list[str]]) -> str:
        response = self._request(
            "POST", self.collection, 201, body={"name": scope_name(name), **definition(sites)},
        )
        return strong_etag(response.headers.get("ETag"))

    def replace_scope(self, name: str, sites: dict[str, list[str]], etag: str) -> str:
        response = self._request(
            "PUT", self._path(name), 200, body=definition(sites), etag=etag,
        )
        return strong_etag(response.headers.get("ETag"))

    def delete_scope(self, name: str, etag: str) -> None:
        self._request("DELETE", self._path(name), 204, etag=etag)


def discover(http: httpx.Client, token: str) -> list[object]:
    response = send(
        http, "GET", "/v1/customer/authorized-resources",
        headers={"Authorization": f"Bearer {token}"}, expected=200,
    )
    try:
        customers = response.json()
    except ValueError as error:
        raise SampleError("Authorized-resource discovery returned invalid JSON.") from error
    if not isinstance(customers, list) or not all(isinstance(item, dict) for item in customers):
        raise SampleError("Authorized-resource discovery returned an unexpected JSON shape.")
    return customers


def search(
    http: httpx.Client, name: str, query: str, *,
    headers: dict[str, str], app_id: str | None = None,
) -> SearchOutcome:
    scope_name(name)
    if not query.strip():
        raise SampleError("The search query cannot be empty.")
    if app_id is not None and ("x-apikey" in headers or not app_id.strip()):
        raise SampleError("APPID is only for Entra search and must be a nonempty Web IQ AppID.")
    response = send(
        http, "POST", "/v3/search/web", headers=headers, expected=200,
        params={"APPID": app_id} if app_id else None,
        body={
            "query": query, "scopeName": name, "failOnMissingScope": True,
            "maxResults": 5, "language": "en", "region": "US",
            "contentFormat": "passage", "maxLength": 3000,
        },
    )
    body = json_object(response)
    results = body.get("webResults")
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise SampleError("The search response did not contain a webResults array.")
    applied = body.get("scopeApplied")
    if applied is not None and not isinstance(applied, bool):
        raise SampleError("The search response contained an invalid scopeApplied value.")
    if applied is False:
        raise SampleError(
            "Search returned scopeApplied: false; the named scope was not applied. "
            "Stopped without retrying unscoped."
        )
    return SearchOutcome(len(results), applied)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage Web IQ named scopes or search with one explicit named scope.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="List authorized accounts; never auto-select an account.")
    listing = commands.add_parser("list", help="List all pages of scope metadata, not selectors.")
    listing.add_argument("--page-size", type=int, default=50)
    for command in ("get", "create", "replace", "delete"):
        subparser = commands.add_parser(command)
        subparser.add_argument("name")
        if command in {"create", "replace"}:
            subparser.add_argument("--definition", type=Path, required=True)
            subparser.add_argument(
                "--allow-all-sites", action="store_true",
                help="Acknowledge that an empty includeSites broadens to all non-excluded sources.",
            )
        if command in {"replace", "delete"}:
            subparser.add_argument("--if-match", required=True, help="Exact current quoted ETag.")
    searching = commands.add_parser("search", help="Search with an API- or UI-authored scope.")
    searching.add_argument("name")
    searching.add_argument("query")
    searching.add_argument("--auth", choices=("api-key", "entra"), default="api-key")
    searching.add_argument("--app-id", help="Web IQ AppID for Entra multi-profile selection.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command not in {"discover", "list"}:
        scope_name(args.name)
    sites = None
    if args.command in {"create", "replace"}:
        sites = load_definition(args.definition)
        if not sites["includeSites"] and not args.allow_all_sites:
            raise SampleError("Empty includeSites requires an explicit --allow-all-sites.")
    if args.command in {"replace", "delete"}:
        strong_etag(args.if_match)
    if args.command == "list" and not 1 <= args.page_size <= 100:
        raise SampleError("Page size must be between 1 and 100.")
    if args.command == "search":
        if not args.query.strip():
            raise SampleError("The search query cannot be empty.")
        if args.app_id is not None and (args.auth == "api-key" or not args.app_id.strip()):
            raise SampleError("--app-id is only for Entra search and must not be empty.")

    billing_account_id = (
        required_setting("WEBIQ_BILLING_ACCOUNT_ID")
        if args.command not in {"discover", "search"} else None
    )
    with ExitStack() as stack:
        if args.command == "search" and args.auth == "api-key":
            headers = {"x-apikey": required_setting("WEBIQ_API_KEY")}
            token = None
        else:
            credential = stack.enter_context(certificate_credential())
            token = credential.get_token(TOKEN_SCOPE).token
            headers = {"Authorization": f"Bearer {token}"}
        http = stack.enter_context(httpx.Client(timeout=30.0, follow_redirects=False))
        if args.command == "search":
            outcome = search(
                http, args.name, args.query, headers=headers, app_id=args.app_id,
            )
            status = (
                "scopeApplied: true" if outcome.scope_applied is True
                else "the service did not report whether the scope was applied"
            )
            print(
                f"Search returned {outcome.result_count} web results; {status}. "
                "Retrieved content was not printed."
            )
        elif args.command == "discover":
            assert token is not None
            customers = discover(http, token)
            print(json.dumps(customers, indent=2))
            if not customers:
                print("No authorized resources. Check workload registration and CustomerApiAccess.")
        else:
            assert token is not None and billing_account_id is not None
            client = ScopesClient(http, token, billing_account_id)
            if args.command == "list":
                print(json.dumps(client.list_scopes(args.page_size), indent=2))
            elif args.command == "get":
                scope, etag = client.get_scope(args.name)
                print(json.dumps({"name": args.name, **scope, "etag": etag}, indent=2))
            elif args.command == "delete":
                client.delete_scope(args.name, args.if_match)
                print("Scope deleted from authoring storage; serving propagation is asynchronous.")
            else:
                assert sites is not None
                etag = (
                    client.create_scope(args.name, sites) if args.command == "create"
                    else client.replace_scope(args.name, sites, args.if_match)
                )
                print(f"Authoring write succeeded. ETag: {etag}")
                print("Serving propagation is asynchronous; this is not a readiness signal.")
    return 0


def entrypoint() -> None:
    try:
        raise SystemExit(main())
    except SampleError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except AzureError as error:
        print(
            "Error: Entra authentication failed. Check certificate settings and service access; "
            "credential details were not logged.",
            file=sys.stderr,
        )
        raise SystemExit(1) from error
