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

  - TERMINAÇÕES que o inglês não produz (-ção, -mente, -ável, -ado). Existem
    porque a lista de palavras tem recall ruim contra português sem acento, e
    exigir que alguém preveja o vocabulário não escala.

Medido contra o corpus real (os 1.727 literais dos 9 módulos varridos), e é
assim que ele deve continuar sendo calibrado: palavra nova entra depois de
rodar contra o corpus, nunca por intuição.
"""
from __future__ import annotations

import re
import unicodedata

#: Letras acentuadas que o inglês não usa em palavra nativa. `é` aparece em
#: empréstimos ("café", "résumé") — por isso ela sozinha não condena, entra
#: junto das outras evidências.
_PT_CHARS = re.compile(r"[ãõçáàâêíóôúÃÕÇÁÀÂÊÍÓÔÚ]")

#: Palavras funcionais do português que NÃO são palavras do inglês. Escolhidas
#: por serem impossíveis de aparecer por acaso num texto inglês correto.
_PT_WORDS = frozenset({
    "não", "nao", "você", "voce", "está", "são", "sao", "já",
    "que", "isso", "quando", "porque", "então", "entao", "também", "tambem",
    "arquivo", "arquivos", "erro", "erros", "falhou", "falha", "mensagem",
    "precisa", "precisam", "tarefa", "revisão", "revisao", "aprovado",
    "rejeitado", "enviado", "registrado", "escalado", "aguardando",
    "decidir", "consegui", "tente", "nenhum", "nenhuma",
    "plano", "veredito", "rodada", "conserta", "conserte", "encontrei",
    "invalido", "inválido", "limite", "tentativas", "atingido", "encerrada",
})
#: Palavras que existem nas DUAS línguas ficam FORA de `_PT_WORDS` de
#: propósito: "final", "total", "local", "base", "item", "status". E `com` foi
#: REMOVIDA depois de medir: ela casa em `https://github.com/` e em
#: `src/main/java/com/…`, dois literais que já existem no repositório — a um
#: passo de derrubar a build com "traduza isto" sobre uma URL.

#: Terminações que o inglês não produz. Existem porque a lista de palavras
#: sozinha tem recall ruim: "Repositorio invalido" e "Limite de tentativas
#: atingido" — português real, sem acento — passavam com zero evidências.
#: Sufixo pega a MORFOLOGIA em vez de exigir que alguém preveja o vocabulário.
_PT_SUFFIXES = re.compile(
    r"\b\w{3,}(?:ção|çao|cao|ções|coes|mente|ável|avel|ível|ivel|"
    r"ório|orio|ária|aria|ado|ados|ida|idas|indo|ando)\b",
    re.IGNORECASE,
)

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
    for m in _PT_SUFFIXES.finditer(text or ""):
        hits.append(f"terminação: {m.group(0).lower()!r}")
    # dedup preservando ordem, para a mensagem não repetir a mesma evidência
    seen: set[str] = set()
    unique = [h for h in hits if not (h in seen or seen.add(h))]
    return len(unique), unique
