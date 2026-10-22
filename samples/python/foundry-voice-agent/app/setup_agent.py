# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Explicit one-time agent creation; never run during app startup."""

import argparse
import asyncio
import json
from datetime import UTC, datetime

from azure.ai.projects.aio import AIProjectClient
from azure.core.exceptions import HttpResponseError
from azure.identity.aio import AzureCliCredential

from app.agent import definition
from app.config import AGENT_FILE, Settings


async def create(name: str, model: str, new_version: bool = False) -> None:
    settings = Settings.load()
    if AGENT_FILE.exists() and not new_version:
        raise SystemExit("An agent is already recorded in .foundry/agent.json; use --new-version to update it.")
    if new_version and (not AGENT_FILE.exists() or settings.agent_name != name):
        raise SystemExit("--new-version must use the agent name recorded in .foundry/agent.json.")
    async with (
        AzureCliCredential(tenant_id=settings.tenant_id or None, process_timeout=15) as credential,
        AIProjectClient(
            settings.project_endpoint, credential=credential, allow_preview=True,
            retry_total=0, connection_timeout=15, read_timeout=30,
        ) as client,
    ):
        try:
            created = await client.agents.create_version(agent_name=name, definition=definition(model))
        except HttpResponseError as exc:
            # Agent creation contains no WebIQ credentials or user content.
            print(f"Native voice creation rejected: HTTP {exc.status_code}; {exc.message}")
            raise SystemExit(1) from None
        AGENT_FILE.parent.mkdir(exist_ok=True)
        AGENT_FILE.write_text(json.dumps({
            "name": name, "version": created.version, "model": model,
            "kind": created.definition.kind, "store": False,
            "project_endpoint": settings.project_endpoint,
            "created_at": datetime.now(UTC).isoformat(),
        }, indent=2) + "\n")
        print(f"Created native voice agent {name}, version {created.version}, managed model {model}, store=False.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args()
    asyncio.run(create(args.name, args.model, args.new_version))
