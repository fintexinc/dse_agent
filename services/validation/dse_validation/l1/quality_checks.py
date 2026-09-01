"""WSE-E1-T1 — deterministic in-sandbox gates: lint, typecheck, test, build.

Each check runs the configured command (dse_validation.config.L1Config) through a
`SandboxExecutor` (inside the real sandbox once WS-C ships the runtime; through
`LocalFakeSandbox` in test/dev) and parses the result in a STRUCTURED way —
never just the raw exit code — because the exit code alone says neither "how
many problems" nor gives readable evidence for `L1Finding.detail`
(P8: evidence over assertion).
"""
from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from dse_contracts import GateStatus, L1Finding

from dse_validation.config import L1Config
from dse_validation.sandbox_exec import ExecResult, SandboxExecutor

_MAX_DETAIL_LINES = 40

#: How many of the lines a gate ACTUALLY COUNTED are reproduced in `detail`.
#: These are the evidence for the verdict, so they get their own budget ahead of
#: the raw tail rather than competing with it.
_MAX_ATTRIBUTED_LINES = 60


def _tail(text: str, max_lines: int = _MAX_DETAIL_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    omitted = len(lines) - max_lines
    return "\n".join(lines[-max_lines:]) + f"\n... ({omitted} earlier line(s) omitted)"


def _exec_facts(result: ExecResult) -> str:
    """What RAN, with what exit, and how much each stream wrote.

    rc.130. An ERROR finding used to carry only a headline plus the tail of
    the output — and when the tail was empty, "the tool printed nothing" and
    "the output was lost" (a dead `kubectl exec` answers rc −1/127 with the
    kubectl's own stderr, not the gate's) read exactly the same. The `lint
    exit=2` ghost on the revalidation path cost an hour of forensics for want
    of these five facts. They go where `detail` already goes —
    `validation_runs` — never the append-only ledger."""
    argv = " ".join(str(a) for a in (result.argv or []))
    facts = (
        f"ran: {argv} | exit={result.returncode} | "
        f"stdout={len(result.stdout or '')} bytes, stderr={len(result.stderr or '')} bytes"
    )
    if getattr(result, "timed_out", False):
        facts += " | timed out"
    return facts


def _output(result: ExecResult) -> str:
    """Both streams, because the diagnosis is rarely on the one `or` picks.

    This was `result.stdout or result.stderr`, which discards stderr entirely
    whenever stdout is non-empty — and for a Node toolchain stdout is NEVER
    empty. Measured on the Angular testbed for one `npx jest --ci` run:

        stdout  24,610 lines  — captured console.* from tests, coverage table
        stderr   7,074 lines  — the PASS/FAIL headers, the `-` failure blocks,
                                and the "Test Suites: 2 failed, 275 passed" line

    So every `detail` ever written for that gate was 40 lines of the coverage
    table, and the failure blocks naming the broken suite were thrown away at
    write time. Raising `_MAX_DETAIL_LINES` could not have fixed it at any
    value: the answer was never in the stream being tailed. The same `or` sent
    `build`'s detail the `ng build` bundle-size table while the compiler error
    went to stderr.
    """
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def _detail_with(summary: str, attributed: list[str], result: ExecResult) -> str:
    """Lead with the lines the gate counted; keep the raw tail behind them.

    `detail` used to be `summary + _tail(output)`, so the only evidence anyone
    received was the END of the tool's output. On the Angular testbed that meant
    an operator reading a `typecheck` failure saw the alphabetical tail of a
    262-error dump — pre-existing errors in files the change never opened — with
    24,569 lines omitted, while the three errors that actually failed the gate
    sat in the omitted head.

    That is not a cosmetic problem. `workflows.py::_l1_failure_context` feeds
    this exact string to the NEXT Coder turn. Handed other people's errors, the
    Coder produced a byte-identical diff across four paid rounds — a measured
    delta of zero. The gate has to show its own evidence for the fix loop to be
    a loop at all.
    """
    parts = [summary]
    # The execution facts, whenever the tool did not simply succeed (rc.130):
    # what ran, the exit code, bytes per stream. `test` and `build` build their
    # detail here, so this is where their ERROR/FAIL findings get the header
    # the other gates prepend by hand.
    if result.returncode != 0 or getattr(result, "timed_out", False):
        parts.append(_exec_facts(result))
    if attributed:
        shown = attributed[:_MAX_ATTRIBUTED_LINES]
        parts.append(f"--- the {len(attributed)} line(s) this gate counted ---")
        parts.extend(shown)
        if len(attributed) > len(shown):
            parts.append(f"... ({len(attributed) - len(shown)} more not shown)")
        parts.append("--- raw output (tail) ---")
    parts.append(_tail(_output(result)))
    return "\n".join(parts)


def _run(executor: SandboxExecutor, argv: list[str], timeout: int) -> ExecResult:
    return executor.run(argv, timeout=timeout)


#: 128 + signal. 137 = SIGKILL (the cgroup's OOM killer inside a container),
#: 139 = SIGSEGV, 143 = SIGTERM.
#:
#: 134 = SIGABRT, and it is the one that actually showed up: V8 does not get
#: killed when it exhausts its heap, it prints "FATAL ERROR: ... JavaScript
#: heap out of memory" and calls abort(). Observed on the Angular testbed —
#: `ng lint` died with exit=134 after printing nothing but `Linting "..."`, and
#: the first version of this classifier missed it, so the gate still read as a
#: verdict on the customer's code. The marker text was on stderr and went with
#: the process, which is why the return code has to carry the diagnosis.
_KILLED_RETURNCODES = frozenset({134, 137, 139, 143})

#: What a Node toolchain prints on its way out of memory. `ng lint` and
#: `ng build` on a real Angular app are the two commands that reach it.
_OOM_MARKERS = (
    "javascript heap out of memory",
    "allocation failure",
    "fatal error: reached heap limit",
    "killed",
    "cannot allocate memory",
    "enomem",
)


#: Os DOIS formatos do ruff, e a razão de o segundo existir aqui. O `concise`
#: (`path:line:col: CODE msg`) era o único que este gate conhecia — e o ruff
#: instalado neste próprio repositório imprime, por padrão, o formato `full`:
#:
#:     F401 [*] `os` imported but unused
#:      --> bad.py:1:8
#:       |
#:     1 | import os
#:
#: Descoberto em 2026-08-20 pela própria mudança que fez o gate parar de dizer
#: PASS sobre saída que não entende: sem ler a seta, TODO repositório Python
#: com ruff moderno passaria a escalar por "não consegui ler". A lição não é
#: "acrescente dialetos" — é que um parser cego devolve `ERROR`, e um ERROR
#: honesto expõe o dialeto que falta em vez de escondê-lo num verde.
_LINT_CONCISE_RE = re.compile(r"^\S+:\d+:\d+:\s")
_LINT_ARROW_RE = re.compile(r"^\s*-->\s+(?P<loc>\S+:\d+:\d+)\s*$")

#: spotless-maven: a saída é uma LISTA DE ARQUIVOS violados, cada um seguido do
#: diff que o consertaria. Nenhuma linha é `path:line:col` — o dialeto inteiro
#: era invisível para este parser, e a primeira execução real do spotless num
#: repo do cliente (wi_6d1e0f5fc7e, 16,46s depois de três releases consertando
#: a rede) escalou como "não consegui ler" um veredito de formatação legítimo.
#: A linha de arquivo é a única `[ERROR] <token-único>` cujo token é um caminho
#: nu: tem `/`, termina em extensão, sem `·` (o marcador de espaço que o diff
#: usa) — o corpo do diff começa em `@@`/`+`/`-`/`·` ou termina em `;`.
_SPOTLESS_MARKERS = ("had format violations", "spotless:apply")
_SPOTLESS_FILE_RE = re.compile(
    r"^\[ERROR\]\s+(?P<path>[^\s@+\-·][^\s]*\.[A-Za-z0-9]{1,10})\s*$"
)


#: O `stylish` do ESLint: o CAMINHO numa linha própria, os problemas
#: indentados abaixo dela. É o formatter default do ecossistema JS/TS inteiro
#: (o `unix`, que casaria com o parser canônico, saiu do core no ESLint 9 e
#: exigiria dependência nova — proibido pela _SPEC do manifesto).
_ESLINT_FILE_RE = re.compile(r"^(?P<path>[./]?[\w./-]+\.[jt]sx?)\s*$")
_ESLINT_ISSUE_RE = re.compile(
    r"^\s+(?P<line>\d+):(?P<col>\d+)\s+(?P<sev>error|warning)\s+(?P<msg>.+?)\s*$"
)


def _eslint_violations(text: str, changed_files: set[str] | None) -> list[str]:
    """Linhas canônicas `path:line:col: msg` sintetizadas do stylish do ESLint.

    Só `error` vira issue, e isso é FIDELIDADE ao repositório: o ESLint sai 0
    quando só há warning, e um repo maduro normaliza dívida ali (236 warnings
    no `apps/api` do glide-path). Contá-los faria o item pagar por debt de
    qualquer arquivo que encostasse — o incidente que `_only_in_changed_files`
    existe para impedir, entrando pela outra porta.

    O caminho impresso é ABSOLUTO no pod (`/workspace/apps/...`) e o diff é
    relativo ao repositório: resolve-se por sufixo contra `changed_files`, o
    mesmo que o spotless precisou fazer."""
    out: list[str] = []
    atual: str | None = None
    for ln in text.splitlines():
        m = _ESLINT_FILE_RE.match(ln)
        if m:
            atual = m.group("path")
            # O pod sempre monta o clone em /workspace: o prefixo é NOSSO, não
            # do repositório, e sem removê-lo nenhum caminho casaria com o diff.
            if atual.startswith("/workspace/"):
                atual = atual[len("/workspace/"):]
            atual = atual.lstrip("./")
            if changed_files is not None and atual not in changed_files:
                candidatos = sorted(f for f in changed_files if atual.endswith("/" + f))
                if candidatos:
                    atual = candidatos[0]
            continue
        if atual is None:
            continue
        mi = _ESLINT_ISSUE_RE.match(ln)
        if mi and mi.group("sev") == "error":
            out.append(f"{atual}:{mi.group('line')}:{mi.group('col')}: {mi.group('msg')}")
    return out


def _spotless_violations(text: str, changed_files: set[str] | None) -> list[str]:
    """Linhas canônicas `path:1:1: ...` sintetizadas do relatório do spotless.

    Duas decisões que não são detalhe:

    - O caminho impresso é relativo ao MÓDULO Maven (`src/test/...`); o diff do
      item é relativo ao repositório (`rest-adapter/src/test/...`). Sem
      resolver por sufixo contra `changed_files`, o casamento exato do escopo
      descartaria a violação NOSSA como "de outro arquivo" e o gate passaria
      por cima do veredito que acabou de aprender a ler.
    - A linha sintetizada carrega o caminho (do repositório, quando resolve):
      os nomes vêm ANTES dos diffs na saída e o detail guarda o `_tail` — numa
      saída longa o tail corta justamente os nomes."""
    if not any(m in text for m in _SPOTLESS_MARKERS):
        return []
    out: list[str] = []
    vistos: set[str] = set()
    for ln in text.splitlines():
        m = _SPOTLESS_FILE_RE.match(ln)
        if not m:
            continue
        path = m.group("path")
        if "/" not in path or "·" in path:
            continue
        resolvido = path
        if changed_files is not None and path not in changed_files:
            candidatos = sorted(f for f in changed_files if f.endswith("/" + path))
            if candidatos:
                resolvido = candidatos[0]
        if resolvido in vistos:
            continue
        vistos.add(resolvido)
        out.append(
            f"{resolvido}:1:1: SPOTLESS format violation — the file does not "
            "match the repository's declared format (run spotless:apply, or "
            "apply the diff shown below)"
        )
    return out


def _lint_diagnostics(text: str) -> list[str]:
    """Uma linha por diagnóstico, normalizada para começar em `path:line:col`.

    A normalização não é cosmética: `_only_in_changed_files` compara o caminho
    do começo da linha com o diff, e a forma de seta começa em `-->`."""
    out: list[str] = []
    for ln in text.splitlines():
        if _LINT_CONCISE_RE.match(ln):
            out.append(ln)
            continue
        m = _LINT_ARROW_RE.match(ln)
        if m:
            out.append(f"{m.group('loc')}: {ln.strip()}")
    return out


def _infra_failure(result: ExecResult) -> str | None:
    """Names an INFRASTRUCTURE failure, or None if the tool merely disagreed
    with the code.

    Without this, a gate killed by the cgroup's OOM killer is scored
    `GateStatus.FAIL` — a verdict on the customer's code — because the only
    thing distinguishing it from real lint errors is a return code nobody
    looked at. The workflow then spends a paid Coder turn "fixing" a lint run
    that never produced a finding, three times, and the work item ends `failed`
    with a reason that blames the diff.

    The sandbox runs `ng lint` and `ng build` under a 1536Mi limit while V8
    sizes its heap from the NODE's memory, so this is not hypothetical: it is
    the same exhaustion that already killed this repository's checkpoint
    commits when husky ran the linter on them.

    The Tester path has had this distinction for a while
    (`_tester_infra_outcome`); L1 had none."""
    if result.returncode in _KILLED_RETURNCODES:
        return f"the process was killed (exit={result.returncode})"
    blob = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    for marker in _OOM_MARKERS:
        if marker in blob:
            return "the process ran out of memory"
    return None


#: The two diagnostic shapes these gates parse:
#:   ruff/eslint  src/a.py:12:5: F401 msg
#:   tsc          src/a.ts(690,48): error TS2345: msg
_DIAGNOSTIC_PATH_RE = re.compile(r"^\s*(?P<path>[^\s:(]+)\s*[(:]")


def _path_of(line: str) -> str:
    """The file a diagnostic line points at, or "" when it points at none."""
    m = _DIAGNOSTIC_PATH_RE.match(line)
    return m.group("path").lstrip("./") if m else ""


def _only_in_changed_files(lines: list[str], changed_files: set[str] | None) -> list[str]:
    """Keep the diagnostics that land in files THIS CHANGE touched.

    The gate exists to judge the work item's diff, not the repository's history.
    Measured on the Angular testbed: `tsc --noEmit` reported 262 errors, every
    one of them in a `.spec.ts` the DSE never opened, against a change that
    added a single CONTRIBUTING.md. A markdown file cannot introduce TS2345 —
    but the gate failed the work item for them, the fix loop sent a paid Coder
    turn to repair someone else's specs, and no number of rounds could ever
    make a repository with pre-existing debt pass.

    `changed_files` of None means "no diff available" and everything counts:
    losing a real finding is worse than reporting one that is not ours."""
    if changed_files is None:
        return lines
    return [ln for ln in lines if _path_of(ln) in changed_files]


def _gate_is_unreachable(check: str, changed_files: set[str] | None) -> str | None:
    """Why this gate needs no run, or None if it does.

    The predicate itself lives in `dse_contracts.paths` because the workflow
    needs the SAME answer to decide whether the Tester has anything to test —
    and it turned out to be load-bearing that they agree. The Tester used to
    run unconditionally, author a spec, and have that spec committed into the
    diff, which made the change non-documentation-only and re-armed all four
    gates. This skip could therefore never fire while the Tester ran; the two
    decisions have to be taken from one list."""
    from dse_contracts.paths import is_documentation_only

    if not is_documentation_only(changed_files):
        return None
    return f"the change touches documentation only ({len(changed_files)} file(s))"


def _not_applicable(check: str, why: str) -> L1Finding:
    """A gate that did not need to run, recorded as such rather than silently
    absent. It PASSES because its answer is known, and the summary says why —
    an operator must never have to guess whether a gate ran."""
    return L1Finding(
        check=check,
        passed=True,
        status=GateStatus.PASS,
        detail=f"not run: {why}",
        summary=f"not run: {why}",
    )


def _not_configured(check: str, cfg: L1Config) -> L1Finding:
    # `cfg.source` stays out of `summary`: it interpolates the manifest path and
    # base sha, and `summary` is the one field that reaches the append-only
    # ledger. The operator gets the same fact without them.
    return L1Finding(
        check=check,
        passed=False,
        status=GateStatus.NOT_CONFIGURED,
        detail=f"no {check} command in the trusted manifest {cfg.source}",
        summary=f"no {check} command in the trusted manifest",
    )


def _disabled(check: str, cfg: L1Config) -> "L1Finding | None":
    """O finding de um estágio que o REPO desligou, ou None.

    Vem ANTES do check de not-configured em todos os gates: um estágio
    desligado E sem comando é desligado — a intenção declarada vence a
    ausência. E "desligado" significa que NADA executa; o ledger diz que não
    rodou e por quê, nunca um PASS mudo."""
    if check not in cfg.disabled_stages:
        return None
    return _not_applicable(check, "disabled by the repository manifest (disabled_stages)")


def egress_denial_note(
    denials: "Callable[[], Iterable[tuple[str, int]]] | Iterable[tuple[str, int]] | None",
) -> str:
    """A linha que diz QUE rede o gate não alcançou.

    Um gate que morre por egress dizia só "não consegui ler a saída", e a causa
    ficava numa tabela que ninguém correlacionava: o proxy audita cada recusa
    como `egress_denied`, mas SEM work_item_id — ele vê uma conexão, não um
    item. Por isso a correlação é por janela de tempo, e o texto diz "during
    this run" e nunca "deste item": dois itens em paralelo dividem a janela, e
    afirmar posse seria a plataforma alegando o que não mediu.

    Custou três releases descobrir isto com o spotless: o P2 do Eclipse consulta
    `download.eclipse.org` E `archive.eclipse.org`, e abrir só o primeiro deixa
    o erro byte-idêntico."""
    # Callable, e não lista: a consulta tem de acontecer DEPOIS de o comando
    # rodar (é ele que produz as recusas) e SÓ quando o veredito é ilegível.
    # Recebido como valor pronto, o argumento seria avaliado antes do exec e
    # traria a janela errada — vazia, sempre.
    if callable(denials):
        try:
            denials = denials()
        except Exception:  # noqa: BLE001 — enriquecer mensagem nunca derruba gate
            return ""
    if not denials:
        return ""
    vistos: list[str] = []
    for host, port in denials:
        alvo = f"{host}:{port}"
        if alvo not in vistos:
            vistos.append(alvo)
    return (
        "\nthe egress proxy refused these hosts during this run (they may or "
        "may not belong to this gate — the proxy logs connections, not work "
        f"items): {', '.join(vistos)}\n"
        "add the host to the egress allowlist if the repository's toolchain "
        "legitimately needs it."
    )


def services_hint_note(output: str, *, services_declared: bool) -> str:
    """A linha que aponta o dono certo de um `connection refused` local.

    Tema 1: o sandbox não tem docker e o egress é proxy HTTP — uma suite que
    precisa de Postgres/Redis morre em ECONNREFUSED, o veredito diz FAIL, e o
    laço de fix edita asserções que não têm culpa. O conserto mora no
    MANIFESTO (`services` + `prepare`), e é isso que a nota diz. Enriquece,
    nunca decide: quem chama já fechou `passed`/`status`.

    Com `services` declarado a nota SOME — o banco existe, e "declare
    services" mandaria o laço na direção errada (aí o suspeito é porta/env do
    próprio repo). Compartilhada com o Tester do sandbox-runtime, como o
    builder do settings.xml: um texto só, dono só."""
    if services_declared:
        return ""
    low = (output or "").lower()
    if "econnrefused" not in low and "connection refused" not in low:
        return ""
    return (
        "\na REFUSED LOCAL CONNECTION appears in this output and the "
        "repository manifest declares no `services`. This sandbox has no "
        "docker daemon and the egress proxy only forwards HTTP, so a suite "
        "that needs postgres/redis/etc cannot start one here and cannot reach "
        "one outside. If the code under test needs a backing service, declare "
        "`services` (and `prepare` for migrations/seed) in "
        ".dse/validation.json — editing the test will not create a database.\n"
    )


def lint_check(
    executor: SandboxExecutor, cfg: L1Config, changed_files: set[str] | None = None,
    egress_denials: "Callable[[], Iterable[tuple[str, int]]] | Iterable[tuple[str, int]] | None" = None,
) -> L1Finding:
    if (skip := _disabled("lint", cfg)) is not None:
        return skip
    if not cfg.lint_cmd:
        return _not_configured("lint", cfg)
    if (why := _gate_is_unreachable("lint", changed_files)) is not None:
        return _not_applicable("lint", why)
    # `timeout_for`, not `timeout_seconds`: the manifest's per-stage `timeouts`
    # block is the clock that was validated against the activity's budget, and
    # the number in the message below has to be the one that actually ran.
    timeout = cfg.timeout_for("lint")
    # `lint_subset` + diff conhecido: o comando roda SÓ sobre os arquivos
    # tocados (~113s → segundos, medido no glide-path). O filtro de saída
    # abaixo continua — subset muda o custo, nunca o veredito — e escopo
    # desconhecido (None) mantém o comando cheio: julgar menos por economia
    # esconderia achado real.
    cmd = cfg.lint_cmd
    if cfg.lint_subset_cmd and changed_files is not None and changed_files:
        cmd = cfg.lint_subset_cmd + sorted(changed_files)
    result = _run(executor, cmd, timeout)
    # ruff/flake8: 1 line per issue, formatted as "path:line:col: CODE msg".
    # `_output`, não `result.stdout`: ruff e várias ferramentas escrevem o
    # diagnóstico no stderr quando o stdout está tomado, e ler um stream só é
    # a mesma aposta que custou dois dias no #60.
    issue_lines = _lint_diagnostics(_output(result))
    if not issue_lines:
        # Dialeto do spotless: nenhuma linha `path:line:col`, então a sintaxe
        # concisa nunca compete com ele — a síntese só roda quando o parser
        # canônico não viu nada.
        issue_lines = _spotless_violations(_output(result), changed_files)
    if not issue_lines:
        # E o stylish do ESLint, pela mesma razão e na mesma ordem: os dialetos
        # só competem quando o parser canônico não viu nada.
        issue_lines = _eslint_violations(_output(result), changed_files)
    all_issues = len(issue_lines)
    issue_lines = _only_in_changed_files(issue_lines, changed_files)
    # `result.ok` is dropped from the verdict ON PURPOSE when the diff is known:
    # the command exits non-zero for the whole repository's issues, and this
    # gate answers a narrower question — did OUR change introduce any?
    passed = (result.ok or changed_files is not None) and len(issue_lines) == 0
    if result.timed_out:
        detail = f"timed out after {timeout}s\n{_exec_facts(result)}"
        summary = f"timed out after {timeout}s"
        status = GateStatus.ERROR
    elif (infra := _infra_failure(result)) is not None:
        detail = f"lint: {infra}\n{_exec_facts(result)}\n" + _tail(_output(result))
        summary = f"lint could not run: {infra}"
        passed = False
        status = GateStatus.ERROR
    elif result.returncode == 127:
        detail = f"lint command not found: {' '.join(cfg.lint_cmd)} ({result.stderr.strip()})\n{_exec_facts(result)}"
        # Neither the argv nor the stderr belongs in `summary`. Both come from
        # the customer's own manifest: a repo can declare `lint: ["./ci/lint.sh"]`
        # and have that script dump the sandbox's environment to stderr before
        # exiting 127. That is the leak this field exists to close.
        summary = "lint command not found (exit 127)"
        passed = False
        status = GateStatus.ERROR
    elif not result.ok and all_issues == 0:
        # A ferramenta REPROVOU a árvore e não imprimiu uma única linha no
        # formato que este parser conhece — então não sabemos o que ela viu.
        # Ausência de evidência não é evidência de ausência: sem esta cláusula,
        # `passed` sai True (o exit code é descartado de propósito quando há
        # diff) e o gate publica "no lint issues" sobre uma ferramenta que nem
        # rodou (spotless sem rede, eslint com outro formatter, go vet no
        # stderr — 4/4 reproduzidos na auditoria de 2026-08-20).
        #
        # ERROR e não FAIL, deliberadamente: FAIL entra em `failed_checks` e
        # compra turno de Coder. Num repo com dívida de formatação
        # pré-existente esse turno é impagável — é o incidente que
        # `_only_in_changed_files` existe para impedir, entrando pela outra
        # porta. ERROR entra em `_l1_infra_gates` e escala nomeando o estágio.
        detail = (
            f"lint exited {result.returncode} and printed no diagnostic in the "
            f"'path:line:col: CODE msg' format this gate parses — no verdict on "
            f"the change was possible\n{_exec_facts(result)}\n" + _tail(_output(result))
            + egress_denial_note(egress_denials)
        )
        summary = (
            f"lint could not be read (exit={result.returncode}): no diagnostic "
            "matched the expected format"
        )
        passed = False
        status = GateStatus.ERROR
    else:
        n = len(issue_lines)
        if n:
            summary = (
                f"{n} lint issue(s) in the files this change touched"
                if changed_files is not None
                else f"{n} lint issue(s)"
            )
        elif passed:
            summary = (
                f"no lint issues in the files this change touched "
                f"({all_issues} elsewhere in the repository, not this change's)"
                if changed_files is not None and all_issues
                else "no lint issues"
            )
        else:
            # The linter rejected the tree but printed nothing in the
            # "path:line:col: CODE msg" shape this parser knows — eslint's
            # default formatter is one such. Saying "no lint issues" on a gate
            # that just FAILED is worse than saying nothing: it is the ledger
            # asserting the opposite of the verdict beside it. Observed on the
            # Angular testbed, where lint FAILED reading "no lint issues".
            summary = (
                f"lint failed (exit={result.returncode}); no issue line matched "
                "the expected 'path:line:col: CODE msg' format — see the detail"
            )
        detail = _detail_with(summary, issue_lines, result)
        status = GateStatus.PASS if passed else GateStatus.FAIL
    return L1Finding(
        check="lint", passed=passed, status=status, detail=detail, summary=summary
    )


#: `tsc` diagnostics: "src/a.ts(690,48): error TS2345: ...".
_TSC_ERROR_RE = re.compile(r": error TS\d+:")


def typecheck_check(
    executor: SandboxExecutor, cfg: L1Config, changed_files: set[str] | None = None
) -> L1Finding:
    if (skip := _disabled("typecheck", cfg)) is not None:
        return skip
    if not cfg.typecheck_cmd:
        return _not_configured("typecheck", cfg)
    if (why := _gate_is_unreachable("typecheck", changed_files)) is not None:
        return _not_applicable("typecheck", why)
    timeout = cfg.timeout_for("typecheck")
    result = _run(executor, cfg.typecheck_cmd, timeout)
    # `: error:` is mypy. `tsc` writes `path(line,col): error TS2345: ...`, so
    # matching only mypy's shape counted zero errors on every TypeScript repo
    # and the gate failed reporting "no type errors". This widening changes the
    # COUNT only — `passed` is the command's exit code either way.
    error_lines = [
        ln
        for ln in _output(result).splitlines()
        if ": error:" in ln or _TSC_ERROR_RE.search(ln)
    ]
    if (infra := _infra_failure(result)) is not None:
        return L1Finding(
            check="typecheck",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"typecheck: {infra}\n{_exec_facts(result)}\n" + _tail(_output(result)),
            summary=f"typecheck could not run: {infra}",
        )
    if result.returncode == 127:
        return L1Finding(
            check="typecheck",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"typecheck command not found: {' '.join(cfg.typecheck_cmd)}\n{_exec_facts(result)}",
            summary="typecheck command not found (exit 127)",
        )
    all_errors = len(error_lines)
    if not result.ok and all_errors == 0:
        # Mesma regra do lint, e pela mesma razão (ver o comentário lá): o
        # typechecker reprovou e não falamos o dialeto dele.
        return L1Finding(
            check="typecheck",
            passed=False,
            status=GateStatus.ERROR,
            detail=(
                f"typecheck exited {result.returncode} and printed no diagnostic "
                f"this gate parses (mypy's ': error:' or tsc's 'path(l,c): error') "
                f"— no verdict on the change was possible\n{_exec_facts(result)}\n" + _tail(_output(result))
            ),
            summary=(
                f"typecheck could not be read (exit={result.returncode}): no "
                "diagnostic matched the expected format"
            ),
        )
    error_lines = _only_in_changed_files(error_lines, changed_files)
    # Same reasoning as lint: with a diff in hand the question is not "does this
    # repository typecheck" — it is "did this change break the typecheck".
    passed = (result.ok or changed_files is not None) and not error_lines
    n = len(error_lines)
    if n:
        summary = (
            f"{n} type error(s) in the files this change touched"
            if changed_files is not None
            else f"{n} type error(s)"
        )
    elif passed:
        summary = (
            f"no type errors in the files this change touched "
            f"({all_errors} elsewhere in the repository, not this change's)"
            if changed_files is not None and all_errors
            else "no type errors"
        )
    else:
        summary = (
            f"typecheck failed (exit={result.returncode}); no diagnostic line "
            "was recognised — see the detail"
        )
    if result.timed_out:
        summary = f"timed out after {timeout}s"
        passed = False
        status = GateStatus.ERROR
    else:
        status = GateStatus.PASS if passed else GateStatus.FAIL
    detail = _detail_with(summary, error_lines, result)
    return L1Finding(
        check="typecheck", passed=passed, status=status, detail=detail, summary=summary
    )


#: One "<n> <word>" pair from a runner's count line. The alternation is closed,
#: so only digits and these fixed words can ever leave this parser — which is
#: what keeps the result safe for `summary` and therefore for the append-only
#: ledger (see `L1Finding.summary`).
_COUNT_RE = re.compile(r"(?P<n>\d+) (?P<word>passed|failed|errors?|skipped|todo)\b")

#: Words that mean the run has something wrong in it.
_BAD_WORDS = ("failed", "error", "errors")


#: Surefire's dialect is the mirror image of pytest/jest's: capitalized words,
#: the number AFTER the word, comma-separated — `Tests run: 5, Failures: 2,
#: Errors: 1, Skipped: 1`. The pair pattern above cannot see it at all, so
#: every Java run — green or red — used to read "no test count found".
_SUREFIRE_RE = re.compile(
    r"Tests run: (?P<run>\d+), Failures: (?P<failures>\d+),"
    r" Errors: (?P<errors>\d+), Skipped: (?P<skipped>\d+)"
)


@dataclass(frozen=True)
class TestCounts:
    """What the runner said it did, as NUMBERS. `executed` is the evidence the
    gate anchors on — tests that actually ran (a skip is not execution)."""

    executed: int
    failed: int
    #: Human fragment for `summary`/the ledger, still built only from digits
    #: and the closed word lists above.
    rendered: str


def _counts_from_line(line: str) -> TestCounts | None:
    m = _SUREFIRE_RE.search(line)
    if m:
        run, failures, errors, skipped = (
            int(m.group(g)) for g in ("run", "failures", "errors", "skipped")
        )
        return TestCounts(executed=run - skipped, failed=failures + errors, rendered=m.group(0))
    pairs = _COUNT_RE.findall(line)
    if not pairs:
        return None
    executed = sum(int(n) for n, word in pairs if not word.startswith(("skipped", "todo")))
    failed = sum(int(n) for n, word in pairs if word.startswith(_BAD_WORDS))
    return TestCounts(
        executed=executed, failed=failed, rendered=", ".join(f"{n} {word}" for n, word in pairs)
    )


#: Um `<testsuite ...>` — singular. O `<testsuites>` raiz costuma repetir a
#: soma dos filhos, e contar os dois dobraria tudo. Somar só os singulares
#: fecha certo nos quatro formatos que importam: o pytest emite um único
#: `<testsuite>` dentro da raiz, o jest-junit um por arquivo de spec, o
#: surefire um por arquivo de relatório, o nextest um por binário de teste.
_JUNIT_SUITE_RE = re.compile(r"<testsuite\s[^>]*>")
_JUNIT_ATTR_RE = re.compile(r'\b(tests|failures|errors|skipped)\s*=\s*"(\d+)"')

#: Tetos de leitura. O diretório de relatório de um monorepo tem milhares de
#: arquivos, e isto roda dentro do orçamento do gate.
_JUNIT_MAX_FILES = 500
_JUNIT_MAX_BYTES_PER_FILE = 200_000
_JUNIT_MAX_BYTES_TOTAL = 1_000_000


def _read_junit_reports(executor: SandboxExecutor, glob: str) -> str:
    """O conteúdo dos relatórios JUnit do workspace, capado.

    O glob já saiu do parser com o alfabeto fechado (`_REPORT_GLOB_RE`): sem
    espaço, aspas, `$`, crase ou `;`. Ainda assim ele entra numa única posição
    do script, entre aspas simples, e o `find` é quem o expande — não o shell."""
    script = (
        f"find . -path './{glob}' -type f 2>/dev/null | head -n {_JUNIT_MAX_FILES} | "
        f"while read -r f; do head -c {_JUNIT_MAX_BYTES_PER_FILE} \"$f\"; echo; done | "
        f"head -c {_JUNIT_MAX_BYTES_TOTAL}"
    )
    try:
        run = executor.run(["sh", "-c", script], timeout=60)
    except Exception:  # noqa: BLE001 — leitura de relatório nunca derruba o gate
        return ""
    return run.stdout or ""


def _counts_from_junit(text: str) -> TestCounts | None:
    """A contagem que o RUNNER registrou, somada sobre os relatórios.

    Nenhum dialeto de stdout é consultado aqui: `tests`, `failures`, `errors` e
    `skipped` são atributos do formato, iguais em toda linguagem que o emite.
    Um relatório sem `tests` em suíte nenhuma devolve None — arquivo vazio ou
    truncado no meio de um atributo não é evidência."""
    suites = _JUNIT_SUITE_RE.findall(text)
    if not suites:
        return None
    total = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    viu_tests = False
    for suite in suites:
        for nome, valor in _JUNIT_ATTR_RE.findall(suite):
            total[nome] += int(valor)
            if nome == "tests":
                viu_tests = True
    if not viu_tests:
        return None
    executed = total["tests"] - total["skipped"]
    return TestCounts(
        executed=max(0, executed),
        failed=total["failures"] + total["errors"],
        rendered=(
            f"{total['tests']} tests, {total['failures']} failures, "
            f"{total['errors']} errors, {total['skipped']} skipped (junit)"
        ),
    )


#: Linhas que os runners usam como RODAPÉ — a contagem autoritativa da execução.
#: jest/vitest: "Tests:  3 failed, 4972 passed, 4975 total" (e "Test Suites:").
#: pytest: a cerca "== 272 passed, 3 failed in 12.4s ==".
#: O surefire tem regex própria (`_SUREFIRE_RE`) e é tratado por contagem.
#: Ancorado no INÍCIO da linha, que é o que separa rodapé de nome de teste: o
#: pytest abre a linha com a contagem (com ou sem a cerca de `=`, que o `-q`
#: omite), enquanto uma linha de teste do jest verbose abre com `●`, `✓` ou o
#: nome do describe. Sem a âncora, "…for 403 errors…" vira contagem.
_FOOTER_LINE_RE = re.compile(
    r"^\s*(?:Tests|Test Suites|Snapshots)\s*:"
    r"|^\s*=*\s*\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b",
    re.IGNORECASE,
)


def _test_counts(text: str) -> TestCounts | None:
    """The counts a test runner printed, as numbers — pytest, jest and surefire
    dialects.

    History that must not regress: the first pattern here required `passed`
    FIRST with `failed` optional after it — pytest's order (`272 passed,
    3 failed`). Jest writes the opposite (`Test Suites: 2 failed, 275 passed,
    277 total`), so the optional group never matched and a jest run with two
    broken suites published the byte-identical summary to a green one. That
    string goes into the append-only `audit_log`, and
    `workflows.py::_l1_failure_context` hands it to the next Coder turn — so
    the fix loop was told to repair a suite it was told passed.

    Among footers, one that NAMES a failure beats a bigger green one; among
    failing footers, the one with the most executions wins — surefire prints
    one line per class and then the module total, and the total is never
    smaller than a class line in either metric.
    """
    fallback: TestCounts | None = None
    for line in text.splitlines():
        counts = _counts_from_line(line)
        if counts is None:
            continue
        # RODAPÉ vence, sempre — e entre rodapés a seleção tem DOIS níveis.
        #
        # O que estava aqui antes era `if counts.failed > 0: return counts`:
        # a primeira linha com falha ganhava. Medido em 10/08, nos dois
        # dialetos e em direções opostas:
        #   - surefire imprime uma linha por CLASSE antes do total, então o
        #     gate publicava a primeira classe ("Tests run: 1, Errors: 1")
        #     sobre um total de 2/2 (wi_82254f59);
        #   - jest com `verbose` imprime o NOME de cada teste, e um teste
        #     chamado "...for 403 errors..." virou "403 errors" sobre um
        #     rodapé real de "3 failed, 4972 passed" (wi_176dfa72). O
        #     operador leu isso como "longe do verde"; eram três asserções.
        #
        # A correção de 10/08 ("maior executed vence") criou o defeito oposto
        # num reator Maven MULTI-MÓDULO (wi_8c26a5e7, 2026-08-19): cada módulo
        # imprime o próprio total e não existe total global, então o módulo
        # verde de 1141 testes apagou o `bootstrap` com `151, Failures: 1,
        # Errors: 7` — e o resumo publicado foi "no test failed… coverage
        # threshold or similar". Esse texto é o que _l1_failure_context entrega
        # ao Coder: ele caçou um problema de cobertura que não existe e o item
        # morreu em coder_not_converging. Fabricado pelo resumo.
        #
        # Regra atual, preservando as duas lições de 10/08 por construção:
        #   1. rodapé com falha vence rodapé verde (o vermelho nunca é apagado);
        #   2. no mesmo nível, vence o de maior `executed` (total do módulo ≥
        #      suas linhas de classe nas DUAS métricas).
        # Nunca se SOMA: linha de classe + total do próprio módulo dobrariam a
        # conta. O preço honesto é que num reator multi-módulo o resumo é o do
        # módulo que falhou (ou, todo verde, o do maior módulo) — não a soma.
        is_footer = bool(_SUREFIRE_RE.search(line)) or bool(_FOOTER_LINE_RE.search(line))
        if not is_footer:
            continue
        if fallback is None:
            fallback = counts
            continue
        melhor_falha = (counts.failed > 0, counts.executed)
        atual_falha = (fallback.failed > 0, fallback.executed)
        if melhor_falha > atual_falha:
            fallback = counts
    return fallback


#: Lines a runner uses to head a broken suite. These carry file paths, which is
#: why they are only ever used for `detail` (which lives in `validation_runs`
#: and is subject to retention) and never for `summary`.
#: Os dois dialetos: nus (jest/pytest — `FAIL src/x.spec.ts`) e ENTRE
#: COLCHETES (surefire — `[ERROR] contextLoads ... <<< ERROR!`). Sem o segundo,
#: o detail de um vermelho Maven colapsava para a linha-resumo: a evidência
#: real vive acima do rodapé longo do mvn, fora do alcance do tail — e o Coder
#: consertou cego por três rodadas pagas (wi_893de651, defeito 4 da série).
_FAILING_SUITE_RE = re.compile(r"^\s*(?:\[(?:ERROR|FAIL(?:ED)?)\]|(?:FAIL|FAILED|ERROR)\b).*$")


#: Identificadores de suite que os runners imprimem ao reprovar: o caminho, no
#: jest (`FAIL src/...spec.ts`), e a CLASSE, no surefire (`... -- in com.x.Y`).
#: São a chave da comparação com o base — arquivo a arquivo, nunca em bloco.
_JEST_SUITE_ID_RE = re.compile(r"^\s*FAIL\s+(\S+)", re.MULTILINE)
_SUREFIRE_SUITE_ID_RE = re.compile(r"^\[ERROR\].*?--\s+in\s+(\S+)\s*$", re.MULTILINE)

_BASELINE_CACHE_VERSION = 1
#: A mesma guarda de hooks de todo comando git do DSE: a worktree faz checkout,
#: e um `post-checkout` do repositório do cliente rodaria aqui.
_NO_CUSTOMER_HOOKS_PATH = "/nonexistent/dse-no-hooks"


def _failing_suite_ids(output: str) -> list[str]:
    ids: list[str] = []
    for rx in (_JEST_SUITE_ID_RE, _SUREFIRE_SUITE_ID_RE):
        for m in rx.finditer(output or ""):
            sid = m.group(1)
            if sid not in ids:
                ids.append(sid)
    return ids


def _baseline_failing_suites(
    executor: SandboxExecutor, cfg: L1Config, base_sha: str, timeout: int
) -> list[str] | None:
    """As suites que JÁ falhavam em `base_sha`.

    `None` = DESCONHECIDO, e é fail-closed: sem resposta confiável nada é
    herdado e a reprovação fica exatamente como estava.

    Três decisões que o comentário tem que sobreviver a mim:

    - **worktree destacada, nunca o workspace.** O L1 julga o estado COMMITADO;
      um `checkout base_sha` no lugar deixaria a árvore suja para o
      `finalize_pr` e trocaria um falso-negativo por corrupção.
    - **comando do manifesto VERBATIM.** Comparar saídas exige o mesmo comando;
      escopar por suite mudaria o que o runner imprime e a comparação passaria
      a medir o escopo, não o repositório.
    - **cache no /tmp do Pod.** O emptyDir vive enquanto o sandbox viver, que é
      exatamente o alcance do laço de retentativa: o custo é uma vez por item,
      não uma vez por rodada (era a objeção de custo).
    """
    tag = base_sha[:12]
    cache_path = f"/tmp/.dse-baseline-{tag}.json"
    cached = executor.run(["sh", "-c", f"cat {cache_path} 2>/dev/null"], timeout=30)
    if cached.returncode == 0 and (cached.stdout or "").strip():
        try:
            payload = json.loads(cached.stdout)
            if int(payload.get("v") or 0) == _BASELINE_CACHE_VERSION:
                return [str(s) for s in (payload.get("suites") or [])]
        except (ValueError, TypeError, AttributeError):
            pass  # cache ilegível: refaz em vez de confiar

    wt = f"/tmp/dse-baseline-{tag}"
    script = (
        f"rm -rf {wt}; "
        f"git -c core.hooksPath={_NO_CUSTOMER_HOOKS_PATH} worktree add --detach "
        f"{wt} {base_sha} >/dev/null 2>&1 || exit 90; "
        # node_modules por symlink: reinstalar no baseline custaria mais que o
        # próprio gate, e a instalação é a mesma do HEAD por construção.
        f'if [ -d node_modules ]; then ln -s "$PWD/node_modules" {wt}/node_modules 2>/dev/null || true; fi; '
        f"cd {wt} && {shlex.join(cfg.test_cmd)}"
    )
    run = executor.run(["sh", "-c", script], timeout=timeout)
    executor.run(
        ["sh", "-c",
         f"git -c core.hooksPath={_NO_CUSTOMER_HOOKS_PATH} worktree remove --force {wt} "
         f">/dev/null 2>&1; rm -rf {wt}"],
        timeout=60,
    )
    if run.returncode == 90 or _infra_failure(run) is not None or getattr(run, "timed_out", False):
        # A worktree não subiu, ou o runtime matou o baseline. Um resultado
        # vazio aqui seria lido como "o base estava verde" e transformaria uma
        # dúvida em veredito — exatamente o que não pode acontecer.
        return None
    suites = _failing_suite_ids(_output(run))
    executor.run(
        ["sh", "-c",
         "printf '%s' "
         + shlex.quote(json.dumps({"v": _BASELINE_CACHE_VERSION, "suites": suites}))
         + f" > {cache_path}"],
        timeout=30,
    )
    return suites


#: Como cada ecossistema diz "não rode este teste". Maven aceita a negação
#: dentro do `-Dtest=` (`-Dtest='!Foo'`), jest/vitest usam
#: `--testPathIgnorePatterns`, pytest tem `--ignore=`/`--deselect`.
_EXCLUSION_PATTERNS = (
    re.compile(r"-Dtest=['\"]?!([^'\"\s]+)"),
    re.compile(r"--testPathIgnorePatterns[=\s]+['\"]?([^'\"\s]+)"),
    re.compile(r"--ignore[=\s]+['\"]?([^'\"\s]+)"),
    re.compile(r"--deselect[=\s]+['\"]?([^'\"\s]+)"),
)


def _test_exclusions(test_cmd: list[str] | None) -> list[str]:
    """Os testes que o COMANDO DO CLIENTE manda pular, pelo nome.

    Achado em 2026-08-10 no testbed Java: `.dse/validation.json` roda
    `-Dtest='!BmoFeeCalculatorBeApplicationTests'`, e essa é a ÚNICA classe de
    teste do repositório — o `@SpringBootTest contextLoads()`. Ou seja, o
    comando exclui exatamente a prova de que o contexto Spring não sobe naquele
    ambiente. O DSE não sabia, o Tester escreveu testes de contexto (que não
    estão na exclusão) e o item queimou o cap em `Failed to load
    ApplicationContext`.

    Isto só REPORTA. Excluir teste é escolha legítima do dono do repositório, e
    consertar o repositório do cliente não é trabalho nosso (decisão do
    operador). O que deixa de ser aceitável é o operador ter que deduzir isso
    de novo a cada rodada."""
    joined = " ".join(test_cmd or [])
    found: list[str] = []
    for pattern in _EXCLUSION_PATTERNS:
        found.extend(m.group(1) for m in pattern.finditer(joined))
    return found


def test_check(
    executor: SandboxExecutor, cfg: L1Config, changed_files: set[str] | None = None,
    base_sha: str | None = None,
) -> L1Finding:
    if (skip := _disabled("test", cfg)) is not None:
        return skip
    if not cfg.test_cmd:
        return _not_configured("test", cfg)
    if (why := _gate_is_unreachable("test", changed_files)) is not None:
        return _not_applicable("test", why)
    timeout = cfg.timeout_for("test")
    result = _run(executor, cfg.test_cmd, timeout)
    if (infra := _infra_failure(result)) is not None:
        return L1Finding(
            check="test",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"test: {infra}\n{_exec_facts(result)}\n" + _tail(_output(result)),
            summary=f"the test suite could not run: {infra}",
        )
    if result.returncode == 127:
        return L1Finding(
            check="test",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"test command not found: {' '.join(cfg.test_cmd)}\n{_exec_facts(result)}",
            summary="test command not found (exit 127)",
        )
    output = _output(result)
    # Both streams: jest prints its counts and its failure blocks to STDERR,
    # and the old `stdout or stderr` never reached stderr at all.
    #
    # O RELATÓRIO vem primeiro, e a ordem é a tese desta versão: `tests`,
    # `failures`, `errors` e `skipped` são atributos do formato JUnit, iguais
    # em toda linguagem que o emite. O rodapé de stdout é prosa, e conhecê-la
    # custava uma regex por ecossistema — a conta que fazia Go, cargo, dotnet,
    # rspec e phpunit reprovarem VERDES, porque `counts is None` reprovava pela
    # regra de evidência abaixo. Onde os dois existem e discordam, o arquivo
    # ganha: ele é registro, o rodapé é impressão.
    counts = None
    if cfg.junit_reports:
        counts = _counts_from_junit(_read_junit_reports(executor, cfg.junit_reports))
    if counts is None:
        counts = _test_counts(output)
    # Exit code alone is not a verdict: the gate demands EVIDENCE that at least
    # one test executed. On the Java testbed the gate's own command excluded
    # the repo's only test class, and "exit 0, no count found" passed a
    # pristine tree — a gate approving the absence of evidence is not a gate.
    # Declared absence (no test_cmd) already returned NOT_CONFIGURED above;
    # that stays the only escape.
    evidence = counts is not None and counts.executed > 0
    passed = result.ok and evidence

    # --- vermelho HERDADO: o que o item ENCONTROU, não o que trouxe ---------
    # Becos 2 e 3 do mapa de alcançabilidade: uma suite que já era vermelha no
    # base não tem ator autorizado (não é do item) nem parque desenhado (a
    # porta 1 exige o sujeito no diff) — o item morria no teto por algo que não
    # quebrou. Comparação POR SUITE: a granularidade é o preço, e ela é
    # honesta — se o item piorar uma suite já vermelha, o arquivo continua na
    # lista herdada e a piora não aparece aqui (aparece no diff e no L2).
    inherited: list[str] = []
    own_failures: list[str] = []
    if base_sha and not result.ok and not result.timed_out:
        failing_now = _failing_suite_ids(output)
        if failing_now:
            at_base = _baseline_failing_suites(executor, cfg, base_sha, timeout)
            if at_base is not None:
                at_base_set = set(at_base)
                inherited = [s for s in failing_now if s in at_base_set]
                own_failures = [s for s in failing_now if s not in at_base_set]

    if result.timed_out:
        summary = f"timed out after {timeout}s running tests — L1 fails clean (P6)"
        detail = f"timed out after {timeout}s running tests — L1 fails clean (P6), no truncation"
        status = GateStatus.ERROR
    elif counts is not None and evidence and inherited and not own_failures:
        # NOT_OUR_FAILURE: TODA suite vermelha aqui já era vermelha no base. O
        # item segue — não há o que ele conserte, e não há decisão humana sobre
        # ELE (o repositório vermelho é um problema do repositório, e escalar
        # aqui pararia todo item atrás dele). Nunca em silêncio: o rótulo vai
        # no summary, os NOMES no detail, a CONTAGEM no ledger.
        #
        # A regra de evidência do defeito B continua acima desta: `evidence` é
        # condição da branch, então "nada executou" nunca vira NOT_OUR_FAILURE.
        passed = True
        status = GateStatus.PASS
        summary = (
            f"NOT_OUR_FAILURE: {len(inherited)} suite(s) already red at base "
            f"{base_sha[:8]} — not broken by this change (summary: {counts.rendered})"
        )
    elif counts is not None and evidence:
        summary = f"summary: {counts.rendered}"
        if inherited:
            summary += (
                f" — {len(inherited)} of them inherited (already red at base "
                f"{base_sha[:8]}); the rest is this change's (see detail)"
            )
        if not result.ok and counts.failed == 0:
            # Every test passed and the command still failed. That is not a
            # contradiction and it is not ours to hide: the runner is enforcing
            # something besides the tests — a global coverage threshold, a
            # lint-in-test step, `errorOnDeprecated`. Saying only "275 passed"
            # beside a FAIL is the ledger asserting the opposite of the verdict
            # next to it, and it sends the fix loop hunting a broken test that
            # does not exist.
            summary += (
                f" — no test failed, but the command exited {result.returncode}: "
                "the suite's own policy rejected the run (coverage threshold or "
                "similar), not a failing test — see the detail"
            )
        status = GateStatus.PASS if passed else GateStatus.FAIL
    elif counts is not None:
        # Houve leitura, e ela diz que NADA executou. Isso é veredito, não
        # ilegibilidade: a defesa do defeito B continua inteira (o comando do
        # gate excluía a única classe de teste do repositório e o L1 aprovava
        # uma árvore intocada).
        summary = f"exit {result.returncode} without evidence of test execution ({counts.rendered})"
        status = GateStatus.FAIL
    else:
        # NINGUÉM conseguiu ler. Isto era FAIL — e FAIL, no ledger e no
        # `_l1_failure_context`, diz ao próximo turno de Coder que o diff
        # quebrou os testes. Ele então persegue um assert que não existe até o
        # teto de tentativas. ERROR é a verdade e tem outro dono: quem conserta
        # é o manifesto, e a mensagem diz qual campo.
        summary = (
            f"the test run could not be read (exit {result.returncode}): no JUnit "
            "report was declared and no count in the output matched a dialect this "
            "gate knows (pytest/jest/surefire) — declare `reports.junit` in "
            ".dse/validation.json and no verdict has to be guessed"
        )
        status = GateStatus.ERROR
    failing = [ln for ln in output.splitlines() if _FAILING_SUITE_RE.match(ln)]
    # Os nomes do herdado vão no detail SEMPRE — inclusive quando o gate passa,
    # onde `_detail_with` descarta as linhas de falha. Um NOT_OUR_FAILURE sem os
    # nomes seria o gate pedindo confiança em vez de mostrar evidência.
    inherited_note = (
        f"INHERITED (already red at base {base_sha[:8]}): {', '.join(inherited)}\n"
        if inherited else ""
    )
    # ADVISORY de configuração: reporta, nunca reprova (`passed` já está
    # decidido acima). O NOME do teste excluído é conteúdo do repositório do
    # cliente e por isso vai só no `detail` — `summary` é o único campo que
    # entra no audit_log, que é append-only e copiado verbatim para o console.
    excluded = _test_exclusions(cfg.test_cmd)
    if excluded:
        summary += (
            f" — heads-up: this repository's own test command excludes "
            f"{len(excluded)} test(s) by name (see detail); tests the DSE adds "
            "are NOT covered by that exclusion"
        )
        inherited_note += (
            "CONFIG ADVISORY: the test command in this repository excludes "
            f"{', '.join(excluded)}. A test the DSE writes is not excluded by "
            "it, so it can hit a wall the existing suite never reports.\n"
        )
    # ADVISORY do Tema 1: aponta o manifesto quando o vermelho é um
    # `connection refused` sem `services` declarado. Só em detail — `passed` e
    # `status` já estão decididos acima e a nota nunca os toca.
    if not passed:
        inherited_note += services_hint_note(
            output, services_declared=bool(cfg.services_declared)
        )
    detail = inherited_note + _detail_with(summary, [] if passed else failing, result)
    return L1Finding(
        check="test", passed=passed, status=status, detail=detail, summary=summary,
        inherited_failures=inherited,
    )


def build_check(
    executor: SandboxExecutor, cfg: L1Config, changed_files: set[str] | None = None
) -> L1Finding:
    if (skip := _disabled("build", cfg)) is not None:
        return skip
    if not cfg.build_cmd:
        return _not_configured("build", cfg)
    if (why := _gate_is_unreachable("build", changed_files)) is not None:
        return _not_applicable("build", why)
    timeout = cfg.timeout_for("build")
    result = _run(executor, cfg.build_cmd, timeout)
    if (infra := _infra_failure(result)) is not None:
        return L1Finding(
            check="build",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"build: {infra}\n" + _tail(_output(result)),
            summary=f"the build could not run: {infra}",
        )
    if result.returncode == 127:
        return L1Finding(
            check="build",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"build command not found: {' '.join(cfg.build_cmd)}\n{_exec_facts(result)}",
            summary="build command not found (exit 127)",
        )
    passed = result.ok
    summary = "build ok" if passed else f"build failed (exit={result.returncode})"
    if result.timed_out:
        summary = f"timed out after {timeout}s"
        status = GateStatus.ERROR
    else:
        status = GateStatus.PASS if passed else GateStatus.FAIL
    detail = _detail_with(summary, [], result)
    return L1Finding(
        check="build", passed=passed, status=status, detail=detail, summary=summary
    )
