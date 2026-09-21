#!/usr/bin/env python
"""Operator entrypoint for `voiceagent.rbac_bootstrap` (Phase 2.2 brief
section 25: "prefer a dedicated explicit bootstrap/setup command or script").

Run once per tenant, by a human operator with the platform authority the
docstrings on `voiceagent.rbac_bootstrap.bootstrap_tenant_rbac()` describe.
Safe to run again for the same tenant: every step it performs is idempotent.

Usage::

    python scripts/bootstrap_rbac.py \\
        --tenant-id <uuid> \\
        --actor-user-id <uuid> \\
        [--service-account-id <uuid>] \\
        [--role-name voiceagent-runtime]

Requires the same environment a normal `voiceagent` process needs
(`DATABASE_URL`, `MIGRATIONS_DATABASE_URL` is not needed here, `REDIS_URL`
for `api.dependencies`'s own import-time job registration) -- this script
does not read a secret or open any connection beyond the ordinary SaaS-OS
database primitives `voiceagent.rbac_bootstrap` itself uses.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from voiceagent.rbac_bootstrap import DEFAULT_ROLE_NAME, bootstrap_tenant_rbac


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, type=uuid.UUID)
    parser.add_argument(
        "--actor-user-id",
        required=True,
        type=uuid.UUID,
        help="A core.users id already holding ordinary authority over every "
        "permission in voiceagent.rbac_bootstrap.PERMISSIONS at --tenant-id.",
    )
    parser.add_argument(
        "--service-account-id",
        type=uuid.UUID,
        default=None,
        help="The call runtime's own core.identity.ServiceAccount id "
        "(Phase 0 report section 14.4). Omit to only create the role and "
        "grant its permissions, without assigning it to anyone yet.",
    )
    parser.add_argument("--role-name", default=DEFAULT_ROLE_NAME)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    result = bootstrap_tenant_rbac(
        tenant_id=args.tenant_id,
        actor_user_id=args.actor_user_id,
        role_name=args.role_name,
        service_account_id=args.service_account_id,
    )
    print(f"role: {result.role_name} ({result.role_id})")
    print(f"tenant: {result.tenant_id}")
    print("granted permissions:")
    for resource, action in result.granted:
        print(f"  {resource}:{action}")
    if result.service_account_id is not None:
        print(f"assigned to service account: {result.service_account_id}")
    else:
        print("no service account assigned (pass --service-account-id to assign one)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
