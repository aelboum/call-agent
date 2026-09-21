# ADR-0002: FreeSWITCH is the telephony core, not a provider

Status: Accepted (Phase 0)
Date: 2026-09-21

## Context

Voice-AI platforms commonly abstract telephony behind a provider interface so
Twilio, Plivo, Vonage, Telnyx, Asterisk and FreeSWITCH can be swapped. Dograh
does exactly this: its `TelephonyProvider` abstract base class carries roughly
twenty-five abstract methods, because one interface must satisfy both
hosted-CPaaS semantics (webhook signature verification, status callbacks,
per-call cost lookup, number provisioning) and PBX semantics (ESL/ARI channel
control, external media sockets, bridge-based transfers). Its own
`WorkflowRunMode` enum lists ten transport modes.

This product's brief states that FreeSWITCH is the PBX/telephony core and is not
to be treated as a generic SaaS provider. The question this ADR settles is where
the varying part of telephony actually is, and therefore where a provider seam
belongs — if anywhere.

## Decision

1. **FreeSWITCH is the single telephony core.** There is exactly one
   implementation of the product's telephony boundary,
   `FreeSwitchTelephony`. This is a deliberate architectural commitment, not
   an unfinished abstraction.
2. **The provider seam sits below FreeSWITCH, not around it.** Carrier variation
   is expressed as SIP trunks and DID vendors — a `SipTrunk` row plus a
   FreeSWITCH gateway profile — not as a Python provider class. A change of
   carrier requires no product code.
3. **A hosted CPaaS, if ever required, enters as a SIP trunk into FreeSWITCH**,
   never as a parallel telephony provider interface.
4. **Control channel: ESL in inbound mode.** The product connects out to
   FreeSWITCH's `mod_event_socket`; FreeSWITCH does not open a connection back
   per call. Rejected: a raw SIP/RTP bridge (reimplementing a softswitch);
   `mod_xml_rpc` (no first-class ARI-equivalent REST control plane); ESL
   outbound mode (requires a bespoke `socket` dialplan application per
   extension, where inbound mode needs only `park()`).
5. **Media: a WebSocket audio bridge** via `mod_audio_stream`, pinned to
   v1.0.3 or later, with its wire protocol verified against module source and
   asserted by a conformance test. The module is asymmetric — raw binary L16 PCM
   from FreeSWITCH, JSON text frames carrying base64 L16 back — and is not
   interchangeable with `mod_audio_fork`.
6. **Responsibility split is absolute.** FreeSWITCH owns SIP, RTP, codecs, DTMF,
   bridging, hold, transfer execution, recording capture and channel lifecycle.
   The product owns tenant resolution, agent selection, conversation, tool
   authorization, storage and billing. FreeSWITCH never calls an AI provider and
   never holds product state; the product never parses SDP.
7. **FreeSWITCH is not an authority on identity.** Everything arriving over ESL
   — called number, caller ID, channel variables — is input to a server-side
   resolution, never an authorization decision. See ADR-0003 and Phase 0 report
   §14.1.

## Consequences

Positive: one call model, one transfer semantics, one recording path, one set of
failure modes. Self-hosted media means no per-minute CPaaS media markup and full
control over recording and residency.

Negative and accepted: FreeSWITCH operational expertise becomes a hard
requirement (SIP edge hardening, NAT, codecs, scanner abuse, capacity); the
resilience set in Phase 0 report §10.6 — parked-channel reaper, ESL reconnection
with channel/session reconciliation, two-layer concurrency limits — is mandatory
rather than optional; adding a hosted CPaaS later is a trunk-configuration
exercise, which is the intended trade.

## What would be difficult to change later

Committing to one core is easy to reverse in principle and hard in practice:
call-control semantics (transfer, hold, recording, hangup causes) leak into the
domain model. That is precisely why the split in point 6 is stated as absolute —
it is what keeps the leak bounded.

## What is deliberately not decided here

Deployment topology (single node vs pool, SIP edge ownership, how a media socket
is routed to the right runtime process when runtimes scale horizontally) —
Phase 0 report §19 OD-5. Codec policy and whether to upsample the 8 kHz PSTN leg
for a provider. Recording capture strategy (FreeSWITCH-side vs
product-side-from-the-media-stream).

## Related

`docs/PHASE-0-ARCHITECTURE.md` §10, §3.2, §3.4.

---

## Amendment — 2026-09-21 (Phase 0.1): TelephonyProvider and MediaProvider boundaries

Status of the core decision: **unchanged.** The Phase 0.1 blocker analysis
(OD-1, OD-2, OD-4) surfaced no technical contradiction with this ADR, and
FreeSWITCH remains the initial telephony/media core. Nothing below reopens that.

What this amendment adds is an explicit *internal* boundary, so that "one
implementation" (point 1) never degrades into "FreeSWITCH concepts scattered
through the domain".

1. **Two product-owned interfaces are introduced**, and FreeSWITCH becomes an
   implementation behind them rather than a dependency of the application:

   - **`TelephonyProvider`** — call control. Originate, answer, hang up,
     transfer/bridge, hold/unhold, send DTMF, start/stop recording, and a
     normalized stream of call-lifecycle events (ringing, parked, answered,
     destination-answered, hung-up with a normalized cause).
   - **`MediaProvider`** — media transport. Attach and detach a bidirectional
     PCM stream for a given call leg, negotiate frame format and sample rate,
     and report stream health. It owns the socket; the `ConversationEngine`
     does not (ADR-0006 point 9).

2. **Implementations**: `FreeSwitchTelephonyProvider` (ESL, inbound mode) and
   `FreeSwitchMediaProvider` (`mod_audio_stream` WebSocket). These are the only
   implementations, plus in-memory fakes for tests.

3. **These interfaces are narrow and product-shaped, not a vendor union.** They
   are defined by what this product's call lifecycle (Phase 0 report §10.5)
   actually needs — and by nothing else. Specifically excluded, and to stay
   excluded: webhook signature verification, status-callback parsing, per-call
   cost lookup, phone-number provisioning, answering-machine-detection
   parameters, and any other method that exists only because some hosted CPaaS
   has it. Growing this interface toward the union of every vendor's vocabulary
   is the failure mode recorded in Phase 0 report §3.4 and rejected there.

4. **Domain code never imports the FreeSWITCH implementation**, never sees an
   ESL command, a channel UUID semantic, or a FreeSWITCH hangup cause string.
   Channel identifiers are carried as opaque values on `CallSession`; hangup
   causes are normalized at the boundary. An import-linter contract confines
   the ESL client and `mod_audio_stream` framing to
   `voiceagent.telephony.freeswitch`.

5. **Separating the two interfaces is deliberate.** Control and media have
   different lifetimes, different failure modes and different scaling
   properties: a media socket can drop while the channel stays up, and media
   may later terminate on a different process than the one holding the ESL
   connection (Phase 0 report §19 OD-5). One combined interface would hide that.

6. **This is not a portability promise.** The interfaces exist to keep
   FreeSWITCH out of the domain, not to advertise that another PBX can be
   dropped in. Carrier variation remains a SIP-trunk concern (point 2 of the
   original decision), and a second implementation is not a goal.

Related: ADR-0006 (engine/transport split); `docs/PHASE-0-ARCHITECTURE.md`
§10, §22.
