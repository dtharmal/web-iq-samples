# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Short-lived, process-local validation metadata; never stores tokens or definitions."""

import time
from dataclasses import dataclass

from azure.ai.projects.aio import AIProjectClient

from app.config import Settings
from app.webiq import PublicError

VALIDATION_TTL_SECONDS = 30
VALIDATION_REFRESH_SECONDS = 20


async def validate_agent(client: AIProjectClient, settings: Settings) -> str | None:
    agent = await client.agents.get(agent_name=settings.agent_name)
    version = agent.versions.latest
    if version.definition.kind != "voice" or version.definition.store is not False:
        raise PublicError("agent_config", "The agent must be native kind=voice with store=False.")
    if settings.agent_version and version.version != settings.agent_version:
        raise PublicError("agent_version", "The active agent version changed. Review it before reconnecting.")
    greeting = getattr(version.definition, "greeting", None)
    if greeting is not None and greeting.type not in {"template", "llm_generated"}:
        raise PublicError("agent_config", "The agent's startup greeting mode is not supported.")
    return greeting.type if greeting is not None else None


@dataclass(frozen=True)
class ValidatedAgent:
    key: tuple[str, ...]
    expires: float
    greeting_mode: str | None


class AgentValidationCache:
    def __init__(self):
        self.entry: ValidatedAgent | None = None
        self.error_code: str | None = None

    @staticmethod
    def key(settings: Settings) -> tuple[str, ...]:
        return (settings.mode, settings.project_endpoint, settings.tenant_id,
                settings.agent_name, settings.agent_version)

    def lookup(self, settings: Settings) -> ValidatedAgent | None:
        entry = self.entry
        if (not settings.hosted and settings.local_agent_validation_enabled
                and settings.agent_version and entry is not None
                and entry.key == self.key(settings) and time.monotonic() < entry.expires):
            return entry
        return None

    def store(self, settings: Settings, greeting_mode: str | None, checked_at: float) -> None:
        self.entry = ValidatedAgent(self.key(settings), checked_at + VALIDATION_TTL_SECONDS, greeting_mode)
        self.error_code = None

    def invalidate(self, error_code: str | None = None) -> None:
        self.entry = None
        self.error_code = error_code

    def status(self, settings: Settings) -> dict:
        return {
            "state": "fresh" if self.lookup(settings) else "unavailable",
            "max_age_seconds": VALIDATION_TTL_SECONDS,
            "error_code": self.error_code,
        }
