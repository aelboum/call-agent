#!/usr/bin/env python
"""Phase 2.24: real bidirectional FreeSWITCH media validation.

**Staging-only.** Uses this product's own, unmodified `FreeSwitchMediaListener`
/`FreeSwitchMediaProvider`/`FreeSwitchTelephonyProvider` against a real
FreeSWITCH instance that actually has `mod_audio_stream` loaded (Phase
2.23's own validated image, `safarov/freeswitch`, does not -- see
`docs/PHASE-2.24-REAL-MEDIA-CALL-E2E.md` section 1 for the image used
here instead).

What this proves, all for real:

1. A real `wss://` listener, bound on this host, is reachable from inside
   the FreeSWITCH container.
2. `api uuid_audio_stream <uuid> start <wss-url> mono 8k` against the real
   server actually makes real FreeSWITCH open a real WebSocket connection
   to that listener.
3. The connection presents a real signed ticket
   (`voiceagent.telephony.freeswitch.media_transport.mint_media_ticket`),
   which the listener verifies *before* ever calling `register_socket()`
   -- an unverifiable connection is never handed to `MediaProvider.attach()`.
4. Real raw binary PCM frames arrive from FreeSWITCH
   (`_FreeSwitchMediaStream.receive()`).
5. A real JSON `streamAudio` envelope sent back
   (`_FreeSwitchMediaStream.send()`) is accepted by the live server with no
   error.

**What this does NOT prove**: that the audio content itself is a real
caller's speech -- the channel here is FreeSWITCH's own `null` endpoint
(no real signaling device), so any PCM it emits is FreeSWITCH's own
silence/comfort-noise generator, not a human voice. This script validates
the *transport*, not a real conversation; `scripts/validate_staging_call_e2e
.py` (Phase 2.23) and any real SIP-sourced follow-on validate call
*routing*; a real spoken round trip needs both at once, over a real
caller leg -- see the Phase 2.24 doc for exactly how far this goes.

Usage::

    python scripts/validate_staging_media_e2e.py \\
        --fs-host 127.0.0.1 --fs-port 18023 --fs-password ClueCon \\
        --listen-host 0.0.0.0 --listen-port 8199 \\
        --public-base-url ws://<host-reachable-from-the-container>:8199

**A real, generalizable finding from validating this**: this build of
`mod_audio_stream` (`audio_streamer_glue.cpp`'s own websocket client, not
this product's code) failed every connection attempt instantly
(`connection error`, no real TCP attempt observed in FreeSWITCH's own log)
when given `host.docker.internal` as the hostname -- it does not resolve
hostnames the way a normal DNS client does. Passing the container's actual
reachable IP for the host (Docker Desktop's own gateway, e.g.
`192.168.65.254`; `getent hosts host.docker.internal` inside the
container prints it) works. `--public-base-url` therefore defaults to a
placeholder, not a hostname -- pass the real reachable IP for your own
Docker setup explicitly.

Exit code 0 only if every check passes. Never prints a secret, a ticket,
or raw audio content (byte counts only).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import uuid

from voiceagent.telephony.freeswitch.esl_transport import ManagedEslConnection
from voiceagent.telephony.freeswitch.media import FreeSwitchMediaProvider
from voiceagent.telephony.freeswitch.media_transport import (
    FreeSwitchMediaListener,
    serve_freeswitch_media,
)
from voiceagent.telephony.freeswitch.provider import FreeSwitchTelephonyProvider

_TICKET_SECRET = "phase224-validation-secret"  # noqa: S105 -- disposable, this run only.  # pragma: allowlist secret


async def _run(
    fs_host: str,
    fs_port: int,
    fs_password: str,
    listen_host: str,
    listen_port: int,
    public_base_url: str,
) -> int:
    media = FreeSwitchMediaProvider()
    listener = FreeSwitchMediaListener(media, ticket_secret=_TICKET_SECRET)

    print(f"[1/6] starting a real wss:// listener on {listen_host}:{listen_port} ...")
    server = await serve_freeswitch_media(listener, host=listen_host, port=listen_port)
    print(f"      listening for real; FreeSWITCH will reach it via {public_base_url}")

    print(f"[2/6] connecting to real FreeSWITCH ESL at {fs_host}:{fs_port} ...")
    esl = ManagedEslConnection(host=fs_host, port=fs_port, password_provider=lambda: fs_password)
    try:
        await esl.start()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: could not connect/authenticate to FreeSWITCH ESL: {exc}")
        server.close()
        await server.wait_closed()
        return 1
    print("      connected and authenticated")

    telephony = FreeSwitchTelephonyProvider(
        esl,
        media_public_base_url=public_base_url,
        media_ticket_secret_provider=lambda: _TICKET_SECRET,
    )
    events = telephony.events()
    collected = []

    async def _drain() -> None:
        async for event in events:
            collected.append(event)

    drain_task = asyncio.create_task(_drain())

    call_ref = str(uuid.uuid4())
    print(f"[3/6] originating a real channel and starting real media, call_ref={call_ref} ...")
    reply = await esl.send(f"bgapi originate {{origination_uuid={call_ref}}}null/anything &park()")
    if reply.startswith("-ERR"):
        print(f"FAIL: originate rejected: {reply!r}")
        await _shutdown(drain_task, esl, server)
        return 1
    await asyncio.sleep(1.0)

    try:
        await telephony.start_media_stream(call_ref)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: real FreeSWITCH rejected start_media_stream: {exc}")
        await _shutdown(drain_task, esl, server)
        return 1
    print("      real `api uuid_audio_stream ... start` accepted by the live server")

    print("[4/6] waiting for FreeSWITCH's own real WebSocket connection and attaching ...")
    stream = None
    deadline = asyncio.get_running_loop().time() + 10.0
    while asyncio.get_running_loop().time() < deadline:
        try:
            stream = await media.attach(call_ref)
            break
        except Exception:  # noqa: BLE001 -- "no socket registered yet"; keep polling.
            await asyncio.sleep(0.1)
    if stream is None:
        print("FAIL: FreeSWITCH never connected back to our real wss:// listener")
        await _shutdown(drain_task, esl, server)
        return 1
    print(
        "      real socket registered and attached -- FreeSWITCH really connected, ticket verified"
    )

    print("[5/6] reading real inbound PCM frames ...")
    frames_seen = 0

    async def _count_frames() -> None:
        nonlocal frames_seen
        async for _frame in stream.receive():
            frames_seen += 1
            if frames_seen >= 3:
                return

    try:
        await asyncio.wait_for(_count_frames(), timeout=10.0)
    except TimeoutError:
        pass
    if frames_seen == 0:
        print("FAIL: no real inbound PCM frames were received")
        await _shutdown(drain_task, esl, server)
        return 1
    print(f"      PASS: received {frames_seen} real inbound PCM frame(s) from FreeSWITCH")

    print("[6/6] sending a real outbound frame back and hanging up ...")
    try:
        await stream.send(b"\x00\x00" * 160)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: real FreeSWITCH/socket rejected our outbound frame: {exc}")
        await _shutdown(drain_task, esl, server)
        return 1
    print("      PASS: real server accepted a real outbound streamAudio frame")

    await telephony.hangup(call_ref)
    await media.detach(call_ref)
    await _shutdown(drain_task, esl, server)
    print("PASS: real bidirectional FreeSWITCH media transport validated end to end")
    return 0


async def _shutdown(drain_task, esl: ManagedEslConnection, server) -> None:
    drain_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await drain_task
    await esl.close()
    server.close()
    await server.wait_closed()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fs-host", default="127.0.0.1")
    parser.add_argument("--fs-port", type=int, default=18023)
    parser.add_argument("--fs-password", default="ClueCon")
    parser.add_argument("--listen-host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--listen-port", type=int, default=8199)
    parser.add_argument(
        "--public-base-url",
        default="ws://REPLACE-WITH-A-CONTAINER-REACHABLE-IP:8199",
        help=(
            "A real websocket base URL the FreeSWITCH container can reach -- "
            "a hostname does not reliably work against every mod_audio_stream "
            "build (see this module's own docstring); pass a raw IP."
        ),
    )
    args = parser.parse_args()
    return asyncio.run(
        _run(
            args.fs_host,
            args.fs_port,
            args.fs_password,
            args.listen_host,
            args.listen_port,
            args.public_base_url,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
