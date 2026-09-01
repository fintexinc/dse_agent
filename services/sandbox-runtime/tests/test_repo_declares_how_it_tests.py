"""A última escada do Tester: como se roda a suíte deste repositório.

O turno do Tester decidia isso por inferência, em quatro degraus:
`package.json` com `"test"` → `npm install` e `npm test --silent --
--coverage=false <arquivos>`; senão `pom.xml` com `./mvnw` → `./mvnw test`;
senão `pom.xml` → `mvn test`; senão `python3 -m pytest -q`. Go, Ruby, .NET,
Rust, PHP e Elixir caem TODOS no último degrau, e um `pytest` que não acha teste
sai != 0 — que o turno reporta como "os testes que você escreveu falham". Foi
exatamente assim que os itens de Java morreram antes do degrau do Maven existir.

Cada linguagem nova custa um degrau. A escada não escala; a declaração escala.

Duas chaves fecham isso, e nenhuma delas é adivinhada:

  - `install` (topo do manifesto): o passo de dependência, um por repositório.
    O preview usa a MESMA chave — `preview.install` da rc.105 é dobrada aqui,
    porque dois lugares para "instale as dependências" é a complexidade que o
    operador mandou remover. Preparo específico do preview cabe em `prepare`
    (topo), que o Pod do preview roda antes do install.
  - `commands.test_subset`: a suíte restrita aos arquivos que o Tester acabou
    de escrever. É um comando SEPARADO do `commands.test` de propósito: o
    `--coverage=false` não é um detalhe de sintaxe, é um fato do jest daquele
    repositório (`collectCoverage: true` com piso global de 80% reprova
    qualquer subconjunto — medido em 9,83%). Esse fato pertence ao manifesto do
    repositório, não ao código da plataforma. Sem `test_subset` declarado o
    turno roda o `commands.test` inteiro: mais lento, correto em toda
    linguagem, e nunca uma resposta inventada.
"""
from __future__ import annotations

import json
import subprocess

from dse_contracts import RunTesterTurnInput
from sandbox_runtime import activities

_BASE = {
    "version": 1,
    "commands": {"test": ["sh", "-c", "npx jest --ci"]},
}


def _done(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _cluster(manifest, seen):
    def fake_run(argv, **kwargs):
        seen.append(argv)
        joined = " ".join(argv)
        if "head -c" in joined:
            if "find ." in joined:
                return _done(argv, 0, stdout="./src/app.spec.ts\n")
            if "cat package.json" in joined:
                return _done(argv, 0, stdout='{"name":"fe","scripts":{"test":"jest"}}')
            return _done(argv, 0, stdout="")
        if ".dse/validation.json" in joined:
            if manifest is None:
                return _done(argv, 1, stdout="")
            return _done(argv, 0, stdout=json.dumps(manifest))
        if "--grep='^tester('" in joined:
            return _done(argv, 0, stdout="tests/app-dse.spec.ts\n")
        if "git log --format=%s" in joined:
            return _done(argv, 0, stdout="tester(wi): authored\n")
        return _done(argv, 0, stdout="Tests: 1 passed, 1 total\n")

    return fake_run


def _run(monkeypatch, manifest):
    seen: list = []
    monkeypatch.setattr(subprocess, "run", _cluster(manifest, seen))
    monkeypatch.setattr(activities, "audit_emit", lambda **kw: None)
    activities._tester_pod_sync(
        RunTesterTurnInput(work_item_id="wi-td", tenant_id="t", instruction="cover"),
        "dse-sbx-wi-td", None, "vk", False,
    )
    return [" ".join(a) for a in seen]


def _suite_call(calls) -> str:
    """A chamada que rodou a suíte — a que carrega o clock do `timeout` e o
    comando de teste, nunca a do typecheck."""
    hits = [c for c in calls if "timeout -k 10" in c and "tsc" not in c]
    assert hits, f"nenhuma chamada rodou a suíte; vistas={calls}"
    return hits[-1]


def test_the_subset_command_comes_from_the_repo_not_from_a_ladder(monkeypatch):
    manifest = dict(_BASE)
    manifest["commands"] = dict(_BASE["commands"],
                                test_subset=["npx", "jest", "--ci", "--coverage=false"])
    calls = _run(monkeypatch, manifest)
    suite = _suite_call(calls)

    assert "npx jest --ci --coverage=false" in suite
    assert "tests/app-dse.spec.ts" in suite, "o subset roda nos arquivos que o turno escreveu"
    assert "npm test --silent" not in suite, "a escada não decide mais nada"
    assert "python3 -m pytest" not in suite


def test_the_platform_never_invents_a_coverage_flag(monkeypatch):
    """`--coverage=false` é fato do jest daquele repo, e some quando o repo não
    o declara. Plataforma que injeta flag de ferramenta é plataforma que só
    fala uma linguagem."""
    manifest = dict(_BASE)
    manifest["commands"] = dict(_BASE["commands"], test_subset=["go", "test"])
    suite = _suite_call(_run(monkeypatch, manifest))

    assert "go test" in suite
    assert "coverage" not in suite


def test_without_a_subset_the_whole_declared_suite_runs(monkeypatch):
    """Ausência declarada não vira adivinhação: roda o `commands.test` inteiro,
    sem anexar arquivo nenhum (nem todo runner aceita path no fim do argv —
    mvn, dotnet e cargo não aceitam)."""
    suite = _suite_call(_run(monkeypatch, _BASE))

    assert "npx jest --ci" in suite
    assert "tests/app-dse.spec.ts" not in suite
    assert "npm test --silent" not in suite


def test_the_declared_install_replaces_the_npm_guess(monkeypatch):
    manifest = dict(_BASE, install=["pnpm", "install", "--frozen-lockfile"])
    calls = _run(monkeypatch, manifest)
    joined = "\n".join(calls)

    assert "pnpm install --frozen-lockfile" in joined
    assert "npm install --no-audit" not in joined, "a plataforma não escolhe o gerenciador"


def test_the_install_runs_once_even_though_two_steps_need_it(monkeypatch):
    """Typecheck e suíte precisam das dependências, e são dois execs. Instalar
    duas vezes é minutos jogados fora em toda rodada — o marcador no Pod é o
    que faz a segunda chamada ser um no-op."""
    manifest = dict(_BASE, install=["pnpm", "install"])
    manifest["commands"] = dict(_BASE["commands"],
                                typecheck=["sh", "-c", "npx tsc --noEmit"])
    calls = _run(monkeypatch, manifest)

    com_install = [c for c in calls if "pnpm install" in c]
    assert len(com_install) == 2, "os dois passos pedem a instalação"
    assert all("dse-install-done" in c for c in com_install), (
        "sem marcador, a segunda instalação roda de verdade"
    )
