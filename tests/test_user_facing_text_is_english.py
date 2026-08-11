"""Texto que sai do repositório é em INGLÊS.

O projeto trabalha em português — comentários, docstrings, mensagens de commit.
Isso é decisão e continua. O que não pode é português atravessar a fronteira:
o que o cliente lê no Slack, no corpo da PR, num comentário de issue.

Por que isto é um TESTE e não uma revisão: a fronteira é invisível no diff.
Ninguém escreve "vou mandar português para o cliente" — escreve uma mensagem
de erro nova, em português, num arquivo onde as outras 40 linhas de comentário
também são em português, e ela sai. Um teste é o único revisor que olha toda
mensagem, toda vez.

O que ele varre: os módulos que FALAM com o usuário, inteiros. Módulo inteiro e
não uma lista de símbolos, de propósito — a lista de símbolos envelhece calada
e a mensagem nova nasce fora dela. Docstring fica de fora (documentação
interna); o resto é candidato.

Se este teste falhar num texto que NÃO chega ao usuário (um `logger.warning`,
por exemplo), a resposta certa é discutir se aquele módulo devia mesmo estar na
lista — não afrouxar o detector.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from lang_guard import portuguese_score

_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Os módulos cujo texto ATRAVESSA a fronteira do repositório. Cada linha diz
#: por onde o texto sai — sem isso a lista vira inventário morto e ninguém sabe
#: por que um módulo está aqui.
_USER_FACING = {
    # Slack: blocos, acks in-place, mensagens ephemeral.
    "services/adapter-slack/adapter_slack/app.py": "Slack (ack, ephemeral)",
    "services/adapter-slack/adapter_slack/backend.py": "Slack (Block Kit)",
    # O texto de cada transição de status que vira mensagem na thread.
    "services/orchestrator/src/dse_orchestrator/local_activities.py": "Slack/GitHub (status)",
    # O aviso de veredito não-entregue, que EDITA a mensagem que o humano clicou.
    "services/ingest-gateway/ingest_gateway/dispatcher.py": "Slack (undeliverable)",
    # Título, corpo e comentário da PR no repositório do cliente.
    "services/validation/dse_validation/github/pr_finalizer.py": "GitHub (PR)",
    # A linha de preview no corpo da PR. Entrou nesta lista em 2026-08-11, no
    # mesmo commit em que o módulo passou a ESCREVER para humano: até então ele
    # só provisionava, e por isso não era varrido. A ausência dele aqui deixou
    # três frases em português chegarem ao corpo da PR de um cliente — o
    # detector as reprova (score 2 e 3), ele só nunca foi apontado para cá.
    "services/validation/dse_validation/preview/argocd.py": "GitHub (linha de preview na PR)",
    # Os outros canais de saída.
    "services/adapter-github/adapter_github/app.py": "GitHub (issue)",
    "services/adapter-jira/adapter_jira/app.py": "Jira",
    "services/adapter-teams/adapter_teams/app.py": "Teams",
    # O que o console mostra.
    "services/console-projector/console_projector/mappers.py": "console",
}

#: Duas evidências. Uma sozinha erra: "é" aparece em empréstimo inglês
#: ("café"), e "com"/"para" aparecem em identificador. Duas juntas, não.
_THRESHOLD = 2


def _user_facing_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    """Toda string literal do módulo que não seja docstring, com a linha."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]


@pytest.mark.parametrize("rel,surface", sorted(_USER_FACING.items()))
def test_no_portuguese_reaches_the_user(rel: str, surface: str):
    path = _ROOT / rel
    assert path.is_file(), f"{rel} sumiu — a lista de superfícies ficou para trás do código"

    offenders: list[str] = []
    for lineno, text in _user_facing_strings(path):
        score, evidence = portuguese_score(text)
        if score >= _THRESHOLD:
            offenders.append(f"  {rel}:{lineno} ({surface})\n    {text[:110]!r}\n    {evidence[:4]}")

    assert not offenders, (
        f"{len(offenders)} texto(s) em português saindo por «{surface}»:\n"
        + "\n".join(offenders)
        + "\n\nO cliente lê isto. Traduza — não afrouxe o detector."
    )


def test_the_detector_still_detects():
    """PIN: um teste de idioma que parou de detectar passa em silêncio para
    sempre, e é indistinguível de um repositório limpo. Esta é a única
    asserção aqui que falha se o DETECTOR quebrar em vez do código."""
    assert portuguese_score("Approved by <@U1> at 14:32 (UTC)")[0] == 0
    assert portuguese_score("Aprovado por <@U1> às 14:32 (UTC)")[0] >= _THRESHOLD
    assert portuguese_score("Voce nao tem permissao")[0] >= _THRESHOLD, (
        "sem acento também é português — metade do repositório escreve assim"
    )


def test_the_working_language_of_the_repository_is_untouched():
    """PIN de fronteira: docstring e comentário em português continuam
    permitidos, inclusive nos módulos varridos. Se este teste começar a falhar,
    alguém transformou uma decisão de produto (o cliente lê inglês) numa
    proibição de idioma no repositório inteiro — que é outra coisa."""
    path = _ROOT / "services/adapter-slack/adapter_slack/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = [
        ast.get_docstring(n, clean=False) or ""
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
    ]
    assert any(portuguese_score(d)[0] >= _THRESHOLD for d in docs), (
        "nenhuma docstring em português neste módulo — ou o repositório mudou "
        "de língua de trabalho, ou este teste deixou de provar o que diz"
    )
