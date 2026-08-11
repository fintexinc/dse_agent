"""O TAMANHO do diff deixou de reprovar. Decisão de operador, 2026-08-11.

O gate contava linhas fora de caminho de teste e reprovava acima de um teto.
O teto nunca foi dimensionado por ninguém: `PlanArtifact.diff_budget_lines`
tem default 400 no contrato, e o Planner **não é instruído sobre esse campo em
lugar nenhum** — então todo plano de todo item, de qualquer tamanho, saía com
400.

O que isso produz está medido (wi_4f680518, US$ 5,41, sem PR). Um pedido de
bootstrap de aplicação Angular:

    lint PASS  typecheck PASS  test PASS  build PASS  sast PASS  secret_scan PASS
    diff_budget FAIL — diff of 19605 lines outside test paths
                       exceeds diff_budget_lines=400

O `package-lock.json` sozinho passa de 15 mil linhas. E o laço não tinha como
sair: o L1 reprovava, o item comprava outro turno de Coder, e o Coder não pode
encolher o diff porque encolher significaria não fazer o que foi pedido. Cinco
rodadas idênticas até o operador parar.

O gate foi desenhado como "anti-sprawl do Coder", e para isso o número teria de
vir do trabalho. Vindo de uma constante, ele não mede sprawl: mede se a tarefa é
grande. Tarefa grande não é defeito.

**O que NÃO sai**, e é a razão de este arquivo existir em vez de um `git rm`:

  - a consistência `no_code_change` — plano que declara "não mexe em código" e
    entrega diff com arquivos continua reprovando. Nunca foi limite de linha,
    é contradição entre o que foi aprovado e o que foi feito;
  - `forbidden_paths`, que é um gate separado e continua duro;
  - o tamanho continua REPORTADO no finding. Some o veredito, não a medida —
    quem revisa a PR continua vendo quantas linhas vieram.
"""
from __future__ import annotations

from dse_contracts import GateStatus
from dse_contracts.plan_artifact import PlanArtifact

from dse_validation.l1.plan_compliance import diff_budget_finding


class _Diff:
    """O DiffSummary que o gate consome, no mínimo que ele lê."""

    def __init__(self, non_test: int, total: int, files: list[str]):
        self.non_test_lines_changed = non_test
        self.total_lines_changed = total
        self.files_changed = files
        self.base_sha = "0" * 40
        self.head_sha = "1" * 40


def _plan(**kw) -> PlanArtifact:
    base = dict(
        work_item_id="wi_4f680518",
        summary="bootstrap the application",
        steps=["scaffold"],
        expected_files=["package.json"],
        test_plan="jest",
        risk_class="medium",
    )
    base.update(kw)
    return PlanArtifact(**base)


def test_a_bootstrap_sized_diff_passes():
    """O caso literal do wi_4f680518: 19.605 linhas fora de teste."""
    finding = diff_budget_finding(
        _Diff(non_test=19605, total=19800, files=["package-lock.json", "src/main.ts"]),
        _plan(),
    )
    assert finding.passed, (
        f"um diff de 19.605 linhas reprovou: {finding.summary}. Nenhum turno de "
        "Coder encolhe um bootstrap — foi assim que o item queimou 5 rodadas e "
        "US$ 5,41 sem produzir PR"
    )
    assert finding.status != GateStatus.FAIL


def test_the_size_is_still_reported():
    """Some o veredito, não a medida. Um gate que passa em silêncio esconde de
    quem revisa a PR o fato de o diff ter 20 mil linhas."""
    finding = diff_budget_finding(
        _Diff(non_test=19605, total=19800, files=["package-lock.json"]), _plan()
    )
    assert "19605" in (finding.detail or "") or "19605" in (finding.summary or ""), (
        f"o tamanho sumiu do finding: {finding.detail!r}. Ele deixou de reprovar, "
        "não de ser informação"
    )


def test_a_plan_that_promised_no_code_change_still_fails():
    """PIN: isto NUNCA foi limite de linha. É contradição entre o que o humano
    aprovou e o que foi entregue, e continua reprovando — inclusive para um
    diff de uma linha, que nenhum teto jamais pegaria."""
    finding = diff_budget_finding(
        _Diff(non_test=1, total=1, files=["src/app.ts"]),
        _plan(no_code_change=True),
    )
    assert not finding.passed, (
        "um plano com no_code_change=true entregou arquivo alterado e passou — "
        "a remoção do teto levou junto uma checagem que não era de tamanho"
    )
    assert "no_code_change" in (finding.detail or "")


def test_no_line_ceiling_survives_anywhere():
    """PIN da decisão inteira: nenhum caminho, por env ou por plano, volta a
    transformar tamanho em reprovação. Uma variável de ambiente esquecida num
    values.yaml seria indistinguível do bug que acabamos de remover."""
    import inspect

    from dse_validation.l1 import plan_compliance

    fonte = inspect.getsource(plan_compliance)
    assert "DSE_L1_DIFF_BUDGET_LINES" not in fonte, (
        "o override por ambiente continua no código: enquanto ele existir, um "
        "deploy pode reintroduzir o teto sem passar por decisão nenhuma"
    )
    # 4 milhões de linhas, sem plano nenhum dizendo nada: continua passando.
    assert diff_budget_finding(_Diff(4_000_000, 4_000_000, ["x"]), _plan()).passed
