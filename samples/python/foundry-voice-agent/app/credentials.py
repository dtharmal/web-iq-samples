# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Session-scoped token reuse; no credentials or tokens survive session cleanup."""

import asyncio
import time
from types import TracebackType
from typing import Any, Self

from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential

from app.performance import PerformanceTrace


class SessionCredential:
    def __init__(self, credential: AsyncTokenCredential, trace: PerformanceTrace, *, cache: bool):
        self.credential = credential
        self.trace = trace
        self.cache = cache
        self._lock = asyncio.Lock()
        self._token: AccessToken | None = None
        self._key: tuple | None = None
        self._closed = False

    async def __aenter__(self) -> Self:
        await self.credential.__aenter__()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None, traceback: TracebackType | None = None,
    ) -> None:
        self._closed = True
        self._token = self._key = None
        await self.credential.__aexit__(exc_type, exc_value, traceback)

    async def close(self) -> None:
        await self.__aexit__()

    async def get_token(
        self, *scopes: str, claims: str | None = None, tenant_id: str | None = None,
        enable_cae: bool = False, **kwargs: Any,
    ) -> AccessToken:
        async with self._lock:
            if self._closed:
                raise RuntimeError("The session credential is closed.")
            key = (scopes, tenant_id, enable_cae)
            # Challenges and unknown request options must reach the identity provider.
            reusable = self.cache and not claims and not kwargs
            if (reusable and key == self._key and self._token is not None
                    and self._token.expires_on > time.time() + 300):
                return self._token
            self._token = self._key = None
            with self.trace.span("azure_auth"):
                token = await self.credential.get_token(
                    *scopes, claims=claims, tenant_id=tenant_id, enable_cae=enable_cae, **kwargs,
                )
            if reusable:
                self._token, self._key = token, key
            return token
