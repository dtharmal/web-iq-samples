# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""One genuine typed-input turn through the local WebSocket; never writes audio to disk."""

import asyncio
import base64
import json

import websockets

from app.config import Settings


async def main():
    settings = Settings.load()
    audio_bytes = 0
    sources = []
    transcript = ""
    async with (
        asyncio.timeout(100),
        websockets.connect(
            f"ws://127.0.0.1:{settings.port}/api/voice",
            origin=f"http://127.0.0.1:{settings.port}",
            max_size=512_000,
        ) as ws,
    ):
        async for raw in ws:
            event = json.loads(raw)
            kind = event["type"]
            if kind == "ready":
                print("Native model:", event["model"])
                await ws.send(json.dumps({
                    "type": "text",
                    "text": "What is one recent Microsoft Foundry voice agent announcement? Be concise.",
                }))
            elif kind == "interrupt":
                await ws.send(json.dumps({"type": "playback", "turn": event["turn"], "items": []}))
            elif kind == "evidence":
                sources = event["sources"]
            elif kind == "transcript" and event["role"] == "assistant" and event["final"] and event["turn"] > 0:
                transcript = event["text"]
            elif kind == "audio" and event["turn"] > 0:
                audio_bytes += len(base64.b64decode(event["data"], validate=True))
            elif kind == "error":
                raise RuntimeError(f"{event['code']}: {event['message']}")
            elif kind == "done" and event["turn"] > 0:
                if not (sources and transcript and audio_bytes):
                    raise RuntimeError("Missing fresh evidence, transcript or native audio.")
                print("Spoken transcript received; content not logged.")
                print("Retrieved evidence count:", len(sources))
                print(f"Native PCM16: {audio_bytes} bytes; {audio_bytes / 48000:.2f} seconds; not retained.")
                print("No output device: playback and post-speech citations are not verified by this probe.")
                await ws.send(json.dumps({
                    "type": "playback_complete", "turn": event["turn"],
                    "response_id": event["id"], "outcome": "unavailable",
                }))
                await ws.send(json.dumps({"type": "end"}))
                return
    raise RuntimeError("Session closed before the verified answer completed.")


if __name__ == "__main__":
    asyncio.run(main())
