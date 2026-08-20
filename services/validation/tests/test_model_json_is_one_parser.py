"""Um parser de resposta de modelo, com o retry que a rc.102 já provou.

Auditoria de 2026-08-20: existem SEIS cópias de
`json.JSONDecoder().raw_decode(...)` para ler resposta de modelo, cada uma com
uma política diferente de fence, de retry e de custo. Uma delas já mordeu — no
Tester (rc.102), quando a resposta veio truncada no teto de tokens e o item
morreu TERMINAL com o turno de Coder já pago.

O irmão pior é o L2: `max_tokens=1500` contra um prompt que carrega até 20.000
caracteres de diff, `temperature=0` (determinístico — a mesma entrada produz a
mesma saída truncada) e a activity roda sob `maximum_attempts=0`, cujo conjunto
de erros não-retentáveis não inclui erro de parse. Resultado: retenta até o
`schedule_to_close` de 7200s, e o custo de cada tentativa fica FORA do
orçamento do item, porque `_consume_cost` só roda no caminho de sucesso.

Este arquivo pina o helper único: strip de fence, `raw_decode` (o modelo às
vezes continua escrevendo depois do JSON), UM retry com o erro na cara e a
ordem de encolher, custo somado em TODAS as tentativas, e um erro final que
diz para não retentar.
"""
from __future__ import annotations

try:  # o vermelho: o helper ainda não existe
    from dse_validation.model_json import ModelJsonError, complete_json, parse_model_json
except ImportError:  # pragma: no cover
    ModelJsonError = complete_json = parse_model_json = None  # type: ignore[assignment]


def _existe():
    assert parse_model_json is not None, (
        "dse_validation.model_json não existe — seis cópias de raw_decode "
        "seguem com políticas divergentes de retry e de custo"
    )


# ---------------------------------------------------------------------------
# O parse
# ---------------------------------------------------------------------------

def test_it_strips_the_fence_the_models_keep_adding():
    _existe()
    payload, motivo = parse_model_json('```json\n{"passed": true}\n```')
    assert payload == {"passed": True} and motivo == ""


def test_it_ignores_what_the_model_writes_after_the_json():
    """Cicatriz do shadow-run: 'Extra data'. `raw_decode` pega o primeiro
    objeto e ignora o resto."""
    _existe()
    payload, _ = parse_model_json('{"passed": false}\n\nEspero ter ajudado!')
    assert payload == {"passed": False}


def test_a_truncated_response_reports_why():
    _existe()
    payload, motivo = parse_model_json('{"objections": ["a b c')
    assert payload is None and motivo


# ---------------------------------------------------------------------------
# O retry — e o custo de TODAS as tentativas
# ---------------------------------------------------------------------------

def test_a_truncated_first_answer_gets_one_retry_with_the_error_in_its_face():
    _existe()
    prompts: list[str] = []
    respostas = ['{"passed": true, "objections": ["a', '{"passed": true, "objections": []}']

    def completa(prompt: str):
        prompts.append(prompt)
        return respostas.pop(0), 0.01

    payload, custo = complete_json(completa, "julgue este diff")
    assert payload == {"passed": True, "objections": []}
    assert len(prompts) == 2, "o retry é UM — nem zero, nem laço"
    assert "smaller" in prompts[1].lower() or "shorter" in prompts[1].lower(), (
        "a segunda chamada precisa mandar ENCOLHER — a causa é o teto de tokens"
    )
    assert abs(custo - 0.02) < 1e-9, "as duas chamadas foram faturadas; o custo soma"


def test_two_bad_answers_raise_something_that_says_do_not_retry():
    """Sob `maximum_attempts=0`, um erro comum retentaria até o relógio de
    7200s — com temperature=0, produzindo exatamente a mesma saída."""
    _existe()
    chamadas = []

    def completa(prompt: str):
        chamadas.append(prompt)
        return "não sou json", 0.01

    try:
        complete_json(completa, "julgue")
    except ModelJsonError as exc:
        assert len(chamadas) == 2
        assert exc.cost_usd is not None and abs(exc.cost_usd - 0.02) < 1e-9, (
            "o custo das tentativas que aconteceram precisa viajar no erro — "
            "senão ele nunca entra no orçamento do item"
        )
        assert getattr(exc, "non_retryable", False) is True
    else:
        raise AssertionError("duas respostas imprestáveis não levantaram")


def test_a_good_first_answer_never_pays_for_a_second():
    _existe()
    chamadas = []

    def completa(prompt: str):
        chamadas.append(prompt)
        return '{"passed": true}', 0.01

    payload, custo = complete_json(completa, "julgue")
    assert payload == {"passed": True} and len(chamadas) == 1
    assert abs(custo - 0.01) < 1e-9
