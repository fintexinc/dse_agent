"""Patch compliance against the plan and immutable SHAs.

The diff is always ``base_sha...head_sha``. Branch names are mutable and may
not even exist in the sandbox clone; accepting them here was the cause of the
regression where ``main...HEAD`` got the WorkItem stuck.
"""
from __future__ import annotations

import logging

import re

from dse_contracts import GateStatus, L1Finding, PlanArtifact
from dse_contracts.paths import (
    first_forbidden_match,
    is_test_path,
    path_is_authorised,
)

from dse_validation.sandbox_exec import SandboxExecutor


class DiffSummary:
    def __init__(
        self,
        files_changed: list[str],
        total_lines_changed: int,
        *,
        base_sha: str,
        head_sha: str,
        lines_by_file: dict[str, int] | None = None,
    ):
        self.files_changed = files_changed
        self.total_lines_changed = total_lines_changed
        self.base_sha = base_sha
        self.head_sha = head_sha
        # Per-file breakdown, so the budget can charge each line to whoever
        # wrote it. A caller that does not supply it gets an empty map and
        # therefore pays for EVERY line: the budget errs strict, never loose.
        self.lines_by_file = dict(lines_by_file or {})

    @property
    def non_test_lines_changed(self) -> int:
        """Lines of the diff that live outside test paths.

        `is_test_path` is the same predicate the TesterToolset enforces on
        `write_file` (`toolsets.py`), so it is not a suffix heuristic invented
        here: it is exactly the set of paths the Tester was allowed to write.
        """
        return self.total_lines_changed - sum(
            n for path, n in self.lines_by_file.items() if is_test_path(path)
        )


class DiffComputationError(RuntimeError):
    pass


_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


def _verify_commit(executor: SandboxExecutor, sha: str, label: str, timeout: int) -> None:
    if not _FULL_GIT_SHA_RE.fullmatch(sha):
        raise DiffComputationError(
            f"{label} must be a full Git SHA of 40 or 64 hexadecimal characters"
        )
    result = executor.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], timeout=timeout)
    if not result.ok:
        raise DiffComputationError(f"{label}={sha} does not exist as a commit in the sandbox")


def compute_diff_summary(
    executor: SandboxExecutor,
    base_sha: str,
    head_sha: str,
    timeout: int = 60,
) -> DiffSummary:
    """``git diff --numstat <base_sha>...<head_sha>`` inside the sandbox — sums
    added+removed lines per file (binary files report "-" in the numstat; we
    count them as a touched file but 0 lines, so diffs with assets don't
    break)."""
    _verify_commit(executor, base_sha, "base_sha", timeout)
    _verify_commit(executor, head_sha, "head_sha", timeout)
    result = executor.run(
        ["git", "diff", "--numstat", f"{base_sha}...{head_sha}"], timeout=timeout
    )
    if result.returncode != 0:
        raise DiffComputationError(
            f"git diff --numstat failed (exit={result.returncode}): {result.stderr.strip()}"
        )
    files: list[str] = []
    lines_by_file: dict[str, int] = {}
    total = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        changed = 0
        if added.isdigit():
            changed += int(added)
        if removed.isdigit():
            changed += int(removed)
        files.append(path)
        lines_by_file[path] = changed
        total += changed
    return DiffSummary(
        files_changed=files,
        total_lines_changed=total,
        base_sha=base_sha,
        head_sha=head_sha,
        lines_by_file=lines_by_file,
    )


# O matcher vive em `dse_contracts.paths` desde 2026-08-19: o classificador de
# risco (sandbox-runtime) fazia a mesma pergunta com outra regra e dava outra
# resposta em monorepo — "low" lá, violação aqui —, então o plano nem chegava
# ao gate humano. Vale o mais estrito, que é este. O alias fica porque os
# testes deste gate o nomeiam.
_is_forbidden = first_forbidden_match


