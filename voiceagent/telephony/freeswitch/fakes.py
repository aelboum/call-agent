"""Deterministic `EslConnection`/`MediaSocket` doubles, so
`FreeSwitchTelephonyProvider`/`FreeSwitchMediaProvider` are testable with no
running FreeSWITCH instance (this phase's brief section 9: "do not require a
running FreeSWITCH instance in normal CI").

Fakes, not mocks, matching `voiceagent.telephony.fakes`'s own rationale: they
hold real state (every command sent, every event/frame pushed) and behave
like a small, honest ESL peer.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from voiceagent.telephony.freeswitch.esl import EslEvent

__all__ = ["FakeEslConnection", "FakeMediaSocket"]


class FakeEslConnection:
    """Records every command it receives (`commands`); replays events pushed
    with `push_event()`; returns a scripted response per command via
    `responses`, defaulting to `"+OK"`."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.responses: dict[str, str] = {}
        self._events: asyncio.Queue[EslEvent | None] = asyncio.Queue()

    def push_event(self, event: EslEvent) -> None:
        self._events.put_nowait(event)

    def close_event_stream(self) -> None:
        self._events.put_nowait(None)

    async def send(self, command: str) -> str:
        self.commands.append(command)
        return self.responses.get(command, "+OK")

    async def events(self) -> AsyncIterator[EslEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event


class FakeMediaSocket:
    """Records every JSON text frame sent (`sent_text`); replays binary
    frames pushed with `push_binary()`."""

    def __init__(self) -> None:
        self.sent_text: list[str] = []
        self.closed = False
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()

    def push_binary(self, frame: bytes) -> None:
        self._inbound.put_nowait(frame)

    def end_inbound(self) -> None:
        self._inbound.put_nowait(None)

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def receive_binary(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._inbound.get()
            if frame is None:
                return
            yield frame

    async def close(self) -> None:
        self.closed = True
        self.end_inbound()
