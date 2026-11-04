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
WEBIQ_ENDPOINT = "https://api.microsoft.ai/v3/mcp"
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
    perf_log_enabled: bool = True
    local_agent_validation_enabled: bool = True
    mode: str = "local"
    public_origin: str = ""
    auth_verifier: str = field(default="", repr=False)
    managed_identity_client_id: str | None = None
    max_sessions: int = 1

    @property
    def hosted(self) -> bool:
        return self.mode == "hosted"

    @property
    def origins(self) -> set[str]:
        if self.hosted:
            return {self.public_origin}
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
            raise ValueError("WEBIQ_ENDPOINT must be https://api.microsoft.ai/v3/mcp for this adapter.")
        if not 1024 <= self.port <= 65535:
            raise ValueError("PORT must be between 1024 and 65535.")
        if self.mode not in {"local", "hosted"}:
            raise ValueError("APP_MODE must be local or hosted.")
        if not 1 <= self.max_sessions <= 10 or not 1 <= self.session_seconds <= 600:
            raise ValueError("Session limits must be 1-10 conversations and 1-600 seconds.")
        if self.hosted:
            origin = urlsplit(self.public_origin)
            if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                    or origin.path or origin.query or origin.fragment or origin.port not in {None, 443}
                    or origin.hostname in {"localhost", "127.0.0.1", "::1"}
                    or self.public_origin != f"https://{origin.netloc}"):
                raise ValueError("PUBLIC_ORIGIN must be one exact HTTPS origin without a path.")
            from app.access import parse_verifier
            parse_verifier(self.auth_verifier)
            if not self.agent_name or not self.agent_version or not self.webiq_key:
                raise ValueError("Hosted mode requires explicit agent name/version and backend WebIQ credentials.")

    @classmethod
    def load(cls) -> "Settings":
        # Hosting never loads developer secrets or agent state from the checkout.
        hosted = os.environ.get("APP_MODE") == "hosted"
        env = dict(os.environ) if hosted else {**dotenv_values(ROOT / ".env"), **os.environ}
        if env.get("PERF_LOG_ENABLED", "1") not in {"0", "1"}:
            raise ValueError("PERF_LOG_ENABLED must be 0 or 1.")
        if env.get("LOCAL_AGENT_VALIDATION_ENABLED", "1") not in {"0", "1"}:
            raise ValueError("LOCAL_AGENT_VALIDATION_ENABLED must be 0 or 1.")
        agent = json.loads(AGENT_FILE.read_text()) if not hosted and AGENT_FILE.exists() else {}
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
            perf_log_enabled=env.get("PERF_LOG_ENABLED", "1") == "1",
            local_agent_validation_enabled=env.get("LOCAL_AGENT_VALIDATION_ENABLED", "1") == "1",
            mode=env.get("APP_MODE") or "local",
            public_origin=env.get("PUBLIC_ORIGIN") or "",
            auth_verifier=env.get("DEMO_AUTH_VERIFIER") or "",
            managed_identity_client_id=env.get("AZURE_CLIENT_ID") or None,
            max_sessions=int(env.get("MAX_CONVERSATIONS") or ("10" if hosted else "1")),
        )
        settings.validate()
        return settings
