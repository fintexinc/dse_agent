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


def lint_check(
    executor: SandboxExecutor, cfg: L1Config, changed_files: set[str] | None = None
) -> L1Finding:
    if not cfg.lint_cmd:
        return _not_configured("lint", cfg)
    if (why := _gate_is_unreachable("lint", changed_files)) is not None:
        return _not_applicable("lint", why)
    # `timeout_for`, not `timeout_seconds`: the manifest's per-stage `timeouts`
    # block is the clock that was validated against the activity's budget, and
    # the number in the message below has to be the one that actually ran.
    timeout = cfg.timeout_for("lint")
    result = _run(executor, cfg.lint_cmd, timeout)
    # ruff/flake8: 1 line per issue, formatted as "path:line:col: CODE msg".
    issue_lines = [ln for ln in result.stdout.splitlines() if re.match(r"^\S+:\d+:\d+:\s", ln)]
    all_issues = len(issue_lines)
    issue_lines = _only_in_changed_files(issue_lines, changed_files)
    # `result.ok` is dropped from the verdict ON PURPOSE when the diff is known:
    # the command exits non-zero for the whole repository's issues, and this
    # gate answers a narrower question — did OUR change introduce any?
    passed = (result.ok or changed_files is not None) and len(issue_lines) == 0
    if result.timed_out:
        detail = f"timed out after {timeout}s running {' '.join(cfg.lint_cmd)}"
        summary = f"timed out after {timeout}s"
        status = GateStatus.ERROR
    elif (infra := _infra_failure(result)) is not None:
        detail = f"lint: {infra}\n" + _tail(_output(result))
        summary = f"lint could not run: {infra}"
        passed = False
        status = GateStatus.ERROR
    elif result.returncode == 127:
        detail = f"lint command not found: {' '.join(cfg.lint_cmd)} ({result.stderr.strip()})"
        # Neither the argv nor the stderr belongs in `summary`. Both come from
        # the customer's own manifest: a repo can declare `lint: ["./ci/lint.sh"]`
        # and have that script dump the sandbox's environment to stderr before
        # exiting 127. That is the leak this field exists to close.
        summary = "lint command not found (exit 127)"
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
        for ln in result.stdout.splitlines()
        if ": error:" in ln or _TSC_ERROR_RE.search(ln)
    ]
    if (infra := _infra_failure(result)) is not None:
        return L1Finding(
            check="typecheck",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"typecheck: {infra}\n" + _tail(_output(result)),
            summary=f"typecheck could not run: {infra}",
        )
    if result.returncode == 127:
        return L1Finding(
            check="typecheck",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"typecheck command not found: {' '.join(cfg.typecheck_cmd)}",
            summary="typecheck command not found (exit 127)",
        )
    all_errors = len(error_lines)
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

    A line naming a non-zero failure wins outright; otherwise the line with the
    most executions wins — surefire prints one line per class and then the
    totals, and the totals are never smaller than a class line.
    """
    fallback: TestCounts | None = None
    for line in text.splitlines():
        counts = _counts_from_line(line)
        if counts is None:
            continue
        # RODAPÉ vence, sempre — e o maior rodapé vence entre rodapés.
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
        # `_SUREFIRE_RE` já é ancorada o bastante para não casar nome de
        # teste; para os demais dialetos exigimos a forma de rodapé, e entre
        # candidatos vence o de maior `executed` — o total nunca é menor que
        # uma linha de classe, que é o que o docstring sempre afirmou.
        is_footer = bool(_SUREFIRE_RE.search(line)) or bool(_FOOTER_LINE_RE.search(line))
        if not is_footer:
            continue
        if fallback is None or counts.executed > fallback.executed:
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


def test_check(
    executor: SandboxExecutor, cfg: L1Config, changed_files: set[str] | None = None,
    base_sha: str | None = None,
) -> L1Finding:
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
            detail=f"test: {infra}\n" + _tail(_output(result)),
            summary=f"the test suite could not run: {infra}",
        )
    if result.returncode == 127:
        return L1Finding(
            check="test",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"test command not found: {' '.join(cfg.test_cmd)}",
            summary="test command not found (exit 127)",
        )
    output = _output(result)
    # Both streams: jest prints its counts and its failure blocks to STDERR,
    # and the old `stdout or stderr` never reached stderr at all.
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
    elif result.ok:
        rendered = f"({counts.rendered})" if counts is not None else "(no test count found in the output)"
        summary = f"exit 0 without evidence of test execution {rendered}"
        status = GateStatus.FAIL
    else:
        summary = f"exit code {result.returncode} (no test count found in the output)"
        status = GateStatus.FAIL
    failing = [ln for ln in output.splitlines() if _FAILING_SUITE_RE.match(ln)]
    # Os nomes do herdado vão no detail SEMPRE — inclusive quando o gate passa,
    # onde `_detail_with` descarta as linhas de falha. Um NOT_OUR_FAILURE sem os
    # nomes seria o gate pedindo confiança em vez de mostrar evidência.
    inherited_note = (
        f"INHERITED (already red at base {base_sha[:8]}): {', '.join(inherited)}\n"
        if inherited else ""
    )
    detail = inherited_note + _detail_with(summary, [] if passed else failing, result)
    return L1Finding(
        check="test", passed=passed, status=status, detail=detail, summary=summary,
        inherited_failures=inherited,
    )


def build_check(
    executor: SandboxExecutor, cfg: L1Config, changed_files: set[str] | None = None
) -> L1Finding:
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
            detail=f"build command not found: {' '.join(cfg.build_cmd)}",
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
