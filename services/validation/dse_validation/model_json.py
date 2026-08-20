"""One parser for model answers — with the retry rc.102 already proved.

Six copies of `json.JSONDecoder().raw_decode(...)` existed across the platform,
each with its own policy for fences, retries and cost. One of them had already
cost a work item: on 2026-08-19 the Tester's authoring answer came back
truncated at the token ceiling, the parse failed, and the item ended TERMINAL
with a paid Coder turn thrown away.

The worse sibling is the L2 reviewer: `max_tokens=1500` against a prompt that
carries up to 20,000 characters of diff, `temperature=0` — so the same input
deterministically produces the same truncated answer — and an activity policy
of `maximum_attempts=0` whose non-retryable list does not include a parse
error. That combination retries until the 7200s ceiling, and the cost of every
attempt lands outside the work item's budget because the consumption only runs
on the success path.

Three rules, and they are the whole module:

  1. one attempt, then ONE retry carrying the parse error and an order to
     SHRINK — the failure mode is a token ceiling, not a confused model;
  2. the cost of every attempt that happened is returned (or carried on the
     exception), because the gateway bills the moment it answers;
  3. after the retry, the failure is raised as NON-RETRYABLE — a deterministic
     call repeating itself for two hours is not resilience.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

_SHRINK = (
    "\n\n## YOUR PREVIOUS ANSWER WAS REJECTED\n"
    "It was not valid JSON ({motivo}) — most likely truncated at the output "
    "limit. Answer again with STRICT JSON only, and make it SMALLER: keep only "
    "the fields the schema requires and the most important entries.\n"
)


class ModelJsonError(RuntimeError):
    """A model answer that never parsed. Carries the cost that was already
    billed, and says it must not be retried."""

    non_retryable = True

    def __init__(self, motivo: str, *, cost_usd: float = 0.0):
        super().__init__(motivo)
        self.cost_usd = cost_usd


def strip_fence(text: str) -> str:
    """The ```json wrapper the models keep adding despite the instruction."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`\n")
        t = t[4:] if t.startswith("json") else t
    return t.strip()


def parse_model_json(text: str) -> tuple[Any | None, str]:
    """`(payload, "")` when it parses, `(None, reason)` otherwise.

    `raw_decode` and not `loads`: the model sometimes keeps writing after the
    JSON ("Extra data", caught by the shadow-run) — take the first object and
    ignore the rest."""
    try:
        payload, _ = json.JSONDecoder().raw_decode(strip_fence(text))
        return payload, ""
    except json.JSONDecodeError as exc:
        return None, exc.msg


def complete_json(
    complete: Callable[[str], Any], prompt: str, *, attempts: int = 2,
) -> tuple[Any, float]:
    """Call the model, parse, and on failure retry ONCE with the error in its
    face. `complete(prompt)` returns the text, or `(text, cost_usd)`.

    Returns `(payload, total_cost)`. Raises `ModelJsonError` (non-retryable,
    carrying the cost that was already billed) when nothing parsed."""
    custo_total = 0.0
    motivo = ""
    sufixo = ""
    for _ in range(max(1, attempts)):
        resposta = complete(prompt + sufixo)
        if isinstance(resposta, tuple):
            texto, custo = resposta
            custo_total += float(custo or 0.0)
        else:
            texto = resposta
        payload, motivo = parse_model_json(texto)
        if payload is not None:
            return payload, custo_total
        logger.warning("model answer did not parse (%s); response: %.200s", motivo, texto)
        sufixo = _SHRINK.format(motivo=motivo)
    raise ModelJsonError(
        f"the model answer never parsed as JSON ({motivo})", cost_usd=custo_total
    )
