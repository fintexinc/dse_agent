"""Preview que foi TENTADO sempre aparece na PR — inclusive quando falhou.

Em 2026-08-11 o preview falhou seis vezes seguidas, por seis causas diferentes,
e nas seis o humano que abriu a PR viu exatamente a mesma coisa: **nada**.
Nenhuma linha no corpo, nenhum comentário. A informação existia em `audit_log`,
em `wse_previews` e em `work_item_evidence` — três superfícies que ninguém
revisando uma PR abre.

A causa é estrutural, não um esquecimento: existe UM ponto de escrita na PR
(`_put_preview_link_in_pr_body`), chamado de UM lugar, que fica DEPOIS dos
quatro `return` de `trigger_preview_core`. Degradado nunca chega lá. E o próprio
ponto de escrita tem um `except Exception` que engole falha de API sem emitir
evento — quando ele falha, nem o ledger sabe.

O que estes testes fixam:

  - a frase que descreve um preview é UMA função, exaustiva sobre os status. Um
    status novo não pode nascer mudo: ou tem frase, ou está declarado como
    silencioso de propósito;
  - `created` e `degraded` SEMPRE escrevem na PR. `degraded` carrega a CAUSA —
    "cap de concorrência", "o app não subiu", com o erro real — porque
    "degradado" sozinho não diz a ninguém o que fazer;
  - pulo legítimo (mudança sem front-end, preview desligado no binding) NÃO
    escreve. Decisão de operador de 2026-08-11: a seção aparece só quando havia
    preview a fazer, senão vira ruído em toda PR de backend.

A idempotência continua sendo do marker de linha única que já existia — é por
isso que a frase é uma linha, e não uma seção de várias.
"""
from __future__ import annotations

import pytest
from dse_contracts.activities import TriggerPreviewInput

from dse_validation.preview.argocd import preview_body_line

#: Todo status que o código de produção sabe construir. A lista é exaustiva de
#: propósito: se alguém acrescentar um status e não vier aqui, o teste de
#: exaustividade abaixo falha.
_STATUS_QUE_FALAM = ("created", "degraded", "reaped")
_STATUS_SILENCIOSOS = ("skipped_disabled", "skipped_backend_only")


@pytest.mark.parametrize("status", _STATUS_QUE_FALAM)
def test_every_attempted_preview_has_a_sentence(status: str):
    """Nenhum desfecho de uma tentativa pode ser silencioso."""
    linha = preview_body_line(
        status, url="https://p.example/", namespace="preview-wi-x",
        detail="alguma causa",
    )
    assert linha, f"status {status!r} não produz frase nenhuma — o humano vê silêncio"
    assert linha.startswith("- **Preview**:"), (
        f"a frase de {status!r} não usa o bullet que o marker de idempotência "
        "reconhece; ela seria duplicada a cada re-trigger"
    )


@pytest.mark.parametrize("status", _STATUS_SILENCIOSOS)
def test_a_legitimate_skip_writes_nothing(status: str):
    """Decisão de operador: pulo legítimo não gera seção. Sem isto, toda PR de
    backend ganharia uma linha dizendo que não há front-end para ver."""
    assert preview_body_line(status, url=None, namespace=None, detail="") is None


def test_the_degraded_sentence_carries_the_cause():
    """«degraded» sozinho não diz a ninguém o que fazer. As duas causas reais de
    hoje têm de aparecer no texto: o teto de concorrência e o app que não sobe."""
    cap = preview_body_line(
        "degraded", url=None, namespace=None,
        detail="tenant concurrent preview cap reached (3/3, ADR-26)",
    )
    assert "3/3" in cap, f"a causa sumiu da frase: {cap!r}"

    app = preview_body_line(
        "degraded", url=None, namespace=None,
        detail="ng serve: Schema validation failed: must have required property 'buildTarget'",
    )
    assert "buildTarget" in app, (
        f"o erro do runtime sumiu: {app!r}. Foi exatamente essa string que "
        "identificou o defeito das PRs #2 e #3, e ela só existia no log do pod"
    )


