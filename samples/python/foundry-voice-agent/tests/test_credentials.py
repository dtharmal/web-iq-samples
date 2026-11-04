# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError

from app.credentials import SessionCredential
from app.performance import PerformanceLogger, PerformanceTrace


@pytest.fixture
def credential():
    inner = AsyncMock()
    inner.get_token.return_value = AccessToken("TEST_TOKEN", int(time.time()) + 3600)
    return inner


async def test_rest_and_voice_reuse_one_token_and_cleanup_discards_it(credential):
    wrapper = SessionCredential(credential, PerformanceTrace(), cache=True)
    async with wrapper:
        first = await wrapper.get_token("scope")
        assert await wrapper.get_token("scope") is first
        credential.get_token.assert_awaited_once()
    assert wrapper._token is None and wrapper._key is None
    credential.__aexit__.assert_awaited_once()
    with pytest.raises(RuntimeError, match="closed"):
        await wrapper.get_token("scope")


async def test_cache_is_not_shared_between_sessions(credential):
    for _ in range(2):
        async with SessionCredential(credential, PerformanceTrace(), cache=True) as wrapper:
            await wrapper.get_token("scope")
    assert credential.get_token.await_count == 2


@pytest.mark.parametrize("options", [
    {"claims": "challenge"}, {"tenant_id": "different"}, {"enable_cae": True}, {"extra_option": True},
])
async def test_identity_challenges_and_request_options_never_reuse_wrong_token(credential, options):
    async with SessionCredential(credential, PerformanceTrace(), cache=True) as wrapper:
        await wrapper.get_token("scope")
        await wrapper.get_token("scope", **options)
    assert credential.get_token.await_count == 2
    assert all(credential.get_token.call_args.kwargs[k] == v for k, v in options.items())


async def test_different_scope_does_not_reuse_token(credential):
    async with SessionCredential(credential, PerformanceTrace(), cache=True) as wrapper:
        await wrapper.get_token("scope")
        await wrapper.get_token("other")
    assert credential.get_token.await_count == 2


@pytest.mark.parametrize("expires_in", [-1, 0, 299, 300])
async def test_expiring_token_is_refreshed(credential, expires_in):
    credential.get_token.return_value = AccessToken("TEST_TOKEN", int(time.time()) + expires_in)
    async with SessionCredential(credential, PerformanceTrace(), cache=True) as wrapper:
        await wrapper.get_token("scope")
        await wrapper.get_token("scope")
    assert credential.get_token.await_count == 2


async def test_concurrent_requests_only_acquire_once(credential):
    async def acquire(*args, **kwargs):
        await asyncio.sleep(0)
        return AccessToken("TEST_TOKEN", int(time.time()) + 3600)

    credential.get_token.side_effect = acquire
    async with SessionCredential(credential, PerformanceTrace(), cache=True) as wrapper:
        tokens = await asyncio.gather(*(wrapper.get_token("scope") for _ in range(10)))
    assert len({id(token) for token in tokens}) == 1
    credential.get_token.assert_awaited_once()


async def test_hosted_mode_delegates_every_request_to_managed_identity(credential):
    async with SessionCredential(credential, PerformanceTrace(), cache=False) as wrapper:
        await wrapper.get_token("scope")
        await wrapper.get_token("scope")
    assert credential.get_token.await_count == 2


@pytest.mark.parametrize("failure", [ClientAuthenticationError("TEST_SECRET"), asyncio.CancelledError()])
async def test_acquisition_failure_or_cancel_propagates_and_closes(credential, failure):
    credential.get_token.side_effect = failure
    wrapper = SessionCredential(credential, PerformanceTrace(), cache=True)
    with pytest.raises(type(failure)):
        async with wrapper:
            await wrapper.get_token("scope")
    assert wrapper._token is None
    credential.__aexit__.assert_awaited_once()


async def test_failed_challenge_invalidates_old_token(credential):
    async with SessionCredential(credential, PerformanceTrace(), cache=True) as wrapper:
        await wrapper.get_token("scope")
        credential.get_token.side_effect = ClientAuthenticationError("TEST_SECRET")
        with pytest.raises(ClientAuthenticationError):
            await wrapper.get_token("scope", claims="challenge")
        assert wrapper._token is None
        credential.get_token.side_effect = None
        await wrapper.get_token("scope")
    assert credential.get_token.await_count == 3


async def test_auth_trace_contains_timings_not_tokens_or_scopes(credential, tmp_path):
    logger = PerformanceLogger(tmp_path / "private" / "performance.jsonl")
    async with SessionCredential(credential, PerformanceTrace(logger), cache=True) as wrapper:
        await wrapper.get_token("TEST_SCOPE_SECRET")
        await wrapper.get_token("TEST_SCOPE_SECRET")
    rows = list(logger.queue.queue)
    assert [row["event"] for row in rows] == ["stage_start", "stage_end"]
    assert rows[-1]["stage"] == "azure_auth"
    assert rows[-1]["outcome"] == "success"
    assert "TEST_TOKEN" not in str(rows) and "TEST_SCOPE_SECRET" not in str(rows)
