"""D1 — o exemplo de teste que o Tester recebe é escolhido por PROXIMIDADE DO
DIFF, não pela ordem do sistema de arquivos.

Medido em 2026-08-10: `_tester_repo_context` varre com `os.walk` e guarda o
PRIMEIRO arquivo de teste que encontra — na prática, o alfabeticamente
primeiro de um diretório qualquer. Num repo Java com dezenas de testes, o
Tester recebia como modelo um teste de um subsistema que a mudança nem tocou,
e escrevia contra APIs de mock/fixtures que não existem no subsistema alvo —
a origem provável da série de testes que não compilam (rc.69 os transformou em
parque com botões, mas parque não é a cura).

O exemplo certo é o teste do MESMO subsistema que o diff tocou: ele demonstra
as fixtures, os mocks e as importações que aquele código realmente precisa.
"""
from __future__ import annotations

from pathlib import Path

from sandbox_runtime.activities import _tester_repo_context


def _repo(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src/test/java/com/acme/admin").mkdir(parents=True)
    (ws / "src/test/java/com/acme/service").mkdir(parents=True)
    (ws / "src/main/java/com/acme/service").mkdir(parents=True)
    # alfabeticamente primeiro, e de um subsistema que o diff NÃO toca
    (ws / "src/test/java/com/acme/admin/AdminPanelTest.java").write_text(
        "class AdminPanelTest { /* mocks de UI, nada a ver com fees */ }\n"
    )
    (ws / "src/test/java/com/acme/service/AdvisorFeeCalculationServiceTest.java").write_text(
        "class AdvisorFeeCalculationServiceTest { /* fixtures de fee, o modelo certo */ }\n"
    )
    (ws / "src/main/java/com/acme/service/AdvisorFeeCalculationService.java").write_text(
        "class AdvisorFeeCalculationService {}\n"
    )
    return ws


def test_the_example_comes_from_the_subsystem_the_diff_touched(tmp_path):
    ws = _repo(tmp_path)
    _pkg, example, existing = _tester_repo_context(
        str(ws),
        diff_files=["src/main/java/com/acme/service/AdvisorFeeCalculationService.java"],
    )
    assert "AdvisorFeeCalculationServiceTest" in example, (
        "o exemplo tem que vir do subsistema QUE O DIFF TOCOU — o alfabético "
        "ensina fixtures de outro subsistema e produz teste que não compila"
    )
    assert "AdminPanelTest" not in example
    assert len(existing) == 2, "a lista de testes existentes não muda"


def test_without_a_diff_the_old_behaviour_is_preserved(tmp_path):
    """Sem diff (primeiro turno, chamada antiga) nada quebra: qualquer exemplo
    serve, e a assinatura continua compatível."""
    ws = _repo(tmp_path)
    _pkg, example, existing = _tester_repo_context(str(ws))
    assert example, "sem diff ainda há exemplo"
    assert len(existing) == 2
