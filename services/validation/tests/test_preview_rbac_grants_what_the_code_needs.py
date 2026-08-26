"""O chart concede o que `pod_failure_detail` precisa — senão a feature é morta.

Medido em produção (2026-08-11, PR #6 do bmo-test-dse-fe, wi_a8b760de…): o pod
do preview estava em CrashLoopBackOff com o motivo inteiro no log, e as DUAS
tentativas de captura falharam com

    Error from server (Forbidden): pods "preview-5bb67bfbcc-jtf4p" is forbidden:
    User "system:serviceaccount:dse:dse-dse-orchestrator-worker" cannot get
    resource "pods/log" in API group "" in the namespace "preview-wi-a8b760de…"

A causa não é bug de código: é o `preview-rbac.yaml`, que diz textualmente
"No exec, no logs, no delete". Essa fronteira era correta quando foi escrita —
e ficou para trás quando as rc.83/84 criaram a captura de log. Resultado: a
feature "o preview degradado fala com as palavras do pod" NUNCA funcionou na
VPS; toda PR degradada recebeu o timeout pelado e o humano voltou a precisar de
kubectl, que é exatamente o que a feature existe para evitar.

O teste é sobre o TEMPLATE do chart, não sobre um cluster: é o mais perto que a
suíte chega da RBAC real sem provisionar nada — e cobre a regressão que importa,
alguém apertar a role e re-matar a captura sem perceber. `pods/log` é leitura de
stdout de pod de preview (código que o próprio DSE gerou), no ClusterRole que já
enxerga o pod inteiro via `get pods`; não é ampliação de superfície comparável a
exec, que segue proibido.
"""
from __future__ import annotations

import re
from pathlib import Path

_CHART = Path(__file__).resolve().parents[3] / "infra" / "helm" / "dse" / "templates" / "preview-rbac.yaml"


def test_the_preview_role_grants_pod_log_read():
    """A regra literal que faltou em produção: `pods/log`, verbo `get`."""
    text = _CHART.read_text(encoding="utf-8")
    assert "pods/log" in text, (
        "o ClusterRole do preview não concede `pods/log`: `kubectl logs` morre "
        "em Forbidden e a captura de `pod_failure_detail` (rc.83/84) fica morta "
        "em produção — medido na PR #6, wi_a8b760de…, 2026-08-11"
    )
    # e com o verbo certo, na mesma lista de regras — `kubectl logs` é um GET
    # no subresource, não um list/watch.
    rule = re.search(r'resources:\s*\[\s*"pods/log"\s*\][^\n]*\n\s*verbs:\s*\[([^\]]*)\]', text)
    assert rule and '"get"' in rule.group(1), (
        "`pods/log` aparece no template mas sem `get` na mesma regra — "
        "a captura continua Forbidden"
    )


def test_the_grant_lands_on_the_worker_that_runs_the_capture():
    """O sujeito do binding é o SA do erro medido: `…-orchestrator-worker`.
    Se o binding apontar para outro SA, a regra existe e a captura continua
    Forbidden — o Forbidden de produção nomeia exatamente esse worker."""
    text = _CHART.read_text(encoding="utf-8")
    assert "-orchestrator-worker" in text, (
        "o ClusterRoleBinding do preview não aponta para o orchestrator-worker; "
        "foi ele quem levou o Forbidden em produção"
    )


def test_every_secret_the_preview_applies_is_reapplyable():
    """`create` de secrets é irrestrito, mas get/update/patch/delete são
    pinados por NOME — `kubectl apply` faz um GET antes de escrever, então uma
    secret fora da lista morre em Forbidden JÁ NO PRIMEIRO apply.

    A lista vem do CÓDIGO, não escrita à mão aqui, e essa é a tese deste
    teste. A versão anterior repetia `dse-preview-build-credentials` como
    literal; quando o Tema 1 (rc.120) acrescentou a terceira secret — a senha
    dos `services` — a lista manual não acompanhou, o conformance passou verde
    e o preview de TODO repo que declara `services` quebrou em produção
    (wi_bb9a7a99, 2026-08-26). Derivando das constantes, a próxima secret
    quebra a suíte no mesmo commit que a introduz."""
    from dse_validation.preview.argocd import (
        BUILD_CREDENTIALS_SECRET,
        DEPLOY_KEY_SECRET,
        SERVICE_PASSWORD_SECRET,
    )

    text = _CHART.read_text(encoding="utf-8")
    for nome in (DEPLOY_KEY_SECRET, BUILD_CREDENTIALS_SECRET, SERVICE_PASSWORD_SECRET):
        assert nome in text, (
            f"a secret {nome!r} é aplicada pelo preview mas não está no "
            "resourceNames do ClusterRole — o apply toma 403 e o preview "
            "degrada inteiro"
        )
