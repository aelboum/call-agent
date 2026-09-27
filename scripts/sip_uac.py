"""Phase 2.25: a minimal, purpose-built real SIP user agent client (UAC).

**Staging validation tooling only** -- not product code, not imported by
anything under `voiceagent/`. Exists because no scriptable/CLI SIP client
was installable in this environment beyond `pyVoIP` (pure-Python, pip
`pyvoip`), and `pyVoIP`'s own `VoIPPhone`/`SIPClient` hard-couples every
outbound call to a successful SIP REGISTER first (`SIPClient.start()`
always calls `self.register()`, and `register()` calls `self.stop()` --
closing the client's own socket -- after
`pyVoIP.REGISTER_FAILURE_THRESHOLD` consecutive failures). The target
FreeSWITCH profile here (`external`, `auth-calls=false`, no directory/
registrar for arbitrary peers -- see `docs/PHASE-2.25-REAL-SIP-SPOKEN-E2E
.md` section 2) is a trunk-style unauthenticated-inbound profile that was
never meant to be registered against, so that assumption does not fit and
monkeypatching around it (attempted first, see git history of this file's
own development) proved fragile enough that a small, explicit, from-scratch
INVITE/ACK/BYE state machine -- using this environment's real UDP sockets
directly, no retransmission timers, single-peer only -- is more honest and
robust for a fixed, controlled staging validation than fighting a library
built for a different shape of client.

**RTP media is not hand-rolled** -- `pyVoIP.RTP.RTPClient` (a real,
independent, non-registration-coupled component) is used unchanged for
real RTP packet framing, sequencing, timestamping, and PCMU encode/decode;
this module only hand-rolls the SIP signaling layer.

This is a real SIP UAC: it sends a real `INVITE` with a real SDP offer over
a real UDP socket to a real FreeSWITCH SIP profile, parses the real final
response, sends a real `ACK`, and can send/receive real RTP media and a
real `BYE`. It authenticates nothing and expects nothing back requiring
authentication, matching the specific, documented external profile this
phase targets.
"""

from __future__ import annotations

import random
import socket
import string
import time
from dataclasses import dataclass, field

from pyVoIP.RTP import PayloadType, RTPClient, TransmitType

_SIP_VERSION = "SIP/2.0"


def _token(n: int = 10) -> str:
    # SIP Call-ID/tag/branch uniqueness, not a security token -- collision
    # resistance, not unpredictability, is what matters here.
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))  # noqa: S311


@dataclass
class SipCallResult:
    """Outcome of placing one real SIP call."""

    call_id: str
    final_status: int | None = None
    remote_rtp_ip: str | None = None
    remote_rtp_port: int | None = None
    remote_tag: str | None = None
    provisional_statuses: list[int] = field(default_factory=list)
    remote_hung_up: bool = False


class SipMessageError(RuntimeError):
    """A SIP response could not be parsed or was malformed."""


def _parse_status_line(raw: bytes) -> tuple[int, str]:
    first_line = raw.split(b"\r\n", 1)[0].decode("utf-8", "replace")
    parts = first_line.split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("SIP/2.0"):
        raise SipMessageError(f"not a SIP response: {first_line!r}")
    return int(parts[1]), parts[2] if len(parts) > 2 else ""


def _parse_headers(raw: bytes) -> dict[str, str]:
    head = raw.split(b"\r\n\r\n", 1)[0]
    headers: dict[str, str] = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        headers[name.decode("utf-8", "replace").strip().lower()] = value.decode(
            "utf-8", "replace"
        ).strip()
    return headers


def _parse_sdp_media(raw: bytes) -> tuple[str | None, int | None]:
    body = raw.split(b"\r\n\r\n", 1)
    if len(body) < 2:
        return None, None
    sdp = body[1].decode("utf-8", "replace")
    ip: str | None = None
    port: int | None = None
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
        elif line.startswith("m=audio "):
            port = int(line.split()[1])
    return ip, port


