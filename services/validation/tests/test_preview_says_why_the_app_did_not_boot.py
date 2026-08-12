"""Quando o pod não fica Ready, o motivo vem do POD — não do `kubectl wait`.

Medido duas vezes em 2026-08-11, nas PRs #2 e #3, geradas de forma
independente. Nos dois casos o preview provisionou, o pod entrou em
CrashLoopBackOff, e o `PreviewRef` voltou com

    preview degraded: RuntimeError: kubectl wait ... failed (exit=1): timed out

que é uma descrição do RELÓGIO, não do defeito. A frase que dizia o que fazer
estava no log do pod, e só lá:

    Error: Schema validation failed with the following errors:
      Data path "" must have required property 'buildTarget'.

Quem descobriu isso fui eu, à mão, com `kubectl logs`, duas vezes. Nenhum
humano revisando a PR faria isso — nem deveria precisar.

Este arquivo fixa duas coisas:

  1. o `detail` do preview degradado carrega as últimas linhas do log do pod,
     porque é ali que mora a causa;
  2. a captura NUNCA piora a situação: se o `kubectl logs` também falhar, o
     motivo original do timeout continua no `detail`. Trocar um erro ruim por
     nenhum erro seria regressão.

O que ele deliberadamente NÃO faz: decidir de quem é a culpa. Separar "o app
tem defeito" de "a infra não respondeu" é o que autoriza gastar turno de Coder,
e essa decisão tem casa própria — é a mesma lição do `_l1_infra_gates`, que
existe porque confundir os dois custou US$ 18,90 num item que nenhum Coder
podia consertar.
"""
from __future__ import annotations

from dse_validation.config import PreviewConfig
from dse_validation.preview.argocd import pod_failure_detail

_LOG_REAL = """
> angular-login-app@1.0.0 start
> ng serve --host 0.0.0.0 --port 3000 --allowed-hosts preview-x.example

Error: Schema validation failed with the following errors:
  Data path "" must have required property 'buildTarget'.
"""


def test_the_pod_error_reaches_the_detail(monkeypatch):
    """O caso literal das PRs #2 e #3."""
    import dse_validation.preview.argocd as arg

    monkeypatch.setattr(arg, "_kubectl",
                        lambda cfg, args, **kw: type("P", (), {"stdout": _LOG_REAL})())

    detalhe = pod_failure_detail(PreviewConfig(), "preview-x", "timed out waiting")

    assert "buildTarget" in detalhe, (
        f"o erro do app não chegou ao detail: {detalhe!r}. Sem ele, a PR diz "
        "'timed out' e o humano precisa de kubectl para descobrir o motivo"
    )


def test_the_original_timeout_survives_the_capture(monkeypatch):
    """A causa do `kubectl wait` continua no texto: ela diz QUANDO desistiu, e o
    log diz POR QUÊ. As duas juntas são o diagnóstico."""
    import dse_validation.preview.argocd as arg

    monkeypatch.setattr(arg, "_kubectl",
                        lambda cfg, args, **kw: type("P", (), {"stdout": _LOG_REAL})())

    detalhe = pod_failure_detail(PreviewConfig(), "preview-x", "timed out after 900s")
    assert "timed out after 900s" in detalhe


def test_a_log_that_cannot_be_read_never_erases_the_reason(monkeypatch):
    """PIN: a captura é um BÔNUS. Se o cluster não devolve o log — pod já
    apagado, RBAC, kubectl fora do ar — o motivo original tem de sobreviver
    inteiro. Trocar um erro ruim por nenhum erro é regressão."""
    import dse_validation.preview.argocd as arg

    def _explode(cfg, args, **kw):
        raise RuntimeError("Error from server (Forbidden): pods is forbidden")

    monkeypatch.setattr(arg, "_kubectl", _explode)

    detalhe = pod_failure_detail(PreviewConfig(), "preview-x", "timed out after 900s")
    assert "timed out after 900s" in detalhe, (
        f"o motivo sumiu quando o log falhou: {detalhe!r}"
    )


def test_a_huge_log_does_not_become_the_detail(monkeypatch):
    """`npm install` sozinho passa de mil linhas. O detail vai para o corpo da
    PR e para o ledger — as ÚLTIMAS linhas são onde o erro mora."""
    import dse_validation.preview.argocd as arg

    gigante = "\n".join(f"npm notice linha {i}" for i in range(5000)) + "\nError: boom"
    monkeypatch.setattr(arg, "_kubectl",
                        lambda cfg, args, **kw: type("P", (), {"stdout": gigante})())

    detalhe = pod_failure_detail(PreviewConfig(), "preview-x", "timed out")
    assert len(detalhe) < 2000, f"o detail tem {len(detalhe)} chars — é um dump"
    assert "Error: boom" in detalhe, "cortou pelo fim e perdeu justamente o erro"


def test_an_empty_log_says_so_instead_of_pretending(monkeypatch):
    """Pod que morreu antes de escrever qualquer coisa (image pull, OOM no
    arranque) tem log vazio. Dizer isso é informação; devolver o timeout pelado
    faz o humano procurar um log que não existe."""
    import dse_validation.preview.argocd as arg

    monkeypatch.setattr(arg, "_kubectl",
                        lambda cfg, args, **kw: type("P", (), {"stdout": "   \n"})())

    detalhe = pod_failure_detail(PreviewConfig(), "preview-x", "timed out")
    assert "timed out" in detalhe
    assert "log" in detalhe.lower()


def test_a_crashlooping_pod_still_gives_up_its_log(monkeypatch):
    """CrashLoopBackOff é EXATAMENTE o caso que nos interessa, e é o caso em que
    `kubectl logs` sem `--previous` falha.

    Medido na PR #5 (2026-08-11 21:13): o pod morria com
    `npm error Missing script: "start"`, e a linha que chegou à PR trazia só o
    timeout do `kubectl wait` — sem o log e sem o sufixo de "log vazio", ou
    seja, a captura tinha levantado. Em backoff o container está *waiting*, e o
    log do processo que morreu só sai com `--previous`.

    O sintoma é traiçoeiro: a captura falha justamente quando ela seria mais
    útil, porque só o pod que ESTÁ reiniciando tem algo a dizer."""
    import dse_validation.preview.argocd as arg

    tentativas: list[bool] = []

    def _kubectl(cfg, args, **kw):
        anterior = "--previous" in args
        tentativas.append(anterior)
        if not anterior:
            raise RuntimeError(
                "error: container preview is waiting to start: CrashLoopBackOff")
        return type("P", (), {"stdout": 'npm error Missing script: "start"'})()

    monkeypatch.setattr(arg, "_kubectl", _kubectl)

    detalhe = pod_failure_detail(PreviewConfig(), "preview-x", "timed out")

    assert any(tentativas), "nunca tentou `--previous`, que é o log do container morto"
    assert "Missing script" in detalhe, (
        f"o log do container em backoff não chegou ao detail: {detalhe!r}"
    )


def test_when_both_attempts_fail_the_reason_still_survives(monkeypatch):
    """PIN: duas tentativas falhando continua sendo melhor que apagar o motivo."""
    import dse_validation.preview.argocd as arg

    def _sempre_falha(cfg, args, **kw):
        raise RuntimeError("Forbidden")

    monkeypatch.setattr(arg, "_kubectl", _sempre_falha)
    assert pod_failure_detail(PreviewConfig(), "preview-x", "timed out") == "timed out"
