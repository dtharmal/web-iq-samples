# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class StrictMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Text(StrictMessage):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=1000)
    client_turn_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )


class Mute(StrictMessage):
    type: Literal["mute"]
    muted: bool


class Played(StrictMessage):
    item_id: str = Field(min_length=1, max_length=200)
    content_index: int = Field(ge=0, le=10)
    audio_end_ms: int = Field(ge=0, le=90_000)


class Playback(StrictMessage):
    type: Literal["playback"]
    turn: int = Field(ge=0)
    items: list[Played] = Field(max_length=20)


class PlaybackComplete(StrictMessage):
    type: Literal["playback_complete"]
    turn: int = Field(ge=1, le=40)
    response_id: str = Field(min_length=1, max_length=200)
    outcome: Literal["played", "no_audio", "unavailable"]


class Interrupt(StrictMessage):
    type: Literal["interrupt"]


class End(StrictMessage):
    type: Literal["end"]

Milliseconds = Annotated[float, Field(ge=0, le=900_000, allow_inf_nan=False)]


class ClientTiming(StrictMessage):
    type: Literal["client_timing"]
    turn: int = Field(ge=1, le=40)
    phase: Literal["audio_received", "playback_started", "playback_unavailable", "ui_render",
                   "playback_error", "playback_buffered"]
    response_id: str | None = Field(default=None, min_length=1, max_length=200)
    call_id: str | None = Field(default=None, min_length=1, max_length=200)
    ui: Literal["activity", "evidence", "answer", "citations"] | None = None
    browser_submit_to_receipt_ms: Milliseconds | None = None
    browser_submit_to_playback_ms: Milliseconds | None = None
    browser_receipt_to_playback_ms: Milliseconds | None = None
    browser_schedule_lead_ms: Milliseconds | None = None
    browser_output_latency_ms: Milliseconds | None = None
    browser_render_ms: Milliseconds | None = None
    browser_queue_ms: Milliseconds | None = None
    browser_audio_context_ms: Milliseconds | None = None
    browser_clock_stall_ms: Milliseconds | None = None
    browser_received_audio_ms: Milliseconds | None = None
    context_state: Literal["running", "suspended", "closed", "interrupted"] | None = None
    code: Literal["playback_suspended", "playback_stalled", "playback_limit", "playback_invalid"] | None = None


MESSAGE = TypeAdapter(Annotated[
    Text | Mute | Playback | PlaybackComplete | Interrupt | End | ClientTiming, Field(discriminator="type"),
])
