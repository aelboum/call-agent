"""The tenant RLS integration path (Phase 1 brief section 4).

Phase 1 creates no table, so there is no policy to observe on a live
database. What can -- and must -- be pinned now is the *path* every future
product table will take: SaaS-OS's `tenant_rls_statements()` helper, reused
rather than hand-rolled, emitting a policy keyed on the same session setting
`tenant_session_scope()` sets.

These assertions are deliberately about shape, not about text: they fail if a
future re-pin of SaaS-OS ever weakens `ENABLE`/`FORCE`, changes the setting
name, or changes the tenant column convention -- each of which would silently
break tenant isolation for every product table created afterwards.
"""

from __future__ import annotations

from infra.db import tenant_rls_statements

from voiceagent.db import PRODUCT_SCHEMA


def _statements() -> list[str]:
    return tenant_rls_statements("call_sessions", schema=PRODUCT_SCHEMA)


def test_row_level_security_is_enabled_and_forced() -> None:
    """FORCE matters as much as ENABLE: without it the table owner is exempt
    from the policy, which is exactly the role a careless deployment runs the
    application as."""
    joined = " ".join(_statements()).upper()
    assert "ENABLE ROW LEVEL SECURITY" in joined
    assert "FORCE ROW LEVEL SECURITY" in joined


def test_policy_is_keyed_on_the_session_tenant_setting() -> None:
    """The policy must read `app.tenant_id` -- the setting
    `infra.db.tenant_session_scope()` binds per transaction. Any other key
    would mean RLS and the session scope disagree about who the caller is."""
    policy = [statement for statement in _statements() if "CREATE POLICY" in statement]
    assert len(policy) == 1
    assert "current_setting('app.tenant_id'" in policy[0]
    assert "tenant_id =" in policy[0]


def test_statements_target_the_product_schema() -> None:
    """Product tables live in `app` and nowhere else; no product migration
    ever applies a policy to a SaaS-OS-owned schema."""
    assert all(f'"{PRODUCT_SCHEMA}"."call_sessions"' in statement for statement in _statements())


def test_product_schema_is_the_frozen_name() -> None:
    """ADR-0005: the schema name carries no product or commercial identity,
    precisely so renaming either never implies a schema migration."""
    assert PRODUCT_SCHEMA == "app"