def diff_budget_finding(diff: DiffSummary, plan: PlanArtifact) -> L1Finding:
    # no_code_change: the plan declares there is NO code change, but the
    # immutable diff changed files — a real inconsistency, so it fails. (This
    # is NOT expected_files; it is about whether a diff exists at all.)
    if plan.no_code_change and diff.files_changed:
        return L1Finding(
            check="diff_budget",
            passed=False,
            status=GateStatus.FAIL,
            detail=(
                "PlanArtifact.no_code_change=true, but the immutable diff "
                f"{diff.base_sha[:12]}...{diff.head_sha[:12]} changed {diff.files_changed}"
            ),
            summary=(
                "PlanArtifact.no_code_change=true, but the immutable diff "
                f"changed {len(diff.files_changed)} file(s)"
            ),
        )

    # OPERATOR DECISION (2026-07-22, 3rd real occurrence): expected_files no
    # longer fails the diff. The Planner predicts files from the TEXT of the
    # issue, BEFORE reading the code; in a bug fix the defect almost always
    # lives in a different layer than the symptom suggests (the issue talked
    # about DELETE /api/transactions → server.js; the bug was in src/store.js —
    # the Coder picked the right file and the gate failed the CORRECT fix).
    #
    # Safety gates that REMAIN (they don't depend on the Planner's prediction):
    #   - line budget (here): real anti-sprawl;
    #   - forbidden_paths: a SEPARATE hard check (migrations/, workflows/…);
    #   - sandbox scoped to the repo; empty plan blocked in the workflow
    #     (patch reject-empty-expected-files-v1), before L1.
    # expected_files is still used to CLASSIFY RISK in the workflow — it just
    # stopped being an equality gate on the diff.
    #
    # Test paths are NOT charged. This gate exists to contain the CODER's
    # sprawl, and it was being blown by another agent: measured on a real run,
    # 379 of the 400 lines, of which 218 (57.5% of the budget) were the two test
    # files the TESTER wrote one activity earlier — the Coder paid, with a
    # retry, for a diff it did not write. The exemption is not an escape hatch
    # for the Coder either, and desde 2026-08-10 não há mais nenhum revert de
    # teste: o DSE altera qualquer teste e a mudança é revisada no diff da PR.
    # A isenção segue valendo pelo motivo ORIGINAL (o Coder não pagar pelo diff
    # que o Tester escreveu) — não porque "as edições dele seriam desfeitas de
    # qualquer jeito". Este comentário já afirmou as duas políticas anteriores;
    # quem mudar a terceira atualiza aqui.
    # OPERATOR DECISION (2026-08-11): o TAMANHO do diff deixou de reprovar.
    #
    # O teto vinha de `PlanArtifact.diff_budget_lines`, default 400 no contrato,
    # e o Planner não é instruído sobre esse campo em lugar nenhum — então todo
    # plano de todo item saía com 400, independentemente do trabalho pedido.
    # Medido no wi_4f680518 (US$ 5,41, sem PR): um bootstrap de app Angular
    # passou em lint, typecheck, test, build, sast e secret_scan, e reprovou com
    # 19.605 linhas contra 400, só o `package-lock.json` valendo mais de 15 mil.
    # O laço não tinha saída: encolher o diff seria não fazer o que foi pedido,
    # então cada rodada repetia a anterior até um humano parar o item.
    #
    # Um anti-sprawl precisa que o número venha do TRABALHO. Vindo de constante,
    # ele não mede sprawl — mede se a tarefa é grande, e tarefa grande não é
    # defeito. O override por ambiente saiu junto: enquanto existisse, um
    # values.yaml poderia reintroduzir o teto sem passar por decisão nenhuma.
    #
    # A MEDIDA fica, e é isso que separa isto de apagar o gate: quem revisa a PR
    # continua lendo quantas linhas vieram, e `forbidden_paths` (gate separado)
    # continua duro sobre QUAIS caminhos foram tocados — que é a pergunta que
    # de fato protege alguma coisa.
    charged = diff.non_test_lines_changed
    return L1Finding(
        check="diff_budget",
        passed=True,
        detail=(
            f"diff size is informational: {charged} lines outside test paths, "
            f"{diff.total_lines_changed} lines total across "
            f"{len(diff.files_changed)} file(s) "
            "(size stopped being a verdict on 2026-08-11; expected_files is "
            "advisory; forbidden_paths validates the paths)"
        ),
        summary=(
            f"diff size {charged} lines outside test paths (informational)"
        ),
    )


