"""The ESL connection boundary (ADR-0002 amendment point 2; this phase's
brief section 9: "use dependency injection for the ESL/control connection").

`EslConnection` is injected into `FreeSwitchTelephonyProvider` rather than
constructed by it -- the provider never dials out itself, so it never
requires a running FreeSWITCH instance to be tested
(`voiceagent.telephony.freeswitch.fakes.FakeEslConnection` is the double
every hermetic test uses instead). Establishing an actual TCP connection to
`mod_event_socket` (host, port, password, reconnection with backoff) is a
real implementation's own concern and is explicitly out of this phase's
scope (brief section 29: no live FreeSWITCH instance is required by the
default CI suite) -- this module fixes the *shape* of that connection, not
its transport.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Protocol, runtime_checkable

__all__ = ["EslConnection", "EslEvent"]

#: One raw FreeSWITCH event, exactly as ESL delivers it -- a flat,
#: string-keyed map (`Event-Name`, `Unique-ID`, `Caller-Caller-ID-Number`,
#: `variable_...`, ...). Confined to this package: nothing above
#: `voiceagent.telephony.freeswitch` ever sees one, only the normalized
#: `voiceagent.telephony.contracts.CallEvent` `provider.py` produces from it.
EslEvent = Mapping[str, str]


@runtime_checkable
class EslConnection(Protocol):
    """The narrow slice of `mod_event_socket` this product needs: issue one
    command and read its response; consume the raw event stream. Everything
    about *how* the connection is established is this protocol's own
    implementation's concern, injected at construction."""

    async def send(self, command: str) -> str:
        """Issue one ESL command (e.g. `"uuid_answer <uuid>"`) and return its
        raw response body."""
        ...

    def events(self) -> AsyncIterator[EslEvent]:
        """The connection's own raw event stream, exactly as received.
        Normalizing it into `CallEvent` is `FreeSwitchTelephonyProvider
        .events()`'s job, never this protocol's own."""
        ...
