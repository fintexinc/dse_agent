"""Giving up on the router does not fail an item — it parks it.

A router that returns nothing falls through to the human repo picker, and in an
unattended run there is no human: the work item sits in
`awaiting_repo_selection` until someone notices. Measured: one 502 lasting
seconds — the same URL answered 200 minutes later, from inside the same pod —
stopped an item dead for the rest of the night.

So a transient gateway failure is retried. A 4xx is not: a wrong key or a wrong
model name is configuration, not weather, and repeating it only burns the clock
before the same human picker.
"""
from __future__ import annotations

import httpx
import pytest

from dse_orchestrator import local_activities as la


class _Resp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.request = httpx.Request("POST", "http://gw/chat/completions")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=self.request, response=self
            )


def _ok(repos: list[str]) -> _Resp:
    import json as _json

    body = _json.dumps({"repos": repos, "reason": "porque sim"})
    return _Resp(200, {"choices": [{"message": {"content": body}}]})


class _Cur:
    """Two candidate repositories, so the router actually asks the model.

    Answers per statement: the catalogue query and the bindings query return
    different shapes, and a fake that returns the catalogue to both makes the
    branch lookup unpack four columns into two.
    """

    def __init__(self):
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *_a, **_k):
        self._sql = " ".join(str(sql).split())
        return None

    def fetchall(self):
        # Matched on the projection, not on the table: the catalogue query is a
        # UNION that also reads `repo_bindings`.
        if self._sql.startswith("SELECT repo, base_branch FROM repo_bindings"):
            return [("org/fe", "develop"), ("org/be", "main")]
        return [("org/fe", "frontend", "typescript", "ui"),
                ("org/be", "backend", "java", "api")]


class _Conn:
    """The router uses `with conn, conn.cursor()`, so both are context managers."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur()

    def close(self):
        return None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DSE_LITELLM_MASTER_KEY", "sk-test")
    monkeypatch.setenv("DSE_MODEL_GATEWAY_URL", "http://gw")
    monkeypatch.setattr(la, "_ROUTER_BACKOFF_SECONDS", 0.0)  # no real sleeping
    monkeypatch.setattr(la, "_get_connection", lambda *a, **k: _Conn())


def _route(monkeypatch, responses):
    """Drive the real `_route_repos_sync` with a scripted gateway.

    `httpx` is imported INSIDE the function, so the patch has to land on the
    httpx module itself rather than on an attribute of local_activities."""
    calls = {"n": 0}

    def fake_post(*_a, **_k):
        r = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(httpx, "post", fake_post)
    out = la._route_repos_sync(tenant_id="t", instruction="mude a cor do badge")
    return out, calls["n"]


def test_a_transient_502_is_retried_and_the_route_still_lands(monkeypatch):
    """The incident, exactly: 502, then a healthy gateway."""
    out, n = _route(monkeypatch, [_Resp(502), _ok(["org/fe"])])
    assert out["repos"] == ["org/fe"], out
    assert n == 2, f"the router asked {n} time(s) — it did not retry"


def test_a_dropped_connection_is_retried(monkeypatch):
    out, n = _route(monkeypatch, [httpx.ConnectError("connection refused"), _ok(["org/be"])])
    assert out["repos"] == ["org/be"]
    assert n == 2


def test_a_4xx_is_not_retried(monkeypatch):
    """A wrong key or a wrong model name will be wrong on every attempt. Three
    of them just delays the human picker the item is heading for anyway."""
    out, n = _route(monkeypatch, [_Resp(401)])
    assert out["repos"] == []
    assert n == 1, f"a 401 was asked {n} times"


def test_it_still_gives_up_and_asks_a_human_when_the_gateway_stays_down(monkeypatch):
    """Retrying is not the same as blocking. When the outage is real, the item
    must still reach the human picker rather than hang on the activity."""
    out, n = _route(monkeypatch, [_Resp(503)])
    assert out["repos"] == []
    assert "unavailable" in out["reason"]
    assert n == la._ROUTER_ATTEMPTS, f"asked {n}, expected {la._ROUTER_ATTEMPTS}"