def forbidden_paths_finding(diff: DiffSummary, plan: PlanArtifact) -> L1Finding:
    """Nenhum arquivo sob caminho protegido — exceto os que o humano autorizou.

    A ISENÇÃO (2026-08-19). Até aqui este gate não tinha porta: um plano que
    precisava escrever em `.github/workflows/` carregava a proibição do mesmo
    caminho, e o único diff que passava era o diff que não entregava a tarefa.
    Medido: ~US$ 4 e 40 min até `coder_not_converging`, num pedido banal de CI.

    O que autoriza é o `expected_files` do plano APROVADO — este é o plano que
    o workflow passa para o L1 (`input.plan_json`), e o gate humano grava o
    `plan_hash` dele. Desde o mesmo dia, colisão FORÇA o gate humano (patch
    `protected-paths-need-approval-v1`), com os arquivos nomeados no corpo da
    mensagem: quando um arquivo protegido chega aqui isento, alguém leu o nome
    dele e clicou aprovar.

    E a porta não é buraco: a isenção é por ARQUIVO declarado, nunca pelo
    caminho protegido inteiro. O Coder não consegue alargar depois da aprovação
    o que foi aprovado — é o que `test_a_protected_file_nobody_approved_still_fails`
    pina.
    """
    violations: list[tuple[str, str]] = []
    authorised: list[tuple[str, str]] = []
    for f in diff.files_changed:
        hit = _is_forbidden(f, plan.forbidden_paths)
        if not hit:
            continue
        if path_is_authorised(f, plan.expected_files):
            authorised.append((f, hit))
        else:
            violations.append((f, hit))

    if not violations:
        liberados = (
            "; approved at the plan gate and therefore allowed: "
            + ", ".join(f"{f} (under '{hit}')" for f, hit in authorised)
            if authorised
            else ""
        )
        return L1Finding(
            check="forbidden_paths",
            passed=True,
            detail=(
                f"no unapproved file touched under the plan's forbidden_paths "
                f"({plan.forbidden_paths}){liberados}"
            ),
            summary=(
                f"{len(authorised)} approved file(s) under a protected path, no violation"
                if authorised
                else "no file touched under the plan's forbidden_paths"
            ),
        )

    detail = "; ".join(
        f"{f} is under a path forbidden by PlanArtifact.forbidden_paths='{hit}'" for f, hit in violations
    )
    # The paths themselves are repository content and stay in `detail`; the
    # count is the platform's own and is what the ledger gets.
    return L1Finding(
        check="forbidden_paths",
        passed=False,
        detail=detail,
        summary=f"{len(violations)} file(s) under a path forbidden by the plan",
    )


logger = logging.getLogger(__name__)


def compute_diff_or_none(executor: SandboxExecutor, base_sha: str, head_sha: str):
    """The diff, or None when it cannot be computed.

    Computed ONCE per pipeline and handed to both consumers. It used to run
    twice — once to scope the per-file gates, once inside
    `plan_compliance_findings` — which is six `kubectl exec` round trips where
    three suffice, and which also made `_PIPELINE_FIXED_COST_SECONDS` (sized for
    three 60s git calls) understate the activity's fixed cost by 180s against
    its own start_to_close."""
    try:
        return compute_diff_summary(executor, base_sha, head_sha)
    except Exception:  # noqa: BLE001
        logger.warning("l1: the diff could not be computed — gates fall back to whole-repo scope",
                       exc_info=True)
        return None


def changed_files_or_none(
    executor: SandboxExecutor, base_sha: str, head_sha: str
) -> set[str] | None:
    """The paths this change touched, or None when the diff cannot be computed.

    None is deliberately not an empty set: an empty set would scope every
    per-file gate down to nothing and pass a broken change silently. None means
    "scope unknown", and the gates then judge everything, as they did before."""
    try:
        diff = compute_diff_summary(executor, base_sha, head_sha)
    except Exception:  # noqa: BLE001
        # Deliberately every exception, not just DiffComputationError. This
        # function only narrows the scope of other gates; it must never be the
        # reason the L1 activity dies. Whatever went wrong, "scope unknown" is
        # the safe answer and the gates go back to judging everything.
        logger.warning("l1: the diff could not be computed — gates fall back to whole-repo scope",
                       exc_info=True)
        return None
    return {f.lstrip("./") for f in diff.files_changed}


def plan_compliance_findings(
    executor: SandboxExecutor,
    plan: PlanArtifact,
    base_sha: str,
    head_sha: str,
    diff=None,
) -> list[L1Finding]:
    """`diff` is the one the pipeline already computed. Passing it avoids a
    second round of `kubectl exec` calls for an answer we have."""
    if diff is not None:
        return [diff_budget_finding(diff, plan), forbidden_paths_finding(diff, plan)]
    try:
        diff = compute_diff_summary(executor, base_sha, head_sha)
    except DiffComputationError as exc:
        return [
            L1Finding(
                check="git_diff",
                passed=False,
                status=GateStatus.ERROR,
                detail=str(exc),
                # `exc` wraps git's stderr — repository content.
                summary="the diff between base and head could not be computed",
            )
        ]
    return [diff_budget_finding(diff, plan), forbidden_paths_finding(diff, plan)]
