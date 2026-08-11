"""Alcançabilidade das fronteiras de posse (2026-08-07; reescrito em 2026-08-10).

Para cada célula (quem quebrou, em que arquivo a falha se manifesta, modo de
falha), tem que existir PELO MENOS UM ator autorizado a corrigir. Morrer no
teto de retentativas não é saída: é exaustão, e foi exatamente assim que
wi_5620d2c1 e wi_8edaef39 morreram antes das portas 1/5.

Isto NÃO importa os serviços — é um mapa executável, escrito à mão. Por isso
ele pode MENTIR e continuar verde: quem muda uma regra atualiza a célula, e é
esse acordo que dá valor ao arquivo.

**O que este arquivo perdeu em 2026-08-10, e por quê.** Ele já teve dez regras
(R1–R10) e três becos. Metade delas existia para responder uma pergunta só:
*de quem é este arquivo de teste?* — com oráculo de autoria por histórico git,
marcador `-dse` no nome, rename guard, revert pós-turno, e um PARQUE para
quando a resposta fosse "de ninguém que possa agir". Uma decisão de operador
removeu a pergunta: **o DSE altera qualquer teste, e a supervisão é o diff da
PR**. Sem a pergunta, R1 (revert de instrumento), R2 (posse do Tester), R9
(exaustão em spec própria) e R10 (pinça declarada) não descrevem mais nada.

As regras que sobraram, e onde vivem:
  R3 porta 5, zero-veredito       — activities.py:_zero_verdict_specs + reparo
                                    in-place: quando a spec que o TESTER acabou de
                                    escrever não CARREGA (import quebrado, erro de
                                    compilação), ele conserta antes de gastar uma
                                    rodada do Coder atrás de um teste que nunca rodou.
                                    Escopo = a lista de alvos do turno; asserção
                                    falhando é veredito e nunca entra aqui.
  R4 deferral                     — activities.py:_suite_verdict_deferred: suite
                                    própria falhando não é gate; o L1 julga.
  R5 detector de conflito         — workflows.py:preexisting_spec_conflicts: spec
                                    pré-existente FAIL com SUJEITO no diff acumulado.
                                    Não parqueia mais nada — hoje ele só produz a
                                    MIRA (caminhos + asserções) para o turno seguinte
                                    do Coder e a evidência no ledger.
  R6 forbidden_paths              — validation (plan_compliance): gate sobre o diff do
                                    Coder; o próprio Coder pode remover o que criou.
  R7 diff_budget                  — validation: idem — o Coder pode encolher o diff.
  R8 baseline check               — l1/quality_checks.py:_baseline_failing_suites +
                                    test_check(base_sha=): suite já vermelha no base
                                    vira NOT_OUR_FAILURE e o item SEGUE (promoveu os
                                    becos 2 e 3 deste mapa).

E os freios que encerram um item que não converge — teto de tentativas,
`coder_not_converging`, duplo no-op, gate de diff vazio, teto de gasto —
continuam todos, terminando em `escalated`. Eles não são "saída" no sentido
deste mapa (são exaustão), e é por isso que toda célula viva precisa de ator.
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
    """Os atores autorizados — deny-by-default, uma regra por bloco."""
    s: set[str] = set()

    # O CODER, em tudo que tem veredito. Esta é a mudança de 2026-08-10 e é o
    # motivo de este arquivo ter encolhido: antes a autorização dependia de
    # QUEM escreveu o arquivo, e cada resposta a essa pergunta precisava de um
    # oráculo (histórico git, marcador `-dse`), de uma exceção, e de um parque
    # para quando o oráculo dissesse "ninguém pode". Agora é uma linha: se
    # existe veredito, o Coder é ator.
    if c.modo in {COMPILE_PRODUCAO, ASSERCAO, ASSERCAO_CODER_SEM_JOGADA,
                  FORBIDDEN_PATHS, DIFF_BUDGET} and c.quem != "cliente":
        s.add("coder")
    # Zero-veredito em produção/spec de cliente também é dele: a carga morreu
    # por causa do diff, e o diff é o que ele controla.
    if c.modo == ZERO_VEREDITO and c.quem != "cliente":
        s.add("coder")

    # O TESTER, na porta 5 e só nela: a spec que ele acabou de escrever não
    # CARREGA (import quebrado, erro de compilação), então não há veredito a
    # proteger — é instrumento quebrado, e ele conserta in-place antes de
    # gastar uma rodada do Coder atrás de um teste que nunca rodou. O escopo é
    # a lista de alvos DO TURNO; não há mais pergunta ao git.
    if c.arquivo == SPEC_TESTER and c.modo == ZERO_VEREDITO:
        s.add("tester")

    # R8 — o vermelho que o item ENCONTROU: a suite já falhava no base_sha, então
    # não é reprovação dele. Não precisa de ator nem de humano: o gate classifica
    # NOT_OUR_FAILURE (nomes no detail, contagem no ledger) e o item segue.
    if c.quem == "cliente" and c.arquivo == SPEC_CLIENTE:
        s.add("baseline:not_our_failure")

    return s


VIVAS: list[Celula] = [
    # O laço saudável: quem quebra produção conserta produção.
    Celula("coder", PRODUCAO, COMPILE_PRODUCAO,
           nota="fix_context → Coder (medido: wi_1a5f9e3d corrigiu typecheck+build)"),
    Celula("coder", PRODUCAO, ASSERCAO,
           nota="spec reprovando código: o alvo do conserto é a produção"),
    # Spec de CLIENTE quebrada pelo diff: um ator, nenhum parque (2026-08-10).
    Celula("coder", SPEC_CLIENTE, ASSERCAO,
           nota="o Coder recebe os caminhos e as asserções e julga: atualizar a "
                "spec obsoleta ou consertar o código. A edição entra no diff da PR"),
    Celula("coder", SPEC_CLIENTE, ZERO_VEREDITO,
           nota="carga da spec do cliente quebrada por mudança no sujeito"),
    # Spec que o próprio laço escreveu: também do Coder desde 2026-08-10 — era
    # AQUI que morava o impasse que sustentava o reauthor e o parque.
    Celula("coder", SPEC_TESTER, ASSERCAO,
           nota="pageSize (wi_5eecf486), 'warning' (wi_32eb136f), o alternado do "
                "wi_c9c7b200: todos morriam sem ator porque a spec era intocável"),
    Celula("coder", SPEC_TESTER, ASSERCAO_CODER_SEM_JOGADA,
           nota="wi_0d95384f: o duplo no-op deixou de ser 'não tenho jogada' — ele "
                "tem; se ainda assim não agir, escala com a razão nomeada"),
    # Porta 5 (rc.42), preservada: instrumento próprio que nem carrega.
    Celula("tester", SPEC_TESTER, ZERO_VEREDITO,
           nota="@MockBean (wi_5620d2c1): testCompile sem veredito → repair in-place"),
    Celula("coder", SPEC_TESTER, ZERO_VEREDITO,
           nota="@ngx-translate herdado (wi_8edaef39): dois atores, e está certo — "
                "quem chegar primeiro resolve"),
    # O Tester sobrescrevendo spec do CLIENTE deixou de ser impossível: o rename
    # guard saiu junto (decisão de operador). A célula nasce viva e com ator.
    Celula("tester", SPEC_CLIENTE, ASSERCAO,
           nota="sem rename guard o Tester escreve onde pediu; se ele piorar uma "
                "spec do cliente, o Coder conserta e o diff da PR mostra"),
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
    Celula("cliente", SPEC_CLIENTE, ASSERCAO, sujeito_no_diff=True,
           nota="baseline vermelha cujo sujeito o diff tocou: segue NOT_OUR_FAILURE"),
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


def test_o_coder_e_ator_em_qualquer_teste():
    """A decisão de 2026-08-10, no seu formato mais curto.

    Este pin já afirmou o oposto duas vezes, e as duas estavam certas para a
    época: primeiro "o Coder NUNCA edita caminho de teste" (R1, revert total),
    depois "o instrumento do laço continua fora do alcance dele" (rc.76). Hoje
    não há posse de teste: qualquer spec com veredito tem o Coder como ator, e
    é isso que fez o parque e o reauthor perderem a razão de existir."""
    for arquivo in (SPEC_TESTER, SPEC_CLIENTE):
        celula = Celula("tester", arquivo, ASSERCAO)
        assert "coder" in saidas(celula), (
            f"o Coder tem que ser ator em {arquivo} — sem isso a célula volta a "
            "precisar de um parque"
        )


def test_nenhuma_celula_depende_de_humano_no_meio_do_laco():
    """A saída humana do MEIO do laço sumiu — sobrou a da BORDA (a PR).

    Enquanto o parque existiu, `humano:spec_conflict` era a saída de duas
    células, e ele não tinha prazo nenhum: o item ficava parado até alguém
    clicar. Hoje toda célula viva tem ator do laço; o humano decide onde
    sempre decidiu de fato — aprovando o plano e revisando a PR."""
    for celula in VIVAS:
        assert not any(x.startswith("humano:") for x in saidas(celula)), (
            f"{celula} ainda depende de decisão humana no meio do laço"
        )


def test_a_porta_5_continua_sendo_do_tester():
    """O que NÃO mudou: a spec que o Tester acabou de escrever e que nem carrega
    é dele para consertar, in-place, antes de gastar uma rodada do Coder atrás
    de um teste que nunca rodou. É a única autoria que sobrou no sistema, e ela
    não depende de perguntar ao git quem escreveu o quê."""
    celula = Celula("tester", SPEC_TESTER, ZERO_VEREDITO)
    assert "tester" in saidas(celula)
    celula_com_veredito = Celula("tester", SPEC_TESTER, ASSERCAO)
    assert "tester" not in saidas(celula_com_veredito), (
        "asserção falhando é VEREDITO: reescrevê-la seria o laço apagando a "
        "própria régua sem que ninguém visse. Quem mexe ali é o Coder, e a "
        "mudança aparece no diff da PR"
    )


def test_o_vermelho_herdado_nao_precisa_de_ator():
    herdada = Celula("cliente", SPEC_CLIENTE, ASSERCAO, sujeito_no_diff=True)
    assert saidas(herdada) == {"baseline:not_our_failure"}
