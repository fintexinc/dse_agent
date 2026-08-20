"""Preview degradado → o agente decide se uma MUDANÇA DE CÓDIGO conserta.

Decisão de operador (2026-08-12, caso do wi_a8b760de/PR #6): o laço fecha sem
humano. Este módulo é a metade de CONTEÚDO da decisão — o agente recebe o erro
(as palavras do pod, que a rc.85 passou a capturar) e os arquivos-chave do repo
na branch da task, e devolve {fixable, reason, instruction}. A metade de
POLÍTICA (tetos, no-op, gasto, despacho do fix) é código no workflow
(`_maybe_autofix_degraded_preview`) — nenhum modelo decide fluxo.

Mesmo padrão do L2 (`l2/session.py`): sessão por CONFIG (substrato fake →
sessão fake determinística, com WARNING), chamada única via gateway
(enforcement + ledger no caminho), `temperature=0`, `raw_decode` no parse.
Saída malformada LEVANTA — a activity retenta (finito, no call site do
workflow); um default silencioso transformaria "modelo quebrado" em "veredito
infra", que é exatamente a confusão que o L1 já pagou US$ 18,90 para aprender
a não fazer.
"""
from __future__ import annotations

import logging
import os
from collections import deque
from typing import Protocol

from dse_contracts import PreviewTriageVerdict, TriagePreviewFailureInput

logger = logging.getLogger("dse_validation.preview.triage")

#: Os arquivos que costumam carregar a causa de um preview morto por código —
#: manifesto de deps, config de builders, o manifesto do L1 e o Dockerfile.
# Fase A3 (2026-08-19): um preview Java quebrado era triado a partir de
# `angular.json` — o modelo respondia sem ver o manifesto de build do
# ecossistema real. A lista cobre os ecossistemas que o DSE roda; arquivo
# ausente custa um 404 barato e não entra no prompt.
_KEY_FILES = ("package.json", "angular.json", "pom.xml", "build.gradle",
              "go.mod", "requirements.txt", ".dse/validation.json", "Dockerfile")
_FILE_CHARS = 6000
_DETAIL_CHARS = 2000

_PROMPT = """You are the Fintex DSE preview-failure triager. A per-PR preview environment \
failed to come up for this repository branch. Decide ONLY whether a CODE CHANGE in this \
repository can fix it. Infrastructure/platform causes (cluster down, registry, quota, \
DNS, TLS, permissions) are NOT fixable here.

Answer ONLY with valid JSON:
{{"fixable": true|false, "reason": "...", "instruction": "..."}}

- fixable=true only when the failure detail points at something IN the repository \
(missing dependency, broken script or config, bad builder reference).
- instruction: imperative and specific, for a coding agent working on this branch; \
empty when not fixable.

## Failure detail (the pod's words)
{detail}

## Key repository files (at the task branch)
{files}
"""


class PreviewTriageSession(Protocol):
    def decide(self, prompt: str) -> tuple[dict, float]: ...


class FakePreviewTriageSession:
    """Fixture determinística (não-produção), scriptável como a do L2: uma fila
    de dicts crus consumida em ordem; vazia → veredito infra (não gasta turno)."""

    def __init__(self, scripted: list[dict] | None = None):
        self._queue: deque = deque(scripted or [])
        self.prompts: list[str] = []

    def decide(self, prompt: str) -> tuple[dict, float]:
        self.prompts.append(prompt)
        if self._queue:
            return self._queue.popleft(), 0.0
        return {"fixable": False, "reason": "fake default (infra)", "instruction": ""}, 0.0


class ModelPreviewTriageSession:
    """Sessão REAL: 1 chamada stage=reviewer via gateway. `Stage.reviewer` de
    propósito — um estágio novo mexeria em enforcement/ledger do gateway, e a
    triage é uma decisão de revisão sobre o trabalho já feito."""

    def decide(self, prompt: str) -> tuple[dict, float]:

        from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
        from dse_validation.model_json import parse_model_json
        from model_gateway_client.gateway_call import chat_completion
        from model_gateway_client.virtual_keys import mint_virtual_key

        tenant_id, work_item_id = self._ids
        headers = GatewayCallHeaders(
            tenant_id=tenant_id, work_item_id=work_item_id,
            stage=Stage.reviewer, task_class="default", data_class="internal",
        )
        vk_key = mint_virtual_key(tenant_id, work_item_id, Stage.reviewer)
        model = (
            os.environ.get("DSE_PREVIEW_TRIAGE_MODEL")
            or os.environ.get("DSE_L2_MODEL")
            or os.environ.get("DSE_CODER_MODEL", "anthropic/claude")
        )
        result = chat_completion(
            headers=headers, virtual_key=vk_key, model=model,
            messages=[{"role": "user", "content": prompt}],
            timeout=120.0, max_tokens=1000, temperature=0,
        )
        text = (result.content or "").strip()
        if text.startswith("```"):
            text = text.strip("`\n")
            text = text[4:] if text.startswith("json") else text
        raw, _motivo = parse_model_json(text)
        if raw is None:
            raise ValueError(f"preview triage answer did not parse: {_motivo}")
        return raw, float(result.cost_usd or 0.0)

    def __init__(self, tenant_id: str = "", work_item_id: str = ""):
        self._ids = (tenant_id, work_item_id)


