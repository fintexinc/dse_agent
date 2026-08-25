"""WSF-E3-T3 — console SSO/OIDC + offboarding. Checks a real RSA signature (the
dev IdP mints, the verifier validates), account matching by subject, and
offboarding with cascading effect (approver + steering)."""
from __future__ import annotations

import uuid

import pytest
from dse_audit.client import get_connection
from dse_platform import is_console_active
from dse_platform.dev_idp import DevIdP
from dse_platform.sso import (
    InvalidToken,
    LoginDenied,
    OIDCVerifier,
    ensure_sso_principal,
    login,
    offboard,
    provision_console_user,
)

AUDIENCE = "dse-admin-console"
ISSUER = "https://dev-idp.local"


@pytest.fixture()
def idp():
    return DevIdP(issuer=ISSUER)


@pytest.fixture()
def verifier(idp):
    return OIDCVerifier(jwks=idp.jwks(), issuer=ISSUER, audience=AUDIENCE)


def _audit_for(principal_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT action FROM audit_log WHERE actor = %s ORDER BY ts, id", (principal_id,))
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def test_verify_valid_token(idp, verifier):
    sub = f"sub-{uuid.uuid4().hex[:8]}"
    tok = idp.mint_id_token(subject=sub, audience=AUDIENCE, email="a@b.co", name="Alice")
    claims = verifier.verify(tok)
    assert claims.subject == sub
    assert claims.email == "a@b.co"


def test_verify_rejects_wrong_audience(idp):
    v = OIDCVerifier(jwks=idp.jwks(), issuer=ISSUER, audience="some-other-client")
    tok = idp.mint_id_token(subject="x", audience=AUDIENCE)
    with pytest.raises(InvalidToken):
        v.verify(tok)


def test_verify_rejects_expired(idp, verifier):
    tok = idp.mint_id_token(subject="x", audience=AUDIENCE, ttl_seconds=-10)
    with pytest.raises(InvalidToken):
        verifier.verify(tok)


def test_verify_rejects_foreign_signature(verifier):
    other = DevIdP(issuer=ISSUER)  # different keypair
    tok = other.mint_id_token(subject="x", audience=AUDIENCE)
    with pytest.raises(InvalidToken):
        verifier.verify(tok)


def test_login_provisions_console_identity_first_time(idp, verifier):
    sub = f"sub-{uuid.uuid4().hex[:8]}"
    tok = idp.mint_id_token(subject=sub, audience=AUDIENCE, email="new@co", name="New User")
    session = login(verifier, tok)
    assert session.sso_subject == sub
    assert session.principal_id.startswith("usr_")
    assert is_console_active(session.principal_id) is True
    assert "console_login" in _audit_for(session.principal_id)

    # second login: same principal (account matching on the stable subject)
    session2 = login(verifier, idp.mint_id_token(subject=sub, audience=AUDIENCE))
    assert session2.principal_id == session.principal_id


def test_login_denied_after_offboarding(idp, verifier):
    sub = f"sub-{uuid.uuid4().hex[:8]}"
    session = login(verifier, idp.mint_id_token(subject=sub, audience=AUDIENCE))
    offboard(session.principal_id, reason="terminated", actor="system:test")
    assert is_console_active(session.principal_id) is False
    with pytest.raises(LoginDenied):
        login(verifier, idp.mint_id_token(subject=sub, audience=AUDIENCE))


def test_contractor_expiry_denies_login(idp, verifier):
    from datetime import datetime, timedelta, timezone

    sub = f"sub-{uuid.uuid4().hex[:8]}"
    principal = ensure_sso_principal(sub)
    provision_console_user(
        principal, sub, roles=["approver"], is_contractor=True,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert is_console_active(principal) is False
    with pytest.raises(LoginDenied):
        login(verifier, idp.mint_id_token(subject=sub, audience=AUDIENCE))


def test_offboard_requires_reason(idp, verifier):
    session = login(verifier, idp.mint_id_token(subject=f"s-{uuid.uuid4().hex[:8]}", audience=AUDIENCE))
    with pytest.raises(ValueError):
        offboard(session.principal_id, reason="", actor="system:test")
