"""Alcançabilidade das fronteiras de posse (2026-08-07).

Para cada célula (quem quebrou, em que arquivo a falha se manifesta, modo de
falha), tem que existir PELO MENOS UM ator autorizado a corrigir — ou uma
escalada DESENHADA para humano. Morrer no teto de retentativas não é saída:
é exaustão, e foi exatamente assim que wi_5620d2c1 e wi_8edaef39 morreram
antes das portas 1/5.

As regras modeladas aqui espelham código real (referências abaixo). Isto NÃO
importa os serviços — é um mapa executável; quem mudar uma regra atualiza a
célula, e o teste força isso: célula viva sem saída falha; beco documentado
que ganhar saída vira XPASS estrito e falha também, exigindo promover a
célula.

As regras e onde vivem:
  R1 revert do INSTRUMENTO         — workspace_hygiene.revert_test_edits (2026-08-10):
                                     o Coder edita spec do CLIENTE à vontade (a mudança
                                     entra no diff da PR); o que o pós-turno desfaz é a
                                     edição sobre o INSTRUMENTO deste laço — a spec com
                                     commit `tester(<este work_item_id>)`. Antes: todo
                                     caminho de teste era revertido.
  R2 posse do Tester               — activities.py:_is_dse_authored (~2347) + rename guard
                                     (~2329): o Tester nunca destrói spec do cliente.
  R3 reuso existencial + exceção   — activities.py (~2889: reused) + porta 5
     zero-veredito                   (_zero_verdict_specs + repair in-place, rc.42):
                                     re-autoria SÓ quando a suite própria não executa
                                     asserção alguma (carga/compilação).
  R4 deferral                      — activities.py:_suite_verdict_deferred (~2034):
                                     suite própria falhando não é gate; L1 julga.
  R5 detector da porta 1           — dse_orchestrator/workflows.py:preexisting_spec_conflicts:
                                     spec FAIL não-do-Tester com SUJEITO no diff
                                     ACUMULADO (v2). Em DOIS estágios (v3): verde no
                                     base + 1ª ocorrência → o CODER ganha uma rodada
                                     com as asserções (spec_conflict_deferred_to_coder);
                                     a MESMA spec de novo → parque p/ humano. Vermelha
                                     no base nunca entra (é achado herdado, R8).
  R6 forbidden_paths               — validation (plan_compliance): gate sobre o diff do
                                     Coder; o próprio Coder pode remover o que criou.
  R7 diff_budget                   — validation: idem — o Coder pode encolher o diff.
  R8 baseline check                — l1/quality_checks.py:_baseline_failing_suites +
                                     test_check(base_sha=): suite já vermelha no base
                                     vira NOT_OUR_FAILURE e o item SEGUE (promoveu os
                                     becos 2 e 3 deste mapa).
  R9 exaustão em spec própria      — workflows.py:exclusively_tester_spec_failures +
                                     tester_spec_assertion_failures (a MEMÓRIA) +
                                     parque na primitiva da porta 1. Gatilho v2 por
                                     MEMÓRIA DE SPEC: a mesma spec própria reprovando
                                     com veredito em QUALQUER rodada posterior parqueia,
                                     independente do que falhou entre elas — o
                                     fingerprint consecutivo resetava numa rodada de
                                     lint+build no meio e o wi_c9c7b200 morreu no teto
                                     com o parque deployado. Medidos: pageSize
                                     wi_5eecf486, 'warning' wi_32eb136f, alternado
                                     wi_c9c7b200.
  R10 pinça declarada pelo ator     — workflows.py, ramo do coder_made_no_change:
                                     dois no-ops consecutivos com a pinça ARMADA
                                     (última falha de L1 exclusivamente spec própria
                                     com veredito) parqueiam com o MESMO dossiê do
                                     caminho via L1, em vez da escalada muda — o
                                     wi_0d95384f escalou sem dossiê porque o no-op
                                     pula os gates e a memória nunca via a 2ª
                                     reprovação. No-op sem pinça armada escala.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

PRODUCAO = "producao"
SPEC_TESTER = "spec_tester"
SPEC_CLIENTE = "spec_cliente"

ASSERCAO = "assercao"              # a suite EXECUTOU e reprovou — existe veredito
ASSERCAO_CODER_SEM_JOGADA = "assercao_coder_sem_jogada"  # pinça declarada: spec própria
                                   # reprovada + o Coder no-opa 2x ("não tenho jogada")
ZERO_VEREDITO = "zero_veredito"    # carga/compilação da spec morreu — sem veredito
COMPILE_PRODUCAO = "compile_producao"
FORBIDDEN_PATHS = "forbidden_paths"
DIFF_BUDGET = "diff_budget"


@dataclass(frozen=True)
class Celula:
    quem: str        # coder | tester | cliente (estado pré-existente do repo)
    arquivo: str     # onde a falha se manifesta
    modo: str
    sujeito_no_diff: bool = True  # só relevante para SPEC_CLIENTE (R5/v2: diff ACUMULADO)
    nota: str = ""


def saidas(c: Celula) -> set[str]:
    """Os atores/parques autorizados pelas regras R1-R7 — deny-by-default."""
    s: set[str] = set()

    # Coder: autorizado em PRODUÇÃO e nos gates sobre o próprio diff (R6/R7).
    # R1 o exclui apenas do INSTRUMENTO (SPEC_TESTER) — spec do cliente ele
    # edita, e a saída aparece em R5 logo abaixo.
    if c.arquivo == PRODUCAO:
        s.add("coder")
    if c.modo in {FORBIDDEN_PATHS, DIFF_BUDGET}:
        s.add("coder")

    # Tester: R3 — re-autoria in-place APENAS na spec própria sem veredito
    # (posse via git do Pod; asserção falhando é veredito e fica de fora).
    if c.arquivo == SPEC_TESTER and c.modo == ZERO_VEREDITO:
        s.add("tester")

    # R5 (v4, ator único) — spec do CLIENTE na lista FAIL com sujeito no diff
    # acumulado E verde no base: o CODER resolve, em toda rodada, com as
    # asserções exatas no contexto. O 2º estágio (parque na reincidência) SAIU
    # em 2026-08-10 (`client-spec-never-parks-v1`): a política passou a
    # autorizá-lo a atualizar a spec, e a supervisão migrou para o diff da PR —
    # pedir clique no meio do laço para o que já é permitido era só atrito.
    # Vermelha no base (quem == "cliente") não entra: é herdada (R8).
    if (
        c.quem != "cliente"
        and c.arquivo == SPEC_CLIENTE
        and c.sujeito_no_diff
        and c.modo in {ASSERCAO, ZERO_VEREDITO}
    ):
        s.add("coder")

    # R8 — o vermelho que o item ENCONTROU: a suite já falhava no base_sha, então
    # não é reprovação dele. Não precisa de ator nem de humano: o gate classifica
    # NOT_OUR_FAILURE (nomes no detail, contagem no ledger) e o item segue.
    if c.quem == "cliente" and c.arquivo == SPEC_CLIENTE:
        s.add("baseline:not_our_failure")

    # R9 — exaustão reconhecida (não classificada): asserção em spec própria
    # com veredito, REPETIDA EM QUALQUER RODADA POSTERIOR (memória por spec,
    # não fingerprint consecutivo — rodadas heterogêneas no meio não apagam),
    # sem ator autorizado → parque para humano com dossiê, na primitiva da
    # porta 1.
    if c.arquivo == SPEC_TESTER and c.modo == ASSERCAO:
        # F3 (2026-08-10): as duas primeiras ordens de reescrita saem SOZINHAS
        # (`auto-reauthor-before-park-v1`) — é o efeito exato do clique em
        # Reauthor, que o humano dava sempre igual. O parque continua existindo
        # como último recurso, no terceiro impasse: sem ele o laço não converge.
        s.add("tester:auto_reauthor")
        s.add("humano:spec_conflict")

    # R10 — a pinça declarada pelo próprio ator: o duplo no-op contra spec
    # própria reprovada com veredito é reconhecimento de exaustão mais forte
    # que uma segunda rodada de L1. Mesma saída, mesmo dossiê, sem L1 extra.
    if c.arquivo == SPEC_TESTER and c.modo == ASSERCAO_CODER_SEM_JOGADA:
        s.add("tester:auto_reauthor")  # F3: mesma primitiva, mesmo orçamento de 2
        s.add("humano:spec_conflict")

    # R2: célula (tester quebrou spec do cliente) é estruturalmente impossível —
    # o rename guard desvia a escrita para um caminho -dse próprio. Modelada
    # fora da matriz (ver test_r2_torna_a_celula_impossivel).
    return s


VIVAS: list[Celula] = [
    # O laço saudável: quem quebra produção conserta produção.
    Celula("coder", PRODUCAO, COMPILE_PRODUCAO,
           nota="fix_context → Coder (medido: wi_1a5f9e3d corrigiu typecheck+build)"),
    Celula("coder", PRODUCAO, ASSERCAO,
           nota="spec do Tester reprovando código: o alvo do conserto é a produção"),
    # Porta 1 (rc.41 + v2 rc.42 + v3): spec do cliente invalidada pelo diff →
    # Coder primeiro, humano na reincidência.
    Celula("coder", SPEC_CLIENTE, ASSERCAO,
           nota="dois estágios (wi_c9c7b200): 1ª falha vira rodada de Coder com as "
                "asserções; a mesma spec de novo parqueia (wi_8edaef39, diff acumulado)"),
    Celula("coder", SPEC_CLIENTE, ZERO_VEREDITO,
           nota="carga da spec do cliente quebrada por mudança no sujeito → parque"),
    # Porta 5 (rc.42): instrumento próprio quebrado → o próprio Tester repara.
    Celula("tester", SPEC_TESTER, ZERO_VEREDITO,
           nota="@MockBean (wi_5620d2c1): testCompile sem veredito → repair in-place"),
    Celula("coder", SPEC_TESTER, ZERO_VEREDITO,
           nota="@ngx-translate herdado (wi_8edaef39): posse decide, não a autoria do defeito"),
    # Gates sobre o diff do Coder: o Coder é o ator (remove/encolhe).
    Celula("coder", PRODUCAO, FORBIDDEN_PATHS,
           nota="run 1: Dockerfile fora do plano — o Coder pode deletar o que criou"),
    Celula("coder", PRODUCAO, DIFF_BUDGET,
           nota="run 2: 451 linhas — o Coder pode encolher o próprio diff"),
    # Promovidas de beco por R8 (baseline check): o item não morre mais no teto
    # por um vermelho que o repositório já tinha.
    Celula("cliente", SPEC_CLIENTE, ASSERCAO, sujeito_no_diff=False,
           nota="baseline vermelha do repo: classificada NOT_OUR_FAILURE, o item segue"),
    Celula("cliente", SPEC_CLIENTE, ZERO_VEREDITO, sujeito_no_diff=False,
           nota="mesma baseline morrendo na carga: idem — comparada suite a suite"),
    # v3: herdada MESMO com o sujeito no diff acumulado — o vermelho já existia
    # no base, então não é conflito deste item nem ganha chance de Coder.
    Celula("cliente", SPEC_CLIENTE, ASSERCAO, sujeito_no_diff=True,
           nota="baseline vermelha cujo sujeito o diff tocou: segue NOT_OUR_FAILURE"),
    # Promovida de beco por R9: a exaustão em spec própria parqueia com dossiê
    # (specs, asserções, esperado vs recebido, diff) em vez de morrer no teto.
    Celula("tester", SPEC_TESTER, ASSERCAO,
           nota="pageSize (wi_5eecf486), 'warning' (wi_32eb136f) e o alternado "
                "test→lint+build→test (wi_c9c7b200, que escapou do gatilho consecutivo "
                "e morreu no teto): memória por spec parqueia na 2ª reprovação da "
                "mesma spec, e o humano decide com o dossiê — retry ou reauthor"),
    # Promovida por R10: o Coder honesto que se recusa a perseguir a asserção
    # (wi_0d95384f: build → test(badge) → no-op → no-op escalou SEM dossiê,
    # com a memória armada e sem o L1 re-rodar) agora parqueia pelo ramo do
    # no-op, com o dossiê completo do caminho via L1.
    Celula("tester", SPEC_TESTER, ASSERCAO_CODER_SEM_JOGADA,
           nota="a recusa dupla do Coder é a própria evidência de exaustão; "
                "parque imediato, sem pagar outra rodada de L1 (wi_0d95384f)"),
]

#: BECOS CONHECIDOS — vazio hoje: os três medidos foram promovidos (R8, R9).
#: O mecanismo fica: um beco novo entra aqui com xfail ESTRITO, e quem abrir
#: uma saída para ele é OBRIGADO pelo teste a promover a célula.
BECOS: list[Celula] = []


@pytest.mark.parametrize("celula", VIVAS, ids=lambda c: f"{c.quem}/{c.arquivo}/{c.modo}")
def test_toda_celula_viva_tem_saida(celula: Celula):
    quem_pode = saidas(celula)
    assert quem_pode, (
        f"célula ({celula.quem}, {celula.arquivo}, {celula.modo}) ficou sem ator autorizado "
        f"e sem escalada desenhada — {celula.nota}"
    )


def test_r2_torna_a_celula_impossivel():
    """(tester, spec_cliente, *) não é um beco — é INALCANÇÁVEL por construção:
    o rename guard (activities.py ~2329) desvia qualquer escrita do Tester sobre
    spec do cliente para um caminho -dse próprio. Se essa guarda cair, a célula
    nasce sem dono e este teste é o lembrete de modelá-la."""
    protegida = Celula("tester", SPEC_CLIENTE, ASSERCAO)
    assert "tester" not in saidas(protegida), "o Tester nunca é ator em spec do cliente"


def test_porta1_v4_o_coder_resolve_spec_de_cliente_sem_pedir_licenca():
    """v4 (2026-08-10): o conflito em spec do cliente tem UM ator — o Coder — e
    NENHUM parque. Este pin afirmava o contrário até hoje ("a reincidência
    parqueia"), e afirmava a política certa PARA A ÉPOCA: spec de cliente era
    intocável, então parar e perguntar era a única saída honesta.

    A política mudou (spec de cliente é editável, revisada no diff da PR) e o
    parque virou atrito puro: o humano via 3 falhas em 4.980 e clicava a mesma
    coisa toda vez. O que NÃO mudou está no pin abaixo — o instrumento do
    laço continua fora do alcance do Coder."""
    fresca = Celula("coder", SPEC_CLIENTE, ASSERCAO)
    assert saidas(fresca) == {"coder"}
    assert "humano:spec_conflict" not in saidas(fresca), (
        "spec de cliente não parqueia mais: a supervisão é o diff da PR"
    )
    herdada = Celula("cliente", SPEC_CLIENTE, ASSERCAO, sujeito_no_diff=True)
    assert saidas(herdada) == {"baseline:not_our_failure"}


def test_deferral_nao_e_saida_e_sim_encaminhamento():
    """R4: o deferral não autoriza ninguém — só move o veredito para o L1. A
    célula que era o beco 1 tem saída HOJE por causa do parque R9, não do
    deferral: a única saída é o humano, nunca um ator do laço."""
    celula = Celula("tester", SPEC_TESTER, ASSERCAO)
    assert "coder" not in saidas(celula), (
        "o instrumento do laço continua fora do alcance do Coder — é a régua "
        "com que ele é julgado"
    )
    # F3: a saída deixou de ser SÓ humana. As duas primeiras ordens de
    # reescrita saem sozinhas; o parque fica para o terceiro impasse.
    assert saidas(celula) == {"tester:auto_reauthor", "humano:spec_conflict"}
