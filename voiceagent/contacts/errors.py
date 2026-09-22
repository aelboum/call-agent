"""Domain errors for `Contact` (Phase 2.6)."""

from __future__ import annotations

__all__ = [
    "ContactError",
    "ContactInvalidPhoneError",
    "ContactNotFoundError",
    "ContactPhoneConflictError",
]


class ContactError(Exception):
    """Base class for every Contact domain error."""


class ContactNotFoundError(ContactError):
    def __init__(self, contact_id: object) -> None:
        super().__init__(f"Contact not found: {contact_id}")


class ContactInvalidPhoneError(ContactError):
    """`phone_e164` failed E.164 validation -- deliberately carries no echo
    of the rejected value back into the message (the caller already has
    it)."""

    def __init__(self) -> None:
        super().__init__("Invalid E.164 phone number.")


class ContactPhoneConflictError(ContactError):
    """This tenant already has a `Contact` with this `phone_e164`
    (`uq_contacts_tenant_phone`) -- tenant-local, unlike
    `voiceagent.phone_numbers.errors.PhoneNumberUnavailableError`, so there
    is no cross-tenant information-disclosure concern here: a duplicate can
    only ever mean "your own tenant already has this contact"."""

    def __init__(self) -> None:
        super().__init__("Contact with this phone number already exists.")
