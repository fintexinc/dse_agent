"""WSE-E2-T4 — L2 Reviewer session interface (fresh context).

**P3 (no producer approves its own work / reviewer sees no producer history):**
the L2 session input is STRUCTURALLY just `plan` (PlanArtifact) + `diff` (the
final diff, as text). There is no field for the Coder's history/transcript — it
cannot be leaked across this boundary. WS-C's L2 session receives exactly this
object.

The real session implementation belongs to WS-C (WSC-E3-T5), exposed as the
`ACTIVITY_RUN_L2_REVIEW` Activity. Since the workstreams build in parallel, this
module provides:

  - `L2ReviewSession` (Protocol) — `review(inp) -> L2Verdict`.
  - `FakeL2ReviewSession` — deterministic fake (no LLM), scriptable, used in this
    workstream's tests to exercise the real orchestration without WS-C's session.
    Explicitly marked as a fixture (not production).
  - `build_l2_session()` — resolves WS-C's real session if it is already published
    and importable; otherwise returns the fake with a clear WARNING.
"""
from __future__ import annotations

import logging
import os
from collections import deque
from typing import Protocol

from dse_validation.model_json import ModelJsonError, complete_json
from dse_contracts import L2Verdict, PlanArtifact
from pydantic import BaseModel

logger = logging.getLogger("dse_validation.l2")


class L2ReviewInput(BaseModel):
    """Everything — and ONLY — what the L2 session may see (P3). No Coder history."""

    work_item_id: str
    tenant_id: str
    plan: PlanArtifact
    diff: str
    iteration: int = 0  # L2 turn number within the fix-retry loop (informational)


class L2ReviewSession(Protocol):
    def review(self, inp: L2ReviewInput) -> L2Verdict: ...


class FakeL2ReviewSession:
    """Deterministic fake for WS-E's orchestration tests.

    Modes:
      - `scripted`: a queue of `L2Verdict` (or of `(passed, objections)`),
        consumed in order on each `review()` — lets us simulate
        "reject, reject, approve" for the fix-retry loop.
      - default: always approves with a fixed cost.

    NOT production: no real intelligence, it just plays back the script. The real
    session (WS-C) makes the actual fresh-context model call.
    """

    def __init__(
        self,
        scripted: list[L2Verdict] | list[tuple[bool, list[str]]] | None = None,
        *,
        cost_usd: float = 0.02,
    ):
        self._cost = cost_usd
        self._queue: deque = deque(scripted or [])
        self.calls: list[L2ReviewInput] = []

    def review(self, inp: L2ReviewInput) -> L2Verdict:
        # Records the received input so the tests can prove P3 was honored
        # (the object has no Coder-history field — it is structural).
        self.calls.append(inp)
        if self._queue:
            item = self._queue.popleft()
            if isinstance(item, L2Verdict):
                return item.model_copy(update={"work_item_id": inp.work_item_id})
            passed, objections = item
            return L2Verdict(
                work_item_id=inp.work_item_id,
                passed=passed,
                objections=list(objections),
                cost_usd=self._cost,
            )
        return L2Verdict(work_item_id=inp.work_item_id, passed=True, objections=[], cost_usd=self._cost)


_L2_PROMPT = """You are the Fintex DSE Reviewer L2 — a FRESH-CONTEXT reviewer (P3):
you see ONLY the plan and the final diff. Judge whether the change implements the
plan with the minimum safety and quality needed to open a PR (the final review is
human).

Answer ONLY with valid JSON:
{{"passed": true|false, "objections": ["specific objection (file/line) 1", ...]}}

- passed=true when the diff fulfils the plan with no SERIOUS problem (obvious bug,
  security risk, out-of-scope change). Style/nits do not fail it.
- objections: empty when passed; specific and actionable when not.

## Plan
{plan}

## Final diff
{diff}
"""


class ModelL2ReviewSession:
    """REAL L2 session (found during the real run on 2026-07-22: the builder's
    import pointed at a nonexistent module — `dse_sandbox_runtime` — and EVERY
    deployment fell back to the fake that always approves). Same pattern as
    Planner/Tester: 1 stage=l2 call via the gateway (enforcement + ledger on the
    path), P3 by construction (the prompt only carries plan+diff). Call/parse
    failure → exception (the activity retries; billing/auth become non-retryable
    at the source)."""

    def review(self, inp: L2ReviewInput) -> L2Verdict:
        import os as _os

        from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
        from model_gateway_client.gateway_call import chat_completion
        from model_gateway_client.virtual_keys import mint_virtual_key

        headers = GatewayCallHeaders(
            tenant_id=inp.tenant_id, work_item_id=inp.work_item_id,
            stage=Stage.reviewer, task_class="default", data_class="internal",
        )
        # mint_virtual_key -> str (the raw key); caught by the shadow-run —
        # IssuedVirtualKey's .key only exists on the internal rich return.
        vk_key = mint_virtual_key(inp.tenant_id, inp.work_item_id, Stage.reviewer)
        model = _os.environ.get("DSE_L2_MODEL") or _os.environ.get("DSE_CODER_MODEL", "anthropic/claude")
        prompt = _L2_PROMPT.format(
            plan=inp.plan.model_dump_json()[:4000], diff=(inp.diff or "(empty diff)")[:20000]
        )
        # `complete_json` (rc.104) faz o parse, UM retry com o erro na cara e a
        # ordem de encolher, e devolve o custo de TODAS as tentativas. Antes
        # daqui: `raw_decode` cru contra `max_tokens=1500` e um prompt de até
        # 20.000 chars de diff, sob `maximum_attempts=0` — com `temperature=0`,
        # a mesma entrada produzia a mesma saída truncada e a activity retentava
        # até o `schedule_to_close` de 7200s, com o custo de cada tentativa fora
        # do orçamento do item.
        cobrado = {"usd": 0.0}

        def _chama(texto_prompt: str) -> tuple[str, float]:
            r = chat_completion(
                headers=headers, virtual_key=vk_key, model=model,
                messages=[{"role": "user", "content": texto_prompt}],
                timeout=120.0, max_tokens=1500, temperature=0,
            )
            custo = float(r.cost_usd or 0.0)
            cobrado["usd"] += custo
            return (r.content or ""), custo

        try:
            verdict, custo_total = complete_json(_chama, prompt)
        except ModelJsonError as exc:
            # O custo já faturado viaja no erro; o chamador o consome antes de
            # deixar a exceção subir (a activity a marca como não-retentável).
            exc.cost_usd = cobrado["usd"]
            raise
        return L2Verdict(
            work_item_id=inp.work_item_id,
            passed=bool(verdict.get("passed")),
            objections=[str(o) for o in (verdict.get("objections") or [])][:10],
            cost_usd=custo_total,
        )


def build_l2_session() -> L2ReviewSession:
    """L2 session by CONFIG (P1): real substrate configured → ModelL2ReviewSession
    (model via gateway); otherwise the explicit fake (local/test mode, with a
    WARNING — P8: never silently)."""
    if os.environ.get("DSE_CODER_SUBSTRATE", "fake").strip().lower() != "fake":
        logger.info("dse_validation.l2: REAL Reviewer L2 session (model via gateway)")
        return ModelL2ReviewSession()
    logger.warning(
        "dse_validation.l2: DSE_CODER_SUBSTRATE=fake — using FakeL2ReviewSession "
        "(local/test mode, NOT production)"
    )
    return FakeL2ReviewSession()
