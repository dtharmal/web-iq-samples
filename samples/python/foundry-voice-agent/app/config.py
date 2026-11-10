# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
PROJECT_ENDPOINT_PATTERN = re.compile(
    r"https://[a-z0-9][a-z0-9-]{0,62}\.services\.ai\.azure\.com/api/projects/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
)
TENANT_PATTERN = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
WEBIQ_ENDPOINT = "https://api.microsoft.ai/v3/auto"
AGENT_FILE = ROOT / ".foundry" / "agent.json"


@dataclass(frozen=True)
class Settings:
    project_endpoint: str = ""
    tenant_id: str = ""
    agent_name: str = ""
    agent_version: str = ""
    webiq_endpoint: str = WEBIQ_ENDPOINT
    webiq_key: str = field(default="", repr=False)
    port: int = 8765
    session_seconds: int = 600
    perf_log_enabled: bool = False
    local_agent_validation_enabled: bool = True
    max_sessions: int = 1

    @property
    def origins(self) -> set[str]:
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}

    @property
    def hosts(self) -> set[str]:
        return {urlsplit(origin).netloc for origin in self.origins}

    def validate(self) -> None:
        if not PROJECT_ENDPOINT_PATTERN.fullmatch(self.project_endpoint):
            raise ValueError(
                "FOUNDRY_PROJECT_ENDPOINT must look like "
                "https://<resource>.services.ai.azure.com/api/projects/<project>."
            )
        if self.tenant_id and not TENANT_PATTERN.fullmatch(self.tenant_id):
            raise ValueError("AZURE_TENANT_ID must be empty or a directory (tenant) GUID.")
        if self.webiq_endpoint != WEBIQ_ENDPOINT:
            raise ValueError("WEBIQ_ENDPOINT must be https://api.microsoft.ai/v3/auto for this adapter.")
        if not 1024 <= self.port <= 65535:
            raise ValueError("PORT must be between 1024 and 65535.")
        if self.max_sessions != 1 or not 1 <= self.session_seconds <= 600:
            raise ValueError("The local sample permits one conversation for up to 600 seconds.")

    @classmethod
    def load(cls) -> "Settings":
        env = {**dotenv_values(ROOT / ".env"), **os.environ}
        if env.get("PERF_LOG_ENABLED", "0") not in {"0", "1"}:
            raise ValueError("PERF_LOG_ENABLED must be 0 or 1.")
        if env.get("LOCAL_AGENT_VALIDATION_ENABLED", "1") not in {"0", "1"}:
            raise ValueError("LOCAL_AGENT_VALIDATION_ENABLED must be 0 or 1.")
        agent = json.loads(AGENT_FILE.read_text()) if AGENT_FILE.exists() else {}
        if not isinstance(agent, dict) or any(
            not isinstance(agent.get(key, ""), str) for key in ("name", "version", "project_endpoint")
        ):
            raise ValueError("The recorded agent metadata must contain string name/version/project_endpoint fields.")
        name = (env.get("FOUNDRY_VOICE_AGENT_NAME") or "").strip()
        version = (env.get("FOUNDRY_VOICE_AGENT_VERSION") or "").strip()
        if bool(name) != bool(version):
            raise ValueError("Set FOUNDRY_VOICE_AGENT_NAME and FOUNDRY_VOICE_AGENT_VERSION together.")
        endpoint = (env.get("FOUNDRY_PROJECT_ENDPOINT") or "").strip().rstrip("/")
        if (not name and agent.get("project_endpoint")
                and agent["project_endpoint"].rstrip("/") != endpoint):
            raise ValueError(
                "The recorded agent belongs to a different Foundry project. "
                "Set explicit agent name/version for this project or move .foundry/agent.json aside "
                "before running setup_agent for a new project."
            )
        settings = cls(
            project_endpoint=endpoint,
            tenant_id=(env.get("AZURE_TENANT_ID") or "").strip(),
            agent_name=name or agent.get("name", ""),
            agent_version=version or agent.get("version", ""),
            webiq_endpoint=env.get("WEBIQ_ENDPOINT") or WEBIQ_ENDPOINT,
            webiq_key=(env.get("WEBIQ_API_KEY") or "").strip(),
            port=int(env.get("PORT") or "8765"),
            perf_log_enabled=env.get("PERF_LOG_ENABLED", "0") == "1",
            local_agent_validation_enabled=env.get("LOCAL_AGENT_VALIDATION_ENABLED", "1") == "1",
        )
        settings.validate()
        return settings
