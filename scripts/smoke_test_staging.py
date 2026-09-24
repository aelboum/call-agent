#!/usr/bin/env python
"""Read-only staging smoke test (Phase 2.19).

Run this against a live staging deployment's public API origin after
`docker-compose.staging.yml` (or an equivalent real deployment) is up and
migrations have completed. It is a standalone operator tool, not part of
the pytest suite: unlike everything under `tests/`, it needs a real,
reachable staging deployment and makes real HTTP requests.

**What this proves**: the API process is up, `/healthz`/`/readyz` report
healthy, the OIDC login route resolves and redirects to the configured
identity provider (proving `ZITADEL_ISSUER_URL`/`ZITADEL_CLIENT_ID`/
`OIDC_REDIRECT_URI` are wired end-to-end, without performing a real login),
and an unauthenticated request to a tenant-scoped route is correctly
rejected rather than silently allowed (no tenant-isolation bypass).

**What this deliberately does NOT prove** (Phase 2.19 brief section 13:
"do not create a fake end-to-end telephony success test that claims to
prove a real provider works"): it never completes a real OIDC login (that
requires a human at the identity provider's own login page), never places
a real telephone call, and never calls a real AI provider. Those steps stay
manual -- see docs/PHASE-2.19-STAGING-INTEGRATION.md's own smoke-test
section for exactly what a human operator still has to do and verify by
hand.

Usage::

    python scripts/smoke_test_staging.py --base-url https://staging.example.com

Exit code 0 means every automated check passed; non-zero names the first
failure. Never prints a response body or header that could carry a secret
(no smoke-test request in this script carries or receives one).
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request

__all__ = ["main"]


class SmokeTestFailure(RuntimeError):
    pass


def _get(url: str, *, allow_redirects: bool) -> tuple[int, str | None]:
    """Returns `(status_code, location_header)`. Never raises for a plain
    HTTP error status -- only for a transport failure (DNS, connection
    refused, timeout), which is itself a meaningful smoke-test result."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None

    opener = (
        urllib.request.build_opener()
        if allow_redirects
        else urllib.request.build_opener(_NoRedirect)
    )
    # noqa: S310 -- operator-supplied staging URL passed on the command line, not user input.
    request = urllib.request.Request(url, method="GET")  # noqa: S310
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.headers.get("Location")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location")


def _check(label: str, condition: bool, detail: str) -> None:
    if not condition:
        raise SmokeTestFailure(f"{label}: {detail}")
    print(f"OK   {label}")


def run(base_url: str) -> None:
    base_url = base_url.rstrip("/")

    status, _ = _get(f"{base_url}/healthz", allow_redirects=False)
    _check("healthz", status == 200, f"expected 200, got {status}")

    status, _ = _get(f"{base_url}/readyz", allow_redirects=False)
    _check("readyz", status == 200, f"expected 200, got {status}")

    status, location = _get(f"{base_url}/auth/login", allow_redirects=False)
    _check(
        "oidc_login_redirects",
        status in (302, 303, 307, 308) and bool(location),
        f"expected a redirect with a Location header, got status={status} location={location!r}",
    )

    status, _ = _get(f"{base_url}/auth/me", allow_redirects=False)
    _check(
        "unauthenticated_me_is_rejected",
        status in (401, 403),
        f"expected 401/403 with no session cookie, got {status} -- possible auth bypass",
    )

    status, _ = _get(f"{base_url}/v1/agents", allow_redirects=False)
    _check(
        "unauthenticated_api_request_is_rejected",
        status in (401, 403),
        f"expected 401/403 with no session cookie, got {status} "
        "-- possible tenant-isolation bypass",
    )

    print(
        "\nAutomated checks passed. This does NOT prove a real OIDC login, a real "
        "telephone call, or a real AI provider call -- perform those manually "
        "per docs/PHASE-2.19-STAGING-INTEGRATION.md's smoke-test section."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        required=True,
        help="The staging deployment's public API origin, e.g. https://staging.example.com",
    )
    args = parser.parse_args(argv)

    try:
        run(args.base_url)
    except SmokeTestFailure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"FAIL transport error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
