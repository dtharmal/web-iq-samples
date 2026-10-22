# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from dataclasses import dataclass
from time import perf_counter
from typing import Literal

ToolName = Literal["webiq.news", "cite_sources"]
ToolState = Literal["pending", "running", "completed", "failed", "cancelled"]


@dataclass
class ToolActivity:
    session_id: str
    call_id: str
    turn: int
    tool: ToolName
    state: ToolState = "pending"
    started_at: float | None = None
    elapsed_ms: float | None = None
    source_count: int | None = None
    response_received: bool = False

    def start_clock(self) -> None:
        self.started_at = perf_counter()

    def stop_clock(self, *, response_received: bool = False) -> None:
        if self.started_at is not None and self.elapsed_ms is None:
            self.elapsed_ms = (perf_counter() - self.started_at) * 1000
            self.response_received = response_received

    def event(self) -> dict:
        return {
            "session_id": self.session_id,
            "call_id": self.call_id,
            "turn": self.turn,
            "tool": self.tool,
            "state": self.state,
            "scope": "mcp_round_trip" if self.tool == "webiq.news" else "local_citation_validation",
            "elapsed_ms": self.elapsed_ms,
            "source_count": self.source_count,
            "response_received": self.response_received,
        }
