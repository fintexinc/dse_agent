"""Veredito `reauthor` (beco 1): o humano julgou que a asserção da spec
PRÓPRIA do Tester está errada e o código está certo — indecidível por dentro
do laço (portas 2/5) — e ORDENOU a reescrita. Aqui vive só a EXECUÇÃO da
ordem, e a guarda que nenhuma ordem atravessa: posse confirmada no git DO POD.
Um arquivo com qualquer sujeito humano na história é do cliente e é recusado
(R2 vale contra ordem também); história vazia idem — um arquivo que atravessou
um parque foi commitado por `tester(...)`, história nenhuma = não é ele.
Vermelho antes do fix.
"""
from __future__ import annotations

import shlex
import subprocess

from sandbox_runtime import activities

_DSE_SPEC = "src/app/components/dashboard-list/dashboard-list.component-dse.spec.ts"
_CLIENT_SPEC = "src/app/components/homepage/homepage.component.spec.ts"
_GHOST_SPEC = "src/app/components/ghost/ghost.component-dse.spec.ts"


class _FakePodSh:
    """Espelho mínimo do `_pod_sh`: responde `git log --format=%s -- <path>`
    com os sujeitos configurados por arquivo."""

    def __init__(self, subjects_by_path: dict[str, list[str]]):
        self._subjects = subjects_by_path
        self.scripts: list[str] = []

    def __call__(self, script: str, **_kw) -> subprocess.CompletedProcess:
        self.scripts.append(script)
        path = shlex.split(script.split(" -- ", 1)[1])[0] if " -- " in script else ""
        return subprocess.CompletedProcess(
            args=script, returncode=0,
            stdout="\n".join(self._subjects.get(path, [])), stderr="",
        )


def test_order_is_executed_only_on_dse_authored_files():
    pod = _FakePodSh({
        _DSE_SPEC: ["tester(wi_x): badge spec", "tester(wi_x): adjust"],
        _CLIENT_SPEC: ["Add homepage spec", "tester(wi_x): touched later"],
        _GHOST_SPEC: [],
    })
    owned, refused = activities._pod_reauthor_partition(
        pod, [_DSE_SPEC, _CLIENT_SPEC, _GHOST_SPEC]
    )
    assert owned == [_DSE_SPEC]
    assert refused == [_CLIENT_SPEC, _GHOST_SPEC], (
        "sujeito humano na história = cliente; história vazia = não atravessou "
        "parque nenhum — ambos fora do alcance de qualquer ordem"
    )


def test_every_dse_prefix_counts_as_ownership():
    subjects = [f"{p}wi_y): housekeeping" for p in activities._DSE_COMMIT_PREFIXES]
    pod = _FakePodSh({_DSE_SPEC: subjects})
    owned, refused = activities._pod_reauthor_partition(pod, [_DSE_SPEC])
    assert owned == [_DSE_SPEC] and refused == []


def test_the_order_filter_normalizes_model_path_prefixes():
    """wi_6f00bf0a: a ordem executou no vazio — o modelo devolveu caminhos que
    o filtro determinístico descartou EM SILÊNCIO. O filtro normaliza os
    prefixos que o modelo usa ('./x', '/workspace/x') para o caminho exato do
    workspace, e devolve também os caminhos VISTOS — a matéria-prima do evento
    tester_reauthor_missed quando nada sobrevive."""
    script = [
        {"tool": "write_file", "path": f"./{_DSE_SPEC}", "content": "rewritten"},
        {"tool": "write_file", "path": f"/workspace/{_CLIENT_SPEC}", "content": "nope"},
        {"tool": "write_file", "path": "src/other-dse.spec.ts", "content": "new path"},
        {"tool": "write_file", "path": _DSE_SPEC, "content": ""},
        {"tool": "read_file", "path": _DSE_SPEC},
    ]
    accepted, seen = activities._reauthor_script_writes(script, [_DSE_SPEC])
    assert accepted == [(_DSE_SPEC, "rewritten")], (
        "o './' do modelo é o MESMO caminho exato; conteúdo vazio não escreve"
    )
    assert seen == [
        f"./{_DSE_SPEC}", f"/workspace/{_CLIENT_SPEC}", "src/other-dse.spec.ts", _DSE_SPEC,
    ], "os caminhos vistos alimentam o tester_reauthor_missed"


def test_stem_equivalent_dse_variant_maps_to_the_ordered_path():
    """A deriva medida (wi_53c820f1, miss parcial da rc.50): ordem para
    X.spec.ts e o modelo escreve X-dse.spec.ts — stem-equivalente, caminho
    novo, recusado pelo filtro exato. Com temperature=0 a deriva se repete e a
    ordem nunca aterrissa. O filtro mapeia a variante para o caminho ORDENADO:
    in-place continua absoluto, nenhum caminho novo nasce, e a simetria cobre
    a direção inversa (ordem em -dse, script sem o sufixo)."""
    ordered = "src/app/components/homepage/components/report-status-badge/report-status-badge.component.spec.ts"
    drifted = "src/app/components/homepage/components/report-status-badge/report-status-badge.component-dse.spec.ts"
    accepted, seen = activities._reauthor_script_writes(
        [{"tool": "write_file", "path": drifted, "content": "rewritten"}], [ordered]
    )
    assert accepted == [(ordered, "rewritten")], "a variante -dse aterrissa no ordenado"
    assert seen == [drifted], "o caminho visto continua cru — evidência da deriva"

    accepted2, _seen2 = activities._reauthor_script_writes(
        [{"tool": "write_file", "path": ordered, "content": "x"}], [drifted]
    )
    assert accepted2 == [(drifted, "x")], "simetria: ordem -dse, script sem sufixo"

    # um stem DIFERENTE continua recusado — a equivalência é só o marcador -dse
    accepted3, _ = activities._reauthor_script_writes(
        [{"tool": "write_file", "path": "src/app/other.component-dse.spec.ts", "content": "y"}],
        [ordered],
    )
    assert accepted3 == []


def test_partial_miss_is_the_delta_not_the_absence():
    """rc.49 fechou o miss TOTAL; o wi_53c820f1 mediu o PARCIAL: ordenadas 2
    specs, reescrita 1, silêncio sobre a que faltou. O que o evento carrega é
    o delta ordered − reauthored — miss total vira caso particular."""
    assert activities._reauthor_missing(
        [_DSE_SPEC, _CLIENT_SPEC], [_CLIENT_SPEC]
    ) == [_DSE_SPEC]
    assert activities._reauthor_missing([_DSE_SPEC], [_DSE_SPEC]) == []
    assert activities._reauthor_missing([_DSE_SPEC, _CLIENT_SPEC], []) == [
        _DSE_SPEC, _CLIENT_SPEC,
    ]