def test_the_sentence_survives_a_detail_that_is_missing_or_huge():
    """O detail vem do runtime: pode não existir, ou ser um dump de log inteiro.
    Nenhum dos dois pode quebrar o corpo da PR."""
    sem = preview_body_line("degraded", url=None, namespace=None, detail=None)
    assert sem and "- **Preview**:" in sem

    enorme = preview_body_line("degraded", url=None, namespace=None, detail="x" * 5000)
    assert len(enorme) < 1000, "um log inteiro foi despejado no corpo da PR"
    assert "\n" not in enorme, (
        "a frase virou multi-linha: o marker de idempotência é `re.M` sobre UMA "
        "linha, então a próxima escrita duplicaria em vez de substituir"
    )


def test_an_unknown_status_is_loud_rather_than_silent():
    """PIN da decisão inteira. O modo de falha que custou o dia foi silêncio, não
    erro — então um status que ninguém previu tem de produzir texto, não None."""
    linha = preview_body_line("algo_que_ninguem_previu", url=None, namespace=None, detail="")
    assert linha, (
        "um status desconhecido devolveu None e o humano voltaria a ver silêncio; "
        "prefira uma frase feia dizendo o status cru"
    )
    assert "algo_que_ninguem_previu" in linha


class _FakePr:
    """Cliente do GitHub que guarda o corpo, como o dos testes vizinhos."""

    def __init__(self, body: str):
        self.body = body
        self.updates = 0

    def get_pull_request(self, repo, n):
        return {"body": self.body}

    def update_pull_request(self, repo, n, *, body):
        self.body = body
        self.updates += 1


_CORPO = (
    "### Fintex DSE\n\n"
    "- **WorkItem**: `wi_x`\n"
    "- **Test evidence (L1)**: L1 green\n"
)


def test_a_degraded_preview_reaches_the_pr_body(monkeypatch):
    """O caso literal de 2026-08-11: cap 3/3, PR #4 aberta, corpo SEM nenhuma
    menção a preview e zero comentários."""
    import dse_validation.github.client as gc
    import dse_validation.preview.argocd as arg

    fake = _FakePr(_CORPO)
    monkeypatch.setattr(gc, "build_github_client", lambda cfg=None: fake)
    inp = TriggerPreviewInput(
        work_item_id="wi_9842e28f", tenant_id="fintex-poc", repo="a/b", pr_number=4,
        files_changed=["src/app/login/login.component.ts"],
    )

    arg._put_preview_in_pr_body(
        inp,
        preview_body_line("degraded", url=None, namespace=None,
                          detail="tenant concurrent preview cap reached (3/3, ADR-26)"),
        actor="system:test",
    )

    assert "**Preview**" in fake.body, (
        "a PR continua sem dizer nada sobre o preview — é exatamente o silêncio "
        "que este arquivo existe para impedir"
    )
    assert "3/3" in fake.body
    # e no lugar certo, logo abaixo da evidência de L1
    assert fake.body.index("- **Test evidence (L1)**:") < fake.body.index("- **Preview**:")


def test_rewriting_replaces_instead_of_stacking(monkeypatch):
    """Um fix cycle re-dispara o preview. O degradado de antes tem de virar o
    link de agora, não acumular duas linhas contraditórias."""
    import dse_validation.github.client as gc
    import dse_validation.preview.argocd as arg

    fake = _FakePr(_CORPO)
    monkeypatch.setattr(gc, "build_github_client", lambda cfg=None: fake)
    inp = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", repo="a/b", pr_number=7,
        files_changed=["src/x.ts"],
    )

    arg._put_preview_in_pr_body(
        inp, preview_body_line("degraded", url=None, namespace=None, detail="cap"),
        actor="t")
    arg._put_preview_in_pr_body(
        inp, preview_body_line("created", url="https://p.example/", namespace="ns"),
        actor="t")

    assert fake.body.count("- **Preview**:") == 1
    assert "https://p.example/" in fake.body
    assert "cap" not in fake.body