class SipUac:
    """One real SIP UDP endpoint able to place and tear down one call.

    Deliberately single-call, single-peer, no retransmission timers, no
    authentication: exactly what this phase's own controlled staging
    validation needs, and nothing more.
    """

    def __init__(
        self,
        *,
        local_ip: str,
        local_sip_port: int,
        local_rtp_port: int,
        remote_host: str,
        remote_port: int,
        advertise_ip: str,
    ) -> None:
        """`advertise_ip` is what this UAC puts in its own Via/Contact/SDP --
        it must be reachable *from the SIP peer's own network*, which for a
        containerized FreeSWITCH is not `local_ip` (see this repo's own
        Phase 2.24 finding re: `host.docker.internal`/Docker Desktop's
        gateway address, documented again in
        docs/PHASE-2.25-REAL-SIP-SPOKEN-E2E.md).
        """
        self.local_ip = local_ip
        self.local_sip_port = local_sip_port
        self.local_rtp_port = local_rtp_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.advertise_ip = advertise_ip

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((local_ip, local_sip_port))
        self._sock.settimeout(0.5)

        self._call_id = f"{_token(16)}@{advertise_ip}"
        self._from_tag = _token(8)
        self._branch = f"z9hG4bK{_token(16)}"
        self._cseq = 1
        self._local_uri = f"sip:phase225@{advertise_ip}:{local_sip_port}"
        self._remote_target: tuple[str, int] = (remote_host, remote_port)

        self.rtp: RTPClient | None = None

    def close(self) -> None:
        if self.rtp is not None:
            self.rtp.stop()
            self.rtp = None
        self._sock.close()

    def _sdp_offer(self) -> str:
        sess_id = int(time.time())
        return (
            "v=0\r\n"
            f"o=phase225 {sess_id} {sess_id} IN IP4 {self.advertise_ip}\r\n"
            "s=phase225-e2e\r\n"
            f"c=IN IP4 {self.advertise_ip}\r\n"
            "t=0 0\r\n"
            f"m=audio {self.local_rtp_port} RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=sendrecv\r\n"
        )

    def _build_invite(self, to_user: str, from_user: str) -> bytes:
        sdp = self._sdp_offer()
        request_uri = f"sip:{to_user}@{self.remote_host}:{self.remote_port}"
        lines = [
            f"INVITE {request_uri} {_SIP_VERSION}",
            (
                f"Via: SIP/2.0/UDP {self.advertise_ip}:{self.local_sip_port}"
                f";branch={self._branch};rport"
            ),
            "Max-Forwards: 70",
            (f"From: <sip:{from_user}@{self.advertise_ip}>;tag={self._from_tag}"),
            f"To: <sip:{to_user}@{self.remote_host}>",
            f"Call-ID: {self._call_id}",
            f"CSeq: {self._cseq} INVITE",
            f"Contact: <{self._local_uri}>",
            "User-Agent: phase225-sip-uac/1.0",
            "Content-Type: application/sdp",
            f"Content-Length: {len(sdp.encode('utf-8'))}",
            "",
            sdp,
        ]
        return "\r\n".join(lines).encode("utf-8")

    def invite(
        self, to_user: str, from_user: str = "phase225caller", *, final_timeout: float = 25.0
    ) -> SipCallResult:
        """Send a real INVITE and block for the real final response.

        Real production behavior: FreeSWITCH sends `100 Trying` almost
        immediately (dialplan match + `park`), then nothing further until
        this product's own orchestrator explicitly calls `uuid_answer` --
        which only happens after real routing/authorization/ownership
        database round-trips complete. `final_timeout` must exceed the
        orchestrator's own real answer path latency.
        """
        result = SipCallResult(call_id=self._call_id)
        self._sock.sendto(self._build_invite(to_user, from_user), self._remote_target)

        deadline = time.monotonic() + final_timeout
        while time.monotonic() < deadline:
            try:
                raw, _addr = self._sock.recvfrom(8192)
            except TimeoutError:
                continue
            except OSError:
                continue
            try:
                status, _reason = _parse_status_line(raw)
            except SipMessageError:
                continue
            headers = _parse_headers(raw)
            if headers.get("call-id") != self._call_id:
                continue
            if status < 200:
                result.provisional_statuses.append(status)
                continue
            result.final_status = status
            to_header = headers.get("to", "")
            if "tag=" in to_header:
                result.remote_tag = to_header.split("tag=", 1)[1].split(";")[0].strip()
            if status == 200:
                ip, port = _parse_sdp_media(raw)
                result.remote_rtp_ip = ip
                result.remote_rtp_port = port
                self._ack(headers)
            return result
        return result

    def _ack(self, invite_200_headers: dict[str, str]) -> None:
        to_header = invite_200_headers.get("to", "")
        ack = "\r\n".join(
            [
                f"ACK sip:{self.remote_host}:{self.remote_port} {_SIP_VERSION}",
                (
                    f"Via: SIP/2.0/UDP {self.advertise_ip}:{self.local_sip_port}"
                    f";branch={self._branch};rport"
                ),
                "Max-Forwards: 70",
                f"From: <sip:phase225caller@{self.advertise_ip}>;tag={self._from_tag}",
                f"To: {to_header}",
                f"Call-ID: {self._call_id}",
                f"CSeq: {self._cseq} ACK",
                "Content-Length: 0",
                "",
                "",
            ]
        )
        self._sock.sendto(ack.encode("utf-8"), self._remote_target)

    def start_rtp(self, remote_ip: str, remote_port: int) -> RTPClient:
        self.rtp = RTPClient(
            {0: PayloadType.PCMU},
            self.local_ip,
            self.local_rtp_port,
            remote_ip,
            remote_port,
            TransmitType.SENDRECV,
        )
        self.rtp.start()
        return self.rtp

    def bye(self, *, remote_tag: str | None) -> int | None:
        """Caller-initiated hangup. Returns the peer's response status, if any."""
        self._cseq += 1
        to_line = f"<sip:{self.remote_host}>"
        if remote_tag:
            to_line += f";tag={remote_tag}"
        bye = "\r\n".join(
            [
                f"BYE sip:{self.remote_host}:{self.remote_port} {_SIP_VERSION}",
                (
                    f"Via: SIP/2.0/UDP {self.advertise_ip}:{self.local_sip_port}"
                    f";branch=z9hG4bK{_token(16)};rport"
                ),
                "Max-Forwards: 70",
                f"From: <sip:phase225caller@{self.advertise_ip}>;tag={self._from_tag}",
                f"To: {to_line}",
                f"Call-ID: {self._call_id}",
                f"CSeq: {self._cseq} BYE",
                "Content-Length: 0",
                "",
                "",
            ]
        )
        self._sock.sendto(bye.encode("utf-8"), self._remote_target)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                raw, _addr = self._sock.recvfrom(8192)
            except (TimeoutError, OSError):
                continue
            try:
                status, _ = _parse_status_line(raw)
            except SipMessageError:
                continue
            headers = _parse_headers(raw)
            if headers.get("call-id") == self._call_id:
                return status
        return None

    def poll_for_remote_bye(self, *, timeout: float) -> bool:
        """Non-INVITE-transaction poll: did the peer (FreeSWITCH) hang up on us?

        Real production behavior for "caller stays connected, agent (or the
        orchestrator's own bounded timeout) ends the call first" -- this is
        the inverse of `bye()`.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw, addr = self._sock.recvfrom(8192)
            except (TimeoutError, OSError):
                continue
            if raw.startswith(b"BYE "):
                headers = _parse_headers(raw)
                if headers.get("call-id") != self._call_id:
                    continue
                cseq = headers.get("cseq", "0 BYE")
                ok = "\r\n".join(
                    [
                        f"{_SIP_VERSION} 200 OK",
                        f"Via: {headers.get('via', '')}",
                        f"From: {headers.get('from', '')}",
                        f"To: {headers.get('to', '')}",
                        f"Call-ID: {self._call_id}",
                        f"CSeq: {cseq}",
                        "Content-Length: 0",
                        "",
                        "",
                    ]
                )
                self._sock.sendto(ok.encode("utf-8"), addr)
                return True
        return False