def build_triage_session(tenant_id: str = "", work_item_id: str = "") -> PreviewTriageSession:
    """Sessão por CONFIG (P1), espelho de `build_l2_session`."""
    if os.environ.get("DSE_CODER_SUBSTRATE", "fake").strip().lower() != "fake":
        logger.info("preview.triage: REAL session (model via gateway)")
        return ModelPreviewTriageSession(tenant_id, work_item_id)
    logger.warning(
        "preview.triage: DSE_CODER_SUBSTRATE=fake — FakePreviewTriageSession "
        "(local/test mode, NOT production)"
    )
    return FakePreviewTriageSession()


def _ledger_detail(work_item_id: str) -> str:
    """As palavras do pod, do ledger (`wse_previews.detail`) — a fonte que
    sobrevive a QUALQUER caminho de exceção. Medido no primeiro veredito de
    produção (wi_9580d984, 2026-08-12): o workflow só tinha o boilerplate do
    relógio ('StartToClose timeout') e o agente diagnosticou 'build lento'
    para uma dependência ausente; a causa real estava gravada aqui pela
    attempt anterior. Bônus, nunca piora: ilegível → string vazia."""
    try:
        from dse_validation import db

        row = db.get_preview(work_item_id)
    except Exception as exc:  # noqa: BLE001 — contexto extra, nunca derruba a triage
        logger.warning("triage: could not read the preview ledger row: %s", exc)
        return ""
    return str((row or {}).get("detail") or "")


def triage_preview_failure_core(
    inp: TriagePreviewFailureInput,
    *,
    github_client=None,
    session: PreviewTriageSession | None = None,
) -> PreviewTriageVerdict:
    """Coleta o contexto (erro + arquivos-chave na branch da task), consulta a
    sessão, valida DURO e devolve o veredito. Se `fixable`, escreve a linha da
    PR ("automatic fix attempt N/cap is running") — o humano que abrir a PR no
    meio do auto-fix vê o que está acontecendo, não um degradado parado."""
    if github_client is None:
        from dse_validation.config import GitHubConfig
        from dse_validation.github.client import build_github_client

        github_client = build_github_client(GitHubConfig())

    # `getattr`: um client de fora do repo (ou um fake antigo) pode não ter
    # get_file_text — a triage decide com o que houver, nunca quebra por isso.
    reader = getattr(github_client, "get_file_text", None)
    partes: list[str] = []
    for path in _KEY_FILES:
        texto = None
        if reader is not None:
            try:
                texto = reader(inp.repo, path, inp.branch)
            except Exception as exc:  # noqa: BLE001 — arquivo ilegível ≠ triage morta
                logger.warning("triage: could not read %s@%s: %s", path, inp.branch, exc)
        partes.append(f"### {path}\n{(texto or '(unavailable)')[:_FILE_CHARS]}")

    # O detail recebido pode ser só o relógio (caminho do timeout). O ledger é
    # onde as palavras do pod moram — mescla SEMPRE que trouxer algo novo.
    detalhe = (inp.detail or "(no detail was captured)")[:_DETAIL_CHARS]
    do_ledger = _ledger_detail(inp.work_item_id)
    if do_ledger and do_ledger[:200] not in detalhe:
        detalhe += (
            "\n\n### Last recorded preview outcome (platform ledger)\n"
            + do_ledger[:_DETAIL_CHARS]
        )
    prompt = _PROMPT.format(
        detail=detalhe,
        files="\n\n".join(partes),
    )
    session = session or build_triage_session(inp.tenant_id, inp.work_item_id)
    raw, cost = session.decide(prompt)
    if not isinstance(raw, dict) or "fixable" not in raw:
        # Malformado LEVANTA (retry finito no workflow) — default silencioso
        # viraria "veredito infra" e ninguém saberia que o modelo quebrou.
        raise ValueError(
            f"preview triage: model output is missing 'fixable': {str(raw)[:200]}"
        )
    verdict = PreviewTriageVerdict(
        work_item_id=inp.work_item_id,
        fixable=bool(raw.get("fixable")),
        reason=str(raw.get("reason") or "")[:1000],
        instruction=str(raw.get("instruction") or "")[:4000],
        cost_usd=float(cost or 0.0),
    )

    if verdict.fixable and inp.pr_number:
        from dse_contracts.activities import TriggerPreviewInput

        from dse_validation.preview import argocd

        argocd._put_preview_in_pr_body(
            TriggerPreviewInput(
                work_item_id=inp.work_item_id, tenant_id=inp.tenant_id,
                repo=inp.repo, pr_number=inp.pr_number, files_changed=[],
            ),
            argocd.preview_autofix_line(
                inp.detail or "", inp.autofix_round, inp.autofix_cap
            ),
            actor="system:validation",
        )
    return verdict
