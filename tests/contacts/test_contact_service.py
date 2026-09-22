"""`voiceagent.contacts.service.normalize_phone_e164()` -- pure, DB-free
(does not open `tenant_scope()`), so it is hermetically unit-tested on its
own, exactly like `voiceagent.calls.lifecycle`."""

from __future__ import annotations

import pytest

from voiceagent.contacts.errors import ContactInvalidPhoneError
from voiceagent.contacts.service import normalize_phone_e164


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+15551234567", "+15551234567"),
        ("+1 555 123 4567", "+15551234567"),
        ("+1-555-123-4567", "+15551234567"),
        ("+1 (555) 123-4567", "+15551234567"),
    ],
)
def test_normalize_strips_common_punctuation(raw: str, expected: str) -> None:
    assert normalize_phone_e164(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-number",
        "15551234567",  # missing leading +
        "+0123456789",  # leading zero after +
        "+1; DROP TABLE contacts;",  # injection-shaped
        "",
        "+",
    ],
)
def test_normalize_rejects_invalid_numbers(raw: str) -> None:
    with pytest.raises(ContactInvalidPhoneError):
        normalize_phone_e164(raw)


def test_normalize_never_guesses_a_missing_country_code() -> None:
    """A number with no `+` must be rejected, never silently reinterpreted
    with an assumed country code."""
    with pytest.raises(ContactInvalidPhoneError):
        normalize_phone_e164("5551234567")
