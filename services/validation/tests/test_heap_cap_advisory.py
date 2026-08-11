"""Quando o comando do repositório estrangula o heap, isso tem que ser DITO.

Medido dentro do sandbox de produção, antes de apagá-lo (wi_530a1f56):

    NODE_OPTIONS que o sandbox define  -> heap máximo do V8: 9264 MB
    NODE_OPTIONS que o comando impõe   -> heap máximo do V8: 1072 MB

O `.dse/validation.json` do repositório define
`NODE_OPTIONS=--max-old-space-size=1024` no próprio comando, e isso vence o
env do container. O `ng lint` de um Angular real estoura 1 GB, o V8 aborta
(exit 134) e o gate morre — com 12 GiB disponíveis no cgroup.

O custo de não dizer: US$ 18,90 num item que passou duas rodadas mexendo no
`package.json` do cliente atrás de um teto que não estava lá. E a VPS foi
aumentada no mesmo dia, sem efeito nenhum sobre isso — porque o teto não é da
máquina.

Isto REPORTA, não corrige: o comando é do dono do repositório, e mexer nele é
decisão dele. É a mesma forma do aviso de teste excluído, que já provou o valor
em produção hoje ao nomear o `BmoFeeCalculatorBeApplicationTests`.
"""
from __future__ import annotations

from dse_validation.l1.quality_checks import heap_cap_advisory


def test_it_names_the_cap_and_what_the_sandbox_offered():
    cmd = ["sh", "-c", "NODE_OPTIONS=--max-old-space-size=1024 npx ng lint"]
    note = heap_cap_advisory(cmd, sandbox_node_options="--max-old-space-size=9216")

    assert note, "o teto do comando é menor que o do sandbox e nada foi dito"
    assert "1024" in note and "9216" in note, (
        "os DOIS números têm que aparecer: sozinho, nenhum deles mostra que há "
        f"um estrangulamento. Veio: {note!r}"
    )
    assert "exit 134" in note or "134" in note, (
        "o operador chega aqui vindo de um exit 134; o aviso tem que casar com "
        "o sintoma que ele viu"
    )


def test_no_cap_no_note():
    """PIN: aviso que aparece sempre vira cabeçalho, e cabeçalho não é lido."""
    assert heap_cap_advisory(["sh", "-c", "npx ng lint"], "--max-old-space-size=9216") == ""
    assert heap_cap_advisory([], "--max-old-space-size=9216") == ""
    assert heap_cap_advisory(None, None) == ""


def test_a_cap_at_or_above_what_we_offer_is_not_a_problem():
    """O repositório pode pedir MAIS que o sandbox — aí o limite do cgroup é
    que decide, e isso não é estrangulamento nosso a reportar."""
    cmd = ["sh", "-c", "NODE_OPTIONS=--max-old-space-size=16384 npm run build"]
    assert heap_cap_advisory(cmd, "--max-old-space-size=9216") == ""


def test_it_survives_a_sandbox_without_the_option():
    """Sem `NODE_OPTIONS` do nosso lado não há comparação a fazer — e um aviso
    chutado seria pior que nenhum."""
    cmd = ["sh", "-c", "NODE_OPTIONS=--max-old-space-size=1024 npx ng lint"]
    assert heap_cap_advisory(cmd, None) == ""
    assert heap_cap_advisory(cmd, "") == ""
