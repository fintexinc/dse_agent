"""WSF-E3-T3 — admin console SSO/OIDC + console identity (ADR-22).

Two responsibilities:
1. `OIDCVerifier` — validates an `id_token` (RS256) against a JWKS (from the real
   IdP in production; from `dev_idp.DevIdP` in dev/test — see README). Checks the
   signature, `iss`, `aud`, `exp`. Never trusts an unverified token (P8).
2. Wiring to the foundation identity map + offboarding: `login` resolves the
   IdP's `sub` to a `principal_id` (via `dse_identity.resolve_principal` with
   platform "sso"), guarantees a row in `dse_console_identity`, and REFUSES the
   login if the account is offboarded (`active = false`) or expired (contractor
   past `expires_at`). `offboard` deactivates the account — which also removes it
   from approver resolution (see access_bundles). A direção não tem mais
   lista própria desde 2026-08-21 (migração 0046): canal = autorização.

Account matching (ADR-22): by the IdP's stable `sub` (subject), NOT by email
(email can be reassigned). The email is stored only for display/contact.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import uuid
from typing import Any

import jwt
import psycopg2
import psycopg2.extras

from dse_audit import emit

logger = logging.getLogger("dse_platform.sso")

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def _get_connection():
    return psycopg2.connect(_DSN)


def ensure_sso_principal(sso_subject: str, *, display_name: str | None = None, conn=None) -> str:
    """ADR-22 account matching for SSO users: resolves (or mints) the
    `principal_id` for an `sso_subject`.

    FOUNDATION NOTE (documented in the README + ADR-22): the foundation's
    `identity_links` has a CHECK that only allows `platform IN
    ('slack','github','jira')` — we cannot write `platform = 'sso'` there (and we
    do not edit the foundation migration). So an SSO user's principal is created
    DIRECTLY in `principals`, and the account-matching key (`sso_subject`) lives
    in `dse_console_identity`. Consumers still see a normal `usr_<uuid>` in
    `principals` — the public signature (`principal_id`) does not change.

    Idempotent by `sso_subject` (the console_identity row is the source of truth
    for matching)."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT principal_id FROM dse_console_identity WHERE sso_subject = %s",
                (sso_subject,),
            )
            row = cur.fetchone()
            if row is not None:
                return row[0]
            principal_id = f"usr_{uuid.uuid4().hex[:16]}"
            cur.execute(
                "INSERT INTO principals (id, display_name) VALUES (%s, %s)",
                (principal_id, display_name),
            )
        if owns:
            conn.commit()
        return principal_id
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


class InvalidToken(Exception):
    """id_token failed verification (signature/iss/aud/exp)."""


class LoginDenied(Exception):
    """Token is valid but the console account is offboarded/expired."""


@dataclasses.dataclass(frozen=True)
class OIDCClaims:
    subject: str
    audience: str
    issuer: str
    email: str | None
    name: str | None
    raw: dict[str, Any]


