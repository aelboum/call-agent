"""Object-storage boundary for call artifacts (Phase 0 report section 2.4 G-2).

SaaS-OS provides no product object storage: `boto3` is present there only for
platform backup tooling, which an import-linter contract marks as not an
application-runtime capability. Recording and transcript-export storage is
therefore the product's own component.

Phase 1 defines the contract and nothing else. There is no implementation, no
client, and **no `boto3` import anywhere in the product** -- when an adapter
is written (Phase 2+) it lives in its own module behind this protocol, and the
SDK import stays there.

Requirements this contract exists to make satisfiable, recorded now so the
first implementation cannot quietly skip them (Phase 0 report section 16.1):

* Audio never lands in PostgreSQL -- the database holds a key, not bytes.
* Keys are prefixed per tenant, so a mis-scoped listing cannot cross tenants.
* Access is exclusively via short-lived, expiring URLs. No object is public
  and no permanent URL is stored anywhere.
* Deletion is real and idempotent, because retention jobs and tenant purge
  both depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["ObjectMetadata", "ObjectStore", "ObjectStoreError", "ObjectNotFoundError"]


class ObjectStoreError(Exception):
    """Base class for object-store failures. No vendor exception escapes an
    adapter."""


class ObjectNotFoundError(ObjectStoreError):
    """The key does not exist (or has already been deleted)."""


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    key: str
    size_bytes: int
    content_type: str


@runtime_checkable
class ObjectStore(Protocol):
    """Storage for call artifacts, addressed by opaque key."""

    async def put(self, key: str, data: bytes, *, content_type: str) -> ObjectMetadata: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None:
        """Idempotent: deleting an absent key is not an error."""
        ...

    async def signed_url(self, key: str, *, expires_in_seconds: int) -> str:
        """A short-lived read URL. Implementations must refuse an unbounded
        or long expiry rather than silently honouring it."""
        ...
