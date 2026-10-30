# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Check local setup; --online adds read-only agent lookup and MCP discovery, never search or voice."""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

from azure.ai.projects.aio import AIProjectClient

from app.config import ROOT, Settings
from app.main import credential_for, prepare_services, public_error
from app.performance import PerformanceTrace


def local_checks(settings: Settings, root: Path = ROOT) -> list[tuple[bool, str]]:
    checks = [
        (sys.version_info >= (3, 11), "Python 3.11 or later."),
        (bool(shutil.which("az")), "Azure CLI on PATH; install it and run az login if missing."),
        (bool(shutil.which("npm")), "npm on PATH; install Node.js 22.12 or later if missing."),
        (not settings.perf_log_enabled or all(hasattr(os, name) for name in ("O_NOFOLLOW", "fchmod")),
         "Private disk logging supported or disabled; on native Windows set PERF_LOG_ENABLED=0."),
        (bool(settings.webiq_key) and "<" not in settings.webiq_key,
         "Web IQ key is set and is not an example placeholder; authentication is not yet verified."),
        ((root / "dist" / "index.html").is_file()
         and (root / "dist" / "audio-worklet.js").is_file()
         and any((root / "dist" / "assets").glob("*.js")),
         "Built UI exists; run npm ci and npm run build if missing."),
    ]
    node = shutil.which("node")
    node_ok = False
    if node:
        try:
            result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5, check=True)
            version = tuple(int(part) for part in result.stdout.strip().removeprefix("v").split("."))
            node_ok = version >= (22, 12, 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            node_ok = False
    checks.append((node_ok, "Node.js 22.12 or later is available."))
    return checks


async def online_check(settings: Settings) -> None:
    if not settings.agent_name or not settings.agent_version:
        raise ValueError("Create the agent using app.setup_agent, then rerun preflight --online.")
    async with (
        asyncio.timeout(45),
        credential_for(settings) as credential,
        AIProjectClient(
            settings.project_endpoint, credential=credential, allow_preview=True,
            retry_total=0, connection_timeout=15, read_timeout=20,
        ) as client,
        prepare_services(client, settings, PerformanceTrace()),
    ):
        pass


async def run(online: bool = False) -> int:
    try:
        settings = Settings.load()
    except (ValueError, OSError):
        # A parser error can quote a configured value. Never echo configuration or exception text.
        print("FAIL configuration: check .env against .env.example, paired agent overrides, "
              "and the project recorded in .foundry/agent.json. No values were printed.")
        return 1
    checks = local_checks(settings)
    for ok, message in checks:
        print(f"{'PASS' if ok else 'FAIL'} {message}")
    if not all(ok for ok, _ in checks):
        return 1
    if not settings.agent_name or not settings.agent_version:
        print("NEXT Create the voice agent using the README app.setup_agent command.")
    if not online:
        print("Local checks only; no service access verified. After agent setup, run with --online.")
        return 0
    try:
        await online_check(settings)
    except ValueError:
        print("FAIL agent configuration: run app.setup_agent or set both agent overrides.")
        return 1
    except Exception as exc:  # noqa: BLE001 -- CLI boundary never prints raw service errors
        error = public_error(exc, hosted=settings.hosted)
        print(f"FAIL {error.code}: {error.message}")
        return 1
    print("PASS Foundry agent access, latest version, kind=voice, store=False, and Web IQ news-tool discovery.")
    print("No agent created, search submitted, or voice session opened. Runtime voice access is not verified.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="Read-only service checks after agent setup.")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.online)))


if __name__ == "__main__":
    main()