def test_a_failed_write_is_audited_instead_of_swallowed(monkeypatch):
    """O `except Exception` mudo do caminho de escrita: quando a API do GitHub
    recusa, hoje sai um `logger.warning` e NADA no ledger — o único sinal de que
    a PR não recebeu o preview desaparece junto com o processo."""
    import dse_validation.preview.argocd as arg
    import dse_validation.github.client as gc

    class _Explode:
        def get_pull_request(self, repo, n):
            raise RuntimeError("401 Bad credentials")

    monkeypatch.setattr(gc, "build_github_client", lambda cfg=None: _Explode())
    emitidos: list[str] = []
    monkeypatch.setattr(arg, "audit_emit",
                        lambda **kw: emitidos.append(kw.get("action", "")))

    inp = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", repo="a/b", pr_number=9,
        files_changed=["src/x.ts"],
    )
    arg._put_preview_in_pr_body(
        inp, preview_body_line("created", url="https://p/", namespace="ns"), actor="t")

    assert any("preview" in a and "fail" in a for a in emitidos), (
        f"nenhum evento de falha no ledger (emitidos: {emitidos}). A escrita na "
        "PR pode falhar por token, 4xx ou PR inexistente, e hoje isso some"
    )


def test_the_pr_is_not_silent_while_the_preview_is_still_coming_up(monkeypatch):
    """A janela de provisionamento também é silêncio.

    Medido na PR #5 (2026-08-11 20:58): a PR finalizou, o operador abriu, e não
    havia linha de preview nenhuma. Não era defeito de escrita — o
    `trigger_preview_core` estava DENTRO do `_wait_deployment_available`, que
    espera até 900s. A frase só era escrita no `return`, ou seja, até 15 minutos
    depois.

    Quinze minutos de silêncio são indistinguíveis de falha para quem abriu a
    PR, e foi exatamente essa a leitura. A linha tem de existir assim que o
    ambiente COMEÇA a subir, e ser reescrita quando ele termina — o marker de
    idempotência já garante substituição, não acúmulo."""
    import dse_validation.preview.argocd as arg

    fake = _FakePr(_CORPO)
    monkeypatch.setattr("dse_validation.github.client.build_github_client",
                        lambda cfg=None: fake)
    inp = TriggerPreviewInput(
        work_item_id="wi_553d907d", tenant_id="fintex-poc", repo="a/b", pr_number=5,
        files_changed=["src/app/login/login.component.ts"],
    )

    arg._announce_preview_provisioning(inp, "https://p.example/", actor="system:test")

    assert "**Preview**" in fake.body, (
        "a PR fica muda enquanto o ambiente sobe — para quem a abre, isso é "
        "indistinguível de não ter preview nenhum"
    )
    assert "https://p.example/" in fake.body, (
        "a URL já é conhecida quando o namespace é aplicado; escondê-la até o "
        "pod ficar Ready não protege de nada"
    )


def test_the_provisioning_line_is_replaced_by_the_outcome(monkeypatch):
    """PIN: o aviso de 'subindo' não pode sobreviver ao desfecho. Uma PR que diz
    'provisioning' para sempre é pior que uma muda — ela afirma algo falso."""
    import dse_validation.preview.argocd as arg

    fake = _FakePr(_CORPO)
    monkeypatch.setattr("dse_validation.github.client.build_github_client",
                        lambda cfg=None: fake)
    inp = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", repo="a/b", pr_number=5,
        files_changed=["src/x.ts"],
    )

    arg._announce_preview_provisioning(inp, "https://p.example/", actor="t")
    arg._put_preview_in_pr_body(
        inp, arg.preview_body_line("degraded", detail="npm error Missing script: start"),
        actor="t")

    assert fake.body.count("- **Preview**:") == 1
    assert "Missing script" in fake.body
