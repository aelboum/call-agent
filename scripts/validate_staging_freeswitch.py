#!/usr/bin/env python
"""Phase 2.23: one-shot real-FreeSWITCH protocol validation script.

**Staging-only.** Connects to a real, disposable FreeSWITCH instance over a
real ESL TCP socket, using this product's own `ManagedEslConnection` and
`FreeSwitchTelephonyProvider` (Phase 2.21/2.22 code, unmodified), and
exercises the real command/event wire protocol end to end: auth, an internal
`originate` that produces a real channel with a real `Unique-ID`, real
`CHANNEL_PARK`/`CHANNEL_ANSWER`/`CHANNEL_HANGUP_COMPLETE` events, and real
`uuid_answer`/`uuid_kill` commands -- the exact regression check for a real
bug this phase found and fixed: `FreeSwitchTelephonyProvider` was sending
these as bare ESL commands (`uuid_answer <uuid>`), which a real FreeSWITCH
server rejects with `-ERR command not found` (`mod_commands` APIs are only
recognized with an `api `/`bgapi ` prefix; only `FakeEslConnection` ever
accepted the bare form). Also confirms `origination_uuid` really does
become the channel's own `Unique-ID` end to end (the exact assumption
Phase 2.21 flagged as unverified against a live server).

Never performs a real PSTN/SIP call -- no SIP trunk or softphone is
involved. The channel is created via FreeSWITCH's own `originate` ESL API
against the `null` endpoint (a real channel with no real media/signaling
device backing it -- FreeSWITCH's own supported mechanism for exactly this
kind of internal test), running `&park()` directly. Note: the `null`
endpoint auto-answers itself as part of channel creation (no real ringing
phase), so `CHANNEL_ANSWER` is observed as already having happened by the
time this script gets to call `answer()` itself -- `answer()`'s own command
success (not the answered *event*, which already passed) is what this
script's "real command must not error against a live server" check
actually depends on. See docs/PHASE-2.23-REAL-STAGING-E2E.md for why a
`loopback`-endpoint alternative (which *does* stay genuinely unanswered
until an explicit `uuid_answer`) was tried and rejected: `loopback`'s own
two-leg (`-a`/`-b`) split means `origination_uuid` only pins the `-a`
leg's UUID, and the dialplan-routed `-b` leg's `CHANNEL_PARK` carries a
different, FreeSWITCH-generated UUID -- a real, generalizable finding
about `loopback` specifically, but irrelevant to this product's own
`originate()`/`transfer()` (single-leg `sofia/gateway/...`) or to a real
single-leg inbound SIP call, so not worth chasing further here.

Usage::

    python scripts/validate_staging_freeswitch.py --host 127.0.0.1 --port 18021 --password ClueCon

Exit code 0 only if every check passes; non-zero and a clear message
naming which boundary failed otherwise. Never prints the ESL password
value itself beyond confirming a successful auth reply.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import uuid

from voiceagent.telephony.contracts import CallEvent, CallEventType, HangupCause
from voiceagent.telephony.freeswitch.esl_transport import ManagedEslConnection
from voiceagent.telephony.freeswitch.provider import FreeSwitchTelephonyProvider


async def _run(host: str, port: int, password: str) -> int:
    esl = ManagedEslConnection(host=host, port=port, password_provider=lambda: password)
    print(f"[1/6] connecting to real FreeSWITCH ESL at {host}:{port} ...")
    try:
        await esl.start()
    except Exception as exc:  # noqa: BLE001 -- report, don't traceback-spam
        print(f"FAIL: could not connect/authenticate to FreeSWITCH ESL: {exc}")
        return 1
    print("      connected and authenticated (real auth/request -> auth -> +OK)")

    provider = FreeSwitchTelephonyProvider(esl)
    collected: list[CallEvent] = []

    async def _drain() -> None:
        # A single, never-restarted consumer of `provider.events()` for the
        # whole script -- pausing and resuming iteration of the *same*
        # async generator across separate bounded windows does not work
        # here: a timeout cancels the generator's own suspended
        # `await self._event_queue.get()`, which leaves it unable to
        # produce anything on a later resumption (silently -- no error, it
        # would just look like no events ever arrived). One long-lived
        # background task sidesteps that entirely.
        async for event in provider.events():
            collected.append(event)

    drain_task = asyncio.create_task(_drain())

    async def _collect_for(seconds: float) -> None:
        await asyncio.sleep(seconds)

    call_ref = str(uuid.uuid4())
    print(f"[2/6] originating a real internal channel, origination_uuid={call_ref} ...")
    reply = await esl.send(
        f"bgapi originate "
        f"{{origination_uuid={call_ref},origination_caller_id_number=+15550100}}"
        f"null/anything &park()"
    )
    if reply.startswith("-ERR"):
        print(f"FAIL: originate rejected: {reply!r}")
        await _shutdown(drain_task, esl)
        return 1
    print("      bgapi originate accepted")

    print("[3/6] collecting real lifecycle events for this call_ref ...")
    await _collect_for(3.0)
    mine = [e for e in collected if e.call_ref == call_ref]
    offered = next((e for e in mine if e.type is CallEventType.OFFERED), None)
    auto_answered = next((e for e in mine if e.type is CallEventType.ANSWERED), None)
    if offered is None:
        print(f"FAIL: no OFFERED event observed for {call_ref} (saw: {[e.type for e in mine]})")
        await _shutdown(drain_task, esl)
        return 1
    print(f"      OFFERED observed, call_ref matches origination_uuid exactly: {call_ref}")
    print(f"      from_number={offered.from_number!r} to_number={offered.to_number!r}")
    if auto_answered is not None:
        print("      (this channel already self-answered on creation -- expected for `null`)")

    print("[4/6] sending a real uuid_answer command (regression check for the api-prefix bug) ...")
    try:
        await provider.answer(call_ref)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: real FreeSWITCH rejected uuid_answer: {exc}")
        await _shutdown(drain_task, esl)
        return 1
    print("      real FreeSWITCH accepted `api uuid_answer` with no error")

    print("[5/6] hanging up the real channel (uuid_kill) ...")
    await provider.hangup(call_ref, HangupCause.NORMAL)
    await _collect_for(3.0)
    hungup = next(
        (e for e in collected if e.call_ref == call_ref and e.type is CallEventType.HUNGUP), None
    )
    if hungup is None:
        print("FAIL: no matching HUNGUP event observed after uuid_kill")
        await _shutdown(drain_task, esl)
        return 1
    print(f"      real CHANNEL_HANGUP_COMPLETE observed, hangup_cause={hungup.hangup_cause}")

    print("[6/6] closing the ESL connection ...")
    await _shutdown(drain_task, esl)
    print("PASS: all real FreeSWITCH ESL protocol checks succeeded")
    return 0


async def _shutdown(drain_task: asyncio.Task[None], esl: ManagedEslConnection) -> None:
    drain_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await drain_task
    await esl.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18021)
    parser.add_argument("--password", default="ClueCon")
    args = parser.parse_args()
    return asyncio.run(_run(args.host, args.port, args.password))


if __name__ == "__main__":
    raise SystemExit(main())
