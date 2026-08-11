"""Detector de PORTUGUÊS em texto que chega ao usuário.

O projeto é escrito em português como língua de trabalho — comentários,
docstrings, mensagens de commit, nomes de teste. Isso é deliberado e fica.
O que NÃO pode é português vazar para fora do repositório: mensagem no Slack,
corpo de PR, comentário em issue, texto que um humano do cliente lê.

Este módulo é só o detector; QUEM é user-facing está declarado em
`test_user_facing_text_is_english.py`. A separação existe porque a lista de
superfícies muda com o produto e o detector não.

Como ele decide: duas evidências independentes, porque cada uma sozinha erra.
  - CARACTERES que o inglês não usa (ã, õ, ç, á, ê, …). Prova forte, mas some
    quando alguém escreve sem acento — e metade do repo escreve sem acento.
  - PALAVRAS funcionais do português que não são palavras inglesas ("não",
    "para", "você", "está"). Prova mais fraca por palavra, forte no conjunto.

Falsos positivos que já foram vistos e estão tratados:
  - `para` também é inglês em nada, mas aparece em identificadores e URLs →
    a busca é por palavra inteira, minúscula, fora de identificador.
  - Nomes próprios e siglas (Fintex, DSE, PR) não têm acento e não casam.
  - Trechos de código dentro de mensagem (ex.: `git push`) não casam.
"""
from __future__ import annotations

import ast
import re
import unicodedata

#: Letras acentuadas que o inglês não usa em palavra nativa. `é` aparece em
#: empréstimos ("café", "résumé") — por isso ela sozinha não condena, entra
#: junto das outras evidências.
_PT_CHARS = re.compile(r"[ãõçáàâêíóôúÃÕÇÁÀÂÊÍÓÔÚ]")

#: Palavras funcionais do português que NÃO são palavras do inglês. Escolhidas
#: por serem impossíveis de aparecer por acaso num texto inglês correto.
_PT_WORDS = frozenset({
    "não", "nao", "você", "voce", "está", "esta", "são", "sao", "já", "jah",
    "com", "uma", "que", "por", "para", "pelo", "pela", "isso", "este", "esta",
    "aqui", "quando", "porque", "então", "entao", "também", "tambem",
    "arquivo", "arquivos", "erro", "erros", "falhou", "falha", "mensagem",
    "precisa", "precisam", "tarefa", "revisão", "revisao", "aprovado",
    "rejeitado", "enviado", "registrado", "escalado", "aguardando",
    "permissão", "permissao", "decidir", "destino", "consegui", "tente",
    "novo", "nenhum", "nenhuma", "todos", "todas", "seu", "sua", "dele",
    "plano", "veredito", "rodada", "laço", "laco", "conserta", "conserte",
})

#: Palavras que existem nas DUAS línguas e por isso nunca condenam sozinhas.
#: Mantidas fora de `_PT_WORDS` de propósito: "final", "total", "local",
#: "base", "item", "status", "no", "a", "e", "de" (nomes de branch/campo).

_WORD = re.compile(r"[a-zà-öø-ÿ]+", re.IGNORECASE)


def portuguese_score(text: str) -> tuple[int, list[str]]:
    """Quantas evidências de português, e quais. 0 = limpo.

    Devolve as evidências para o teste poder MOSTRAR o motivo — uma falha que
    só diz "tem português" manda o próximo a caçar a agulha no palheiro."""
    hits: list[str] = []
    for m in _PT_CHARS.finditer(text or ""):
        # a palavra inteira em volta do caractere, para a evidência ser legível
        start = text.rfind(" ", 0, m.start()) + 1
        end = text.find(" ", m.end())
        word = text[start:end if end != -1 else len(text)].strip(".,:;!?()[]{}\"'`")
        hits.append(f"acento: {word!r}")
    for word in _WORD.findall(text or ""):
        low = unicodedata.normalize("NFC", word.lower())
        if low in _PT_WORDS:
            hits.append(f"palavra: {low!r}")
    # dedup preservando ordem, para a mensagem não repetir a mesma evidência
    seen: set[str] = set()
    unique = [h for h in hits if not (h in seen or seen.add(h))]
    return len(unique), unique


def string_literals_of(module_path: str, symbols: set[str]) -> list[tuple[str, str]]:
    """Todas as strings literais dentro dos SÍMBOLOS pedidos de um módulo.

    Por AST e não por regex: uma constante de mensagem pode ser um dict, uma
    f-string, uma concatenação implícita em várias linhas — e regex sobre isso
    erra nos dois sentidos. Devolve `(símbolo, literal)`.

    Docstrings ficam de FORA: elas são documentação interna, e o repositório
    escreve documentação em português por decisão."""
    src = open(module_path, encoding="utf-8").read()
    tree = ast.parse(src)
    out: list[tuple[str, str]] = []

    def literals(node: ast.AST) -> list[str]:
        found: list[str] = []
        docstrings = set()
        for sub in ast.walk(node):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                doc = ast.get_docstring(sub, clean=False)
                if doc:
                    docstrings.add(doc)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if sub.value not in docstrings:
                    found.append(sub.value)
        return found

    for node in tree.body:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and node.targets:
            t = node.targets[0]
            name = t.id if isinstance(t, ast.Name) else None
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        if name in symbols:
            out.extend((name, lit) for lit in literals(node))
    return out
