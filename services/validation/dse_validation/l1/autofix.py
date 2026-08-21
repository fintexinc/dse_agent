"""O conserto determinístico que vem antes de gastar um turno de modelo.

Formatação é a classe de falha em que a ferramenta que ACUSA também sabe
CONSERTAR, e com 100% de acerto. Pedir a um LLM que reescreva imports até casar
com um formatador custou quatro turnos (~US$ 4, ~14 minutos) num item real sem
convergir — trabalho que `spotless:apply` faz em 7 segundos.

A plataforma não conhece NENHUM formatador. Ela conhece o conceito: existe um
comando que conserta o que este gate reprova, e o repositório o declara em
`commands.lint_fix`. Toda linguagem tem um formatador com modo de escrita
(`ruff format`, `prettier --write`, `gofmt -w`, `dotnet format`, `rubocop -a`,
`cargo fmt`), então a mesma chave serve para todas — é o mesmo desenho de
`preview.start`, `install`, `commands.test_subset` e `reports.junit`.

E não é a plataforma editando o código do cliente por conta própria: é o
formatador DO PRÓPRIO REPOSITÓRIO, declarado por ele, rodando sobre o diff que
o DSE acabou de escrever — e tudo aparece no diff da PR, que é a supervisão.
"""
from __future__ import annotations

from dataclasses import dataclass

from dse_validation.config import L1Config
from dse_validation.sandbox_exec import SandboxExecutor

#: Só a reprovação de LINT convida o conserto. Rodar um formatador porque o
#: `test` falhou seria editar o código do cliente sem relação com o veredito.
_TRIGGER = "lint"

#: Teto do conserto. Um formatador é segundos; minutos aqui significam que algo
#: está errado, e o laço não pode ficar refém disso — o turno de modelo, que é
#: o caminho de sempre, continua disponível.
_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class AutofixResult:
    """`ran` é "o comando existia e foi executado"; `changed` é "ele alterou
    arquivo". Os dois são necessários: um formatador que roda sem mudar nada
    diz que a reprovação NÃO era de formatação, e insistir viraria um laço
    infinito barato em vez de um caro."""

    ran: bool
    changed: bool
    detail: str = ""


def lint_autofix(
    executor: SandboxExecutor, cfg: L1Config, *, failed_checks: list[str]
) -> AutofixResult:
    """Roda o `commands.lint_fix` do repositório e diz se o diff mudou.

    Oportunista de ponta a ponta: sem comando declarado, sem reprovação de
    lint, ou com o comando quebrando, o resultado é `ran/changed` falso e o
    turno de modelo acontece como sempre. Este passo nunca é a razão de um item
    parar — ele só pode ser a razão de um item ficar mais barato."""
    if _TRIGGER not in failed_checks:
        return AutofixResult(ran=False, changed=False, detail="lint did not fail")
    if not cfg.lint_fix_cmd:
        return AutofixResult(ran=False, changed=False,
                             detail="the repository declares no commands.lint_fix")

    run = executor.run(list(cfg.lint_fix_cmd), timeout=_TIMEOUT_SECONDS)
    # O código de saída NÃO decide: vários formatadores saem != 0 quando
    # reformatam algo. Quem decide é o diff, que é o fato.
    diff = executor.run(["git", "diff", "--name-status"], timeout=60)
    changed = bool((diff.stdout or "").strip())
    detalhe = (
        f"{' '.join(cfg.lint_fix_cmd)} exited {run.returncode}; "
        + ("the working tree changed" if changed else "nothing changed")
    )
    return AutofixResult(ran=True, changed=changed, detail=detalhe)
