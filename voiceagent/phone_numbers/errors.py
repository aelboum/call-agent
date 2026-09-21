"""Domain errors for `PhoneNumber`.

`PhoneNumberUnavailableError` is the one error class this module exists to
get right: it is raised for **both** "this E.164 is already claimed by your
own tenant" and "this E.164 is already claimed by a different tenant" --
identically, with no distinguishing detail -- because Phase 0 report §14.1
and Phase 2.0 report §15.1 both identify the alternative (a message or status
code that differs by case) as an information-disclosure vector: `e164` is
globally unique and not filtered by Row-Level Security, so a distinguishing
response would let a caller probe which numbers exist and who owns them.
"""

from __future__ import annotations

__all__ = ["PhoneNumberError", "PhoneNumberNotFoundError", "PhoneNumberUnavailableError"]


class PhoneNumberError(Exception):
    """Base class for every PhoneNumber domain error."""


class PhoneNumberNotFoundError(PhoneNumberError):
    def __init__(self, phone_number_id: object) -> None:
        super().__init__(f"PhoneNumber not found: {phone_number_id}")


class PhoneNumberUnavailableError(PhoneNumberError):
    """Deliberately carries no detail about *why* -- see module docstring.
    The message below is the only one ever shown; it is safe by
    construction because it is invariant, not because it was reviewed for
    leakage this one time."""

    def __init__(self) -> None:
        super().__init__("Phone number unavailable.")
