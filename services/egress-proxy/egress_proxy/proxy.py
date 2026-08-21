"""Default-deny HTTP/HTTPS proxy (WSC-E2-T1/T2), implemented in pure asyncio
(stdlib) — no mitmproxy — so it can run inside a `python:3.11-slim` container
with no `pip install` (useful for the containerized network isolation test,
which cannot depend on building a custom image).

Two traffic forms are supported:
  - `CONNECT host:port` (opaque HTTPS tunnel): used for model-gateway access and
    for any HTTPS host on the allowlist. Enforcement = allowlist only (we cannot
    — and do not want to, it is out of scope for this phase — inspect/inject
    inside an opaque TLS tunnel without terminating TLS, which would require a
    trusted CA installed in the sandbox; see README).
  - Plain HTTP proxying (`GET/POST http://host/path HTTP/1.1` as the
    request-line, the classic forward-proxy form): used for the "git remote
    relay" pattern and for plaintext model-gateway calls — here we DO inject
    credentials, because the proxy terminates the connection and builds the
    outbound request itself.

    This is what makes a PRIVATE repo clonable. An allowlist entry marked
    `tls_upgrade` accepts the request on :80 and re-originates it to :443 over
    TLS, so the plaintext hop is sandbox→proxy inside the Pod network and the
    injected token still reaches GitHub encrypted. A credential is injected ONLY
    onto a TLS leg, or onto loopback (which never reaches an interface);
    anything else is refused with 403 and audited as
    `egress_credential_injection_refused`. Downgrading instead of refusing would
    read like "the repo is public" and fail much later, much more confusingly.

Every refusal (host off the allowlist) emits `dse_audit.emit(action=
"egress_denied", ...)` — the import is optional (the proxy also runs in a "bare"
container without `dse_audit` installed, in which case it falls back to logging
on stdout; production must install `dse-audit` in the egress-proxy image).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass, field

from .allowlist import Allowlist
from .credentials import CredentialBroker

logger = logging.getLogger("egress_proxy")

try:
    from dse_audit import emit as _audit_emit
except Exception:  # pragma: no cover - "bare container" mode, dse_audit not installed
    _audit_emit = None


def _emit_audit(*, actor: str, action: str, tenant_id: str, work_item_id: str | None, details: dict) -> None:
    if _audit_emit is not None:
        try:
            _audit_emit(actor=actor, action=action, tenant_id=tenant_id, work_item_id=work_item_id, details=details)
            return
        except Exception:  # noqa: BLE001 - never let an egress denial fail because of the audit write
            logger.warning("failed to write audit_log, falling back to a local log: %s %s", action, details)
    logger.info("AUDIT (local fallback, no Postgres) action=%s details=%s", action, details)


_ABSOLUTE_URI_RE = re.compile(r"^https?://([^/:]+)(:(\d+))?(/.*)?$")
#: `/owner/repo.git/info/refs?...` -> `owner/repo`. The git smart-HTTP path is
#: the authoritative statement of which repository is being fetched.
_GIT_PATH_RE = re.compile(r"^/([^/]+)/([^/]+?)(?:\.git)?/(?:info/refs|git-upload-pack|git-receive-pack)")


def _repo_from_git_path(path: str) -> str | None:
    m = _GIT_PATH_RE.match(path)
    return f"{m.group(1)}/{m.group(2)}" if m else None


@dataclass
class DenialLog:
    host: str
    port: int
    ts: float = field(default_factory=time.time)


class EgressProxy:
    def __init__(
        self,
        allowlist: Allowlist,
        *,
        tenant_id: str,
        work_item_id: str | None = None,
        credential_broker: CredentialBroker | None = None,
        credential_hosts: frozenset[str] | None = None,
    ) -> None:
        #: The ONLY destinations a GitHub credential may be injected towards.
        #: Derived from the allowlist's `repo` category rather than hardcoded, so
        #: a deployment that renames its repo host stays correct — and so that
        #: adding an unrelated :443 host to the allowlist can never turn it into
        #: a credential destination.
        self.credential_hosts = credential_hosts or frozenset(
            e.host.lower() for e in allowlist.entries if e.category == "repo"
        )
        self.allowlist = allowlist
        self.tenant_id = tenant_id
        self.work_item_id = work_item_id
        self.credential_broker = credential_broker or CredentialBroker()
        self.denials: list[DenialLog] = []
        self._server: asyncio.base_events.Server | None = None

    async def start(self, host: str = "0.0.0.0", port: int = 8806) -> asyncio.base_events.Server:
        self._server = await asyncio.start_server(self._handle_client, host, port)
        return self._server

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # -- internals ----------------------------------------------------------

    async def _read_headers(self, reader: asyncio.StreamReader) -> dict[str, str]:
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"", b"\n"):
                break
            if b":" in line:
                k, v = line.decode(errors="replace").split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return headers

    def _is_allowed(self, host: str, port: int) -> bool:
        return self.allowlist.is_allowed(host, port)

    def _deny(self, host: str, port: int) -> bytes:
        self.denials.append(DenialLog(host=host, port=port))
        # No stdout TAMBÉM, e não só no audit_log: `kubectl logs` é onde se
        # olha primeiro quando um build morre por rede, e o audit precisa de
        # uma consulta que ninguém faz na hora. Em 2026-08-21 a recusa de
        # `archive.eclipse.org` estava gravada desde a primeira rodada e três
        # releases se passaram até alguém correlacionar.
        logger.warning("egress DENIED %s:%s (not in the allowlist)", host, port)
        _emit_audit(
            actor="system:egress-proxy",
            action="egress_denied",
            tenant_id=self.tenant_id,
            work_item_id=self.work_item_id,
            details={"host": host, "port": port},
        )
        return b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            try:
                method, target, _version = request_line.decode(errors="replace").strip().split(" ")
            except ValueError:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            headers = await self._read_headers(reader)

            if method.upper() == "CONNECT":
                await self._handle_connect(target, writer, reader)
            else:
                await self._handle_plain_http(method, target, headers, reader, writer)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:  # noqa: BLE001 - the proxy must never crash the process over 1 bad connection
            logger.exception("error handling egress connection")
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _handle_connect(
        self, target: str, writer: asyncio.StreamWriter, reader: asyncio.StreamReader
    ) -> None:
        host, _, port_str = target.partition(":")
        port = int(port_str) if port_str else 443
        if not self._is_allowed(host, port):
            writer.write(self._deny(host, port))
            await writer.drain()
            return
        try:
            remote_reader, remote_writer = await asyncio.open_connection(host, port)
        except OSError:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(pipe(reader, remote_writer), pipe(remote_reader, writer))

    #: A git-upload-pack negotiation is a few KB of "want/have" lines; this is the
    #: outer bound that stops a hostile or runaway client in the sandbox from
    #: making the proxy buffer without limit. It bounds the REQUEST only — the
    #: response (the packfile, which is the big one) is streamed straight through.
    _MAX_RELAY_BODY_BYTES = 32 * 1024 * 1024

    async def _read_body(self, headers: dict[str, str], reader: asyncio.StreamReader) -> bytes:
        """Content-Length or chunked.

        git speaks BOTH: a small negotiation arrives with a Content-Length, and
        anything past libcurl's buffer arrives chunked. Reading only the former —
        which is what this did — meant a chunked POST was forwarded with an empty
        body, and `git-upload-pack` answered a protocol error that read like a
        network fault."""
        if headers.get("transfer-encoding", "").lower() == "chunked":
            chunks: list[bytes] = []
            total = 0
            while True:
                size_line = await reader.readline()
                size = int(size_line.strip().split(b";")[0] or b"0", 16)
                if size == 0:
                    await reader.readline()  # the trailing CRLF after the last chunk
                    break
                total += size
                if total > self._MAX_RELAY_BODY_BYTES:
                    raise ValueError(f"relayed request body exceeds {self._MAX_RELAY_BODY_BYTES} bytes")
                chunks.append(await reader.readexactly(size))
                await reader.readexactly(2)  # CRLF terminating this chunk
            return b"".join(chunks)
        length = int(headers.get("content-length", "0") or 0)
        if length > self._MAX_RELAY_BODY_BYTES:
            raise ValueError(f"relayed request body exceeds {self._MAX_RELAY_BODY_BYTES} bytes")
        return await reader.readexactly(length) if length else b""

    async def _handle_plain_http(
        self,
        method: str,
        target: str,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        m = _ABSOLUTE_URI_RE.match(target)
        if not m:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        host = m.group(1)
        port = int(m.group(3)) if m.group(3) else 80
        path = m.group(4) or "/"

        if not self._is_allowed(host, port):
            writer.write(self._deny(host, port))
            await writer.drain()
            return

        # An entry marked `tls_upgrade` accepts a plain-HTTP request on the way in
        # and is re-originated over TLS on 443 on the way out. That is the whole
        # git smart-HTTP relay: terminating the request is what allows injecting a
        # credential, and terminating requires the inbound leg to be plaintext.
        entry = self.allowlist.entry_for(host, port)
        upgrade = bool(entry and entry.tls_upgrade and port == 80)
        outbound_port = 443 if upgrade else port
        outbound_tls = upgrade or port == 443

        # Loopback is the one cleartext leg a credential may take: the bytes
        # never reach a network interface, and the proxy's own 127.0.0.1 is not
        # reachable from the sandbox — it is a different Pod. Anything else must
        # be TLS.
        outbound_local = host in ("127.0.0.1", "::1", "localhost")

        inject = headers.pop("x-dse-inject-credential", None)
        if inject == "github":
            # DESTINATION, not just transport. Gating on TLS alone meant every
            # allowlisted :443 host — api.anthropic.com, slack.com, the Jira
            # site, login.microsoftonline.com — would receive a real GitHub
            # installation token if the sandbox simply addressed it with the
            # header set. The credential is scoped to one host; so is its use.
            if host.lower() not in self.credential_hosts:
                _emit_audit(
                    actor="system:egress-proxy",
                    action="egress_credential_injection_refused",
                    tenant_id=self.tenant_id,
                    work_item_id=self.work_item_id,
                    details={"host": host, "port": port, "reason": "host_not_a_credential_destination"},
                )
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return
            if not (outbound_tls or outbound_local):
                # The invariant the :443-only allowlist entry was written to
                # protect: a token must never leave on a cleartext hop. Refusing
                # is not a degradation — a silent anonymous retry would look like
                # "the repo is public" and fail much later, much more confusingly.
                _emit_audit(
                    actor="system:egress-proxy",
                    action="egress_credential_injection_refused",
                    tenant_id=self.tenant_id,
                    work_item_id=self.work_item_id,
                    details={"host": host, "port": port, "reason": "outbound_leg_not_tls"},
                )
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return
            # The repo comes from the REQUEST PATH, not from the sandbox's
            # `X-Dse-Repo` header. Trusting the header let the sandbox mint a
            # token for one repository while fetching another — a token
            # decoupled from the thing it is being used for. Deriving it from
            # the path makes the scope exactly the resource requested. (It does
            # not stop a sandbox from cloning a different repo in the same
            # installation; only a per-work-item proxy identity would, and this
            # proxy is shared. Recorded as a residual in the PR.)
            path_repo = _repo_from_git_path(path) or headers.get("x-dse-repo", "unknown/unknown")
            cred = await asyncio.to_thread(
                self.credential_broker.mint,
                work_item_id=self.work_item_id or "unknown",
                repo=path_repo,
                branch=headers.get("x-dse-branch", "unknown"),
            )
            usable = bool(cred and cred.token) and not (
                str(cred.token).startswith("fixture-") and not outbound_local
            )
            if not usable:
                # A fixture token is WORSE than none against a REAL host:
                # GitHub answers 401 to a bogus credential where it would have
                # answered 200 to an anonymous read, so the fallback would break
                # the public-repo clone that works today, and say nothing about
                # why. The broker degrades silently by design; the relay must
                # not. Loopback is exempt — the dev/test doubles that prove the
                # placeholder swap have no real identity to authenticate to, and
                # a fixture there is the point rather than a hazard.
                _emit_audit(
                    actor="system:egress-proxy",
                    action="egress_credential_injection_refused",
                    tenant_id=self.tenant_id,
                    work_item_id=self.work_item_id,
                    details={"host": host, "reason": "no_real_installation_token"},
                )
                # RELAY ANONYMOUSLY rather than refusing. Answering 403 here
                # broke every PUBLIC repo in any environment without GitHub App
                # credentials — repos that clone fine without this branch. An
                # anonymous fetch succeeds for a public repo and gets GitHub's
                # own 401 for a private one, which is a truthful message; a 403
                # from us is not. The audit event above records that the request
                # went out uncredentialed.
                cred = None
            if cred:
                # BASIC, not `token <t>`. `Authorization: token …` is the REST
                # API's scheme; github.com's git smart-HTTP endpoint answers 401
                # to it and 200 to Basic with the token as the password.
                # Verified against a real private repo before this was written.
                basic = base64.b64encode(f"x-access-token:{cred.token}".encode()).decode()
                headers["authorization"] = f"Basic {basic}"
                headers["x-dse-credential-id"] = cred.credential_id

        try:
            body = await self._read_body(headers, reader)
        except ValueError:
            # Was a bare socket close: the client saw a connection reset with no
            # status, which is indistinguishable from a network fault.
            writer.write(
                b"HTTP/1.1 413 Payload Too Large\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            return

        try:
            remote_reader, remote_writer = await asyncio.open_connection(
                host, outbound_port, ssl=outbound_tls, server_hostname=host if outbound_tls else None
            )
        except OSError:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        headers["host"] = host
        headers["connection"] = "close"
        # Internal routing headers are ours, not GitHub's. Only the placeholder
        # was being stripped, so x-dse-repo / x-dse-branch / x-dse-credential-id
        # were shipped to the upstream along with the token.
        for internal in ("x-dse-repo", "x-dse-branch", "x-dse-credential-id"):
            headers.pop(internal, None)
        if headers.get("transfer-encoding", "").lower() == "chunked" or body:
            # The body was de-chunked while reading, so the framing the upstream
            # is told must match what is actually sent. Leaving a stale
            # `transfer-encoding: chunked` here makes GitHub wait forever for a
            # terminator that will never arrive.
            headers.pop("transfer-encoding", None)
            headers["content-length"] = str(len(body))
        request_lines = [f"{method} {path} HTTP/1.1"]
        request_lines += [f"{k}: {v}" for k, v in headers.items()]
        remote_writer.write(("\r\n".join(request_lines) + "\r\n\r\n").encode())
        if body:
            remote_writer.write(body)
        await remote_writer.drain()

        while True:
            data = await remote_reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        remote_writer.close()