class OIDCVerifier:
    """OIDC id_token verifier. Build it with the IdP's JWKS (a dict shaped like
    `{"keys": [...]}`) and the expected `issuer`/`audience` (client_id). In
    production, `jwks` comes from the IdP's `jwks_uri` (fetched and cached by the
    caller); the verification contract is identical to the dev IdP's."""

    def __init__(self, *, jwks: dict, issuer: str, audience: str, jwks_uri: str | None = None):
        self._issuer = issuer
        self._audience = audience
        self._jwks_uri = jwks_uri
        self._keys = {}
        self._load(jwks)

    def _load(self, jwks: dict) -> None:
        self._keys = {
            jwk.get("kid"): jwt.PyJWK.from_dict(jwk) for jwk in jwks.get("keys", [])
        }

    def _refresh(self) -> bool:
        """Re-fetch the JWKS. True when a new set of keys was loaded.

        Identity providers rotate their signing keys on their own schedule —
        Entra does it routinely — and a verifier built once at startup keeps a
        snapshot that eventually contains none of the keys in use. The failure
        is total and undramatic: every login starts returning "invalid token"
        while the IdP, the network and this service all look healthy, and the
        only cure is a restart nobody knows to perform.

        Best-effort: if the fetch fails, the caller still rejects the token, so
        the worst case is the behaviour we had before instead of an exception
        from an unrelated layer.
        """
        if not self._jwks_uri:
            return False
        try:
            import json
            import urllib.request

            with urllib.request.urlopen(self._jwks_uri, timeout=10) as resp:
                self._load(json.loads(resp.read().decode()))
            return True
        except Exception:  # noqa: BLE001 — an unreachable IdP must not raise from here
            logger.warning("could not refresh the JWKS from %s", self._jwks_uri, exc_info=True)
            return False

    def verify(self, id_token: str) -> OIDCClaims:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.InvalidTokenError as e:
            raise InvalidToken(f"invalid header: {e}") from e
        kid = header.get("kid")
        signing = self._keys.get(kid)
        if signing is None:
            # An unknown kid is the signal that the IdP rotated. Re-fetch once
            # and retry rather than rejecting: this is the difference between a
            # console that keeps working through a rotation and one that stops
            # authenticating everybody until someone restarts it.
            if self._refresh():
                signing = self._keys.get(kid)
        if signing is None:
            raise InvalidToken(f"kid {kid!r} unknown in the JWKS")
        try:
            claims = jwt.decode(
                id_token,
                signing.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise InvalidToken(f"token expired: {e}") from e
        except jwt.InvalidTokenError as e:
            raise InvalidToken(f"invalid token: {e}") from e
        return OIDCClaims(
            subject=claims["sub"],
            audience=self._audience,
            issuer=self._issuer,
            email=claims.get("email"),
            name=claims.get("name"),
            raw=claims,
        )


@dataclasses.dataclass(frozen=True)
class ConsoleSession:
    principal_id: str
    sso_subject: str
    email: str | None
    display_name: str | None
    tenant_id: str | None
    roles: list[str]


def login(verifier: OIDCVerifier, id_token: str, *, conn=None) -> ConsoleSession:
    """Verifies the token, resolves/guarantees the console identity and returns
    the session. Raises `LoginDenied` if offboarded/expired (P6: clean failure).
    Writes a `console_login` (or `console_login_denied`) audit entry."""
    claims = verifier.verify(id_token)

    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM dse_console_identity WHERE sso_subject = %s",
                (claims.subject,),
            )
            row = cur.fetchone()

        if row is not None and (
            not row["active"]
            or (row["expires_at"] is not None and _expired(row["expires_at"]))
        ):
            emit(
                actor=row["principal_id"], action="console_login_denied",
                tenant_id=row["tenant_id"] or "platform",
                details={"reason": "offboarded_or_expired", "sso_subject": claims.subject},
                conn=conn,
            )
            if owns:
                conn.commit()
            raise LoginDenied(f"console account offboarded or expired (sub={claims.subject})")

        if row is None:
            # first sighting: mint the principal (account matching by subject)
            principal_id = ensure_sso_principal(claims.subject, display_name=claims.name, conn=conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO dse_console_identity (principal_id, sso_subject, email, display_name)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (sso_subject) DO NOTHING
                    """,
                    (principal_id, claims.subject, claims.email, claims.name),
                )
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM dse_console_identity WHERE sso_subject = %s", (claims.subject,))
                row = cur.fetchone()

        session = ConsoleSession(
            principal_id=row["principal_id"],
            sso_subject=row["sso_subject"],
            email=row["email"],
            display_name=row["display_name"],
            tenant_id=row["tenant_id"],
            roles=list(row["roles"] or []),
        )
        emit(
            actor=session.principal_id, action="console_login",
            tenant_id=session.tenant_id or "platform",
            details={"sso_subject": claims.subject, "roles": session.roles},
            conn=conn,
        )
        if owns:
            conn.commit()
        return session
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


def _expired(ts) -> bool:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts < now


def provision_console_user(
    principal_id: str,
    sso_subject: str,
    *,
    email: str | None = None,
    display_name: str | None = None,
    tenant_id: str | None = None,
    roles: list[str] | None = None,
    is_contractor: bool = False,
    expires_at=None,
    actor: str = "system:platform-admin",
    conn=None,
) -> None:
    """Provisions/updates a console identity explicitly (e.g. an admin grants the
    `approver`/`operator` role before the first login). `principal_id` must exist
    in `principals` (resolved via dse_identity)."""
    if roles is not None:
        for r in roles:
            if r not in {"operator", "approver", "viewer", "admin"}:
                raise ValueError(f"unknown role: {r!r}")
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dse_console_identity
                    (principal_id, sso_subject, email, display_name, tenant_id, roles, is_contractor, expires_at)
                VALUES (%s, %s, %s, %s, %s, COALESCE(%s::jsonb, '[]'::jsonb), %s, %s)
                ON CONFLICT (sso_subject) DO UPDATE SET
                    email = COALESCE(EXCLUDED.email, dse_console_identity.email),
                    display_name = COALESCE(EXCLUDED.display_name, dse_console_identity.display_name),
                    tenant_id = COALESCE(EXCLUDED.tenant_id, dse_console_identity.tenant_id),
                    roles = EXCLUDED.roles,
                    is_contractor = EXCLUDED.is_contractor,
                    expires_at = EXCLUDED.expires_at
                """,
                (
                    principal_id, sso_subject, email, display_name, tenant_id,
                    None if roles is None else psycopg2.extras.Json(roles),
                    is_contractor, expires_at,
                ),
            )
        emit(
            actor=actor, action="console_user_provisioned",
            tenant_id=tenant_id or "platform",
            details={"principal_id": principal_id, "roles": roles, "is_contractor": is_contractor},
            conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


def offboard(principal_id: str, *, reason: str, actor: str, conn=None) -> None:
    """Offboarding (ADR-22): deactivates the console identity. Cascading effect:
    the principal is dropped from approver resolution (access_bundles.resolve_plan_approvers
    filters `active = false`). A allowlist de direção saiu em 2026-08-21
    (migração 0046): direção é autorizada pelo acesso ao canal, não por lista.
    Idempotent. Writes an audit entry (P8). `reason` is required."""
    if not reason:
        raise ValueError("offboard requires `reason` (P8: never silent)")
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dse_console_identity
                SET active = false, deactivated_at = now()
                WHERE principal_id = %s AND active = true
                """,
                (principal_id,),
            )
            changed = cur.rowcount
        emit(
            actor=actor, action="console_user_offboarded",
            tenant_id="platform",
            details={"principal_id": principal_id, "reason": reason, "was_active": changed > 0},
            conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


def is_console_active(principal_id: str, conn=None) -> bool:
    """True iff the principal has an active, non-expired console identity.
    A principal with NO row in dse_console_identity returns False here (it is not
    a console user) — but note that `resolve_plan_approvers` treats CODEOWNERS
    without a row as active; these are checks with different purposes."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT active, expires_at FROM dse_console_identity WHERE principal_id = %s
                """,
                (principal_id,),
            )
            row = cur.fetchone()
    finally:
        if owns:
            conn.close()
    if row is None:
        return False
    active, expires_at = row
    if not active:
        return False
    if expires_at is not None and _expired(expires_at):
        return False
    return True
