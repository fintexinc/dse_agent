"""WSE-E1-T3 — final diff vs PlanArtifact: touched files vs expected_files,
diff_budget_lines, forbidden_paths. Real `git diff --numstat` against the actual
git repo from the `git_repo` fixture (nothing mocked)."""
from __future__ import annotations

import subprocess

import pytest

from dse_contracts import GateStatus, PlanArtifact

from dse_validation.l1.plan_compliance import (
    DiffComputationError,
    compute_diff_summary,
    diff_budget_finding,
    forbidden_paths_finding,
    plan_compliance_findings,
)


def test_diff_within_budget_and_expected_files_passes(sandbox, feature_branch, git_sha):
    feature_branch("app.py", "def add(a, b):\n    return a + b  # small tweak\n")
    plan = PlanArtifact(
        work_item_id="wi1", expected_files=["app.py"], diff_budget_lines=50, forbidden_paths=[]
    )
    diff = compute_diff_summary(sandbox, git_sha("main"), git_sha())
    finding = diff_budget_finding(diff, plan)
    assert finding.check == "diff_budget"
    assert finding.passed is True




def test_diff_touching_unexpected_file_now_passes(sandbox, feature_branch, git_sha):
    """OPERATOR DECISION (3rd real run): a file outside expected_files NO LONGER
    fails the gate — the Planner predicts the file from the issue text, and in a
    bug-fix the defect lives in another layer; the Coder (the only one that reads
    the code) gets it right. Anti-sprawl is handled by the line budget +
    forbidden_paths."""
    feature_branch("unexpected_module.py", "x = 1\n")
    plan = PlanArtifact(
        work_item_id="wi3", expected_files=["app.py"], diff_budget_lines=400, forbidden_paths=[]
    )
    diff = compute_diff_summary(sandbox, git_sha("main"), git_sha())
    finding = diff_budget_finding(diff, plan)
    assert finding.passed is True, finding.detail


def test_diff_touching_forbidden_path_fails(sandbox, feature_branch, git_sha):
    # `expected_files` deixou de ser decoração aqui: desde 2026-08-19 um arquivo
    # protegido DECLARADO no plano é um arquivo que o humano autorizou no gate, e
    # o L1 o deixa passar (ver a seção "A PORTA", no fim do arquivo). Este teste
    # cobre o caso que continua reprovando — o arquivo que ninguém aprovou —, e
    # por isso o plano declara outra coisa.
    feature_branch("migrations/0099_evil.sql", "DROP TABLE audit_log;\n")
    plan = PlanArtifact(
        work_item_id="wi4",
        expected_files=["src/app.py"],
        diff_budget_lines=400,
    )
    diff = compute_diff_summary(sandbox, git_sha("main"), git_sha())
    finding = forbidden_paths_finding(diff, plan)
    assert finding.check == "forbidden_paths"
    assert finding.passed is False
    assert "migrations/" in finding.detail


def test_diff_not_touching_forbidden_path_passes(sandbox, feature_branch, git_sha):
    feature_branch("app.py", "def add(a, b):\n    return a + b  # tweak\n")
    plan = PlanArtifact(work_item_id="wi5", expected_files=["app.py"], diff_budget_lines=400)
    diff = compute_diff_summary(sandbox, git_sha("main"), git_sha())
    finding = forbidden_paths_finding(diff, plan)
    assert finding.passed is True


def test_forbidden_path_nested_in_a_monorepo_is_blocked(sandbox, feature_branch, git_sha):
    """Exit-criterion assertion A4. The matcher used to be a root-anchored
    prefix, so in a monorepo the SHIPPED defaults matched nothing and a PR
    touching a package's own CI workflow walked through L1."""
    feature_branch("packages/web/.github/workflows/ci.yml", "on: push\njobs: {}\n")
    plan = PlanArtifact(work_item_id="wi-monorepo")  # shipped forbidden_paths
    diff = compute_diff_summary(sandbox, git_sha("main"), git_sha())
    finding = forbidden_paths_finding(diff, plan)
    assert finding.passed is False, finding.detail
    assert ".github/workflows/" in finding.detail


def test_directory_named_like_a_forbidden_one_is_not_blocked(sandbox, feature_branch, git_sha):
    """`migrations_backup/` is not `migrations/`: matching at any depth must not
    trade the old false negative for a false positive (the old `startswith` had
    this bug already, at the root)."""
    feature_branch("services/api/migrations_backup/0001.sql", "SELECT 1;\n")
    plan = PlanArtifact(work_item_id="wi-backup")  # shipped forbidden_paths
    diff = compute_diff_summary(sandbox, git_sha("main"), git_sha())
    assert forbidden_paths_finding(diff, plan).passed is True


def _lines(prefix: str, count: int) -> str:
    return "\n".join(f"{prefix}_{i} = {i}" for i in range(count)) + "\n"


def test_tests_written_by_the_tester_do_not_spend_the_coder_s_budget(
    sandbox, feature_branch, git_sha
):
    """Measured on a real run: 379 of the 400 lines, and 218 of them (57.5% of
    the budget) were the two test files the TESTER wrote. The gate exists to
    contain the Coder, and another agent was blowing it."""
    feature_branch("app.py", _lines("prod", 40))
    feature_branch("tests/test_generated.py", _lines("assert_x", 200))
    plan = PlanArtifact(work_item_id="wi-tester", diff_budget_lines=50, forbidden_paths=[])
    diff = compute_diff_summary(sandbox, git_sha("main"), git_sha())
    finding = diff_budget_finding(diff, plan)

    assert diff.total_lines_changed > 50  # the raw diff IS over the budget
    assert finding.passed is True, finding.detail
    assert f"{diff.total_lines_changed} lines total" in finding.detail  # still reported






def test_plan_compliance_findings_returns_exactly_two_findings(
    sandbox, feature_branch, git_sha
):
    feature_branch("app.py", "def add(a, b):\n    return a + b  # tweak2\n")
    plan = PlanArtifact(work_item_id="wi6", expected_files=["app.py"], diff_budget_lines=400)
    findings = plan_compliance_findings(sandbox, plan, git_sha("main"), git_sha())
    checks = {f.check for f in findings}
    assert checks == {"diff_budget", "forbidden_paths"}
    assert all(f.passed for f in findings)


def test_diff_uses_immutable_sha_when_local_main_is_absent(
    sandbox, feature_branch, git_repo, git_sha
):
    base_sha = git_sha("main")
    feature_branch("app.py", "def add(a, b):\n    return a + b + 0\n")
    head_sha = git_sha()
    subprocess.run(
        ["git", "branch", "-D", "main"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    )

    diff = compute_diff_summary(sandbox, base_sha, head_sha)

    assert diff.files_changed == ["app.py"]


def test_symbolic_git_ref_is_rejected(sandbox, git_sha):
    with pytest.raises(DiffComputationError, match="full Git SHA"):
        compute_diff_summary(sandbox, "main", git_sha())


def test_empty_expected_files_passes_when_within_budget(sandbox, git_sha):
    """An empty expected_files is no longer NOT_CONFIGURED at the gate — an empty
    plan is blocked in the WORKFLOW (patch reject-empty-expected-files-v1), before
    L1. Here the diff is empty (0 lines) → it passes on budget."""
    plan = PlanArtifact(work_item_id="wi-empty", expected_files=[])
    diff = compute_diff_summary(sandbox, git_sha(), git_sha())
    assert diff_budget_finding(diff, plan).passed is True


def test_no_code_change_explicitly_allows_empty_plan_on_empty_diff(sandbox, git_sha):
    plan = PlanArtifact(work_item_id="wi-doc", expected_files=[], no_code_change=True)
    diff = compute_diff_summary(sandbox, git_sha(), git_sha())

    assert diff_budget_finding(diff, plan).passed is True


def test_no_code_change_but_diff_present_fails(sandbox, feature_branch, git_sha):
    """A real inconsistency: the plan says there is no change, but the diff changed."""
    feature_branch("app.py", "x = 1\n")
    plan = PlanArtifact(work_item_id="wi-nc", expected_files=[], no_code_change=True)
    diff = compute_diff_summary(sandbox, git_sha("main"), git_sha())
    finding = diff_budget_finding(diff, plan)
    assert finding.passed is False
    assert finding.status == GateStatus.FAIL


def _mk_diff(files: list[str], lines: int = 20):
    from dse_validation.l1.plan_compliance import DiffSummary

    return DiffSummary(
        files_changed=files, total_lines_changed=lines,
        base_sha="a" * 40, head_sha="b" * 40,
    )


def test_unexpected_source_file_passes_under_new_policy():
    """3rd real run: the Coder fixed src/store.js while the plan predicted
    server.js (the Planner guesses the file from the issue TEXT, before reading the
    code). Under the new policy this PASSES — expected_files is advisory."""
    plan = PlanArtifact(
        work_item_id="wi_real", expected_files=["server.js", "test/api.test.js"],
        diff_budget_lines=400, forbidden_paths=[],
    )
    diff = _mk_diff(["src/store.js", "test/api.test.js"], lines=285)
    assert diff_budget_finding(diff, plan).passed is True




def test_root_anchored_pattern_matches_only_at_the_root():
    """The escape hatch for a name that is sensitive only at the top: a leading
    "/" pins the pattern to the repo root (.gitignore's own convention). Without
    it, `docs/` would now also cover `packages/web/docs/`."""
    plan = PlanArtifact(work_item_id="wi_rooted", forbidden_paths=["/docs/"])
    assert forbidden_paths_finding(_mk_diff(["packages/web/docs/index.md"]), plan).passed is True
    assert forbidden_paths_finding(_mk_diff(["docs/index.md"]), plan).passed is False


def test_lockfile_churn_passes_like_any_other_file():
    """Lockfile churn is already restored at RUNTIME before the commit; if one
    escapes, the gate does not fail per file (only on budget/forbidden)."""
    plan = PlanArtifact(
        work_item_id="wi_lock", expected_files=["src/store.js"],
        diff_budget_lines=400, forbidden_paths=[],
    )
    diff = _mk_diff(["src/store.js", "package-lock.json", "test/delete.test.js"])
    assert diff_budget_finding(diff, plan).passed is True


# Os quatro testes de TETO DE LINHAS que viviam aqui saíram em 2026-08-11:
# o tamanho do diff deixou de ser veredito (decisão de operador). O que
# sobrou daquele gate — a consistência `no_code_change` e o tamanho como
# INFORMAÇÃO — está pinado em test_diff_size_is_not_a_verdict.py, com o
# incidente que motivou a mudança escrito por extenso.


# ---------------------------------------------------------------------------
# A PORTA: caminho protegido que o humano autorizou no gate
# ---------------------------------------------------------------------------
# Medido 2026-08-19 (wi do `calculation-engine-service`, ~US$ 4 e 40 min até
# `coder_not_converging`): "adicione um workflow do GitHub Actions" era uma
# tarefa IMPOSSÍVEL POR CONSTRUÇÃO. O Planner declarava
# `expected_files=[".github/workflows/ci.yml"]` — corretíssimo —, a plataforma
# ANEXAVA `forbidden_paths=[".github/workflows/"]` ao mesmo plano, e o único
# diff que passaria neste gate era o diff que não entrega o pedido. Nem o
# Planner nem o Coder sabem que a lista existe: ela não está em nenhum prompt.
#
# A decisão do operador não foi apagar o gate nem deixar "a tarefa vencer": a
# colisão passa a PARAR NO GATE HUMANO (workflows.py, patch
# protected-paths-need-approval-v1), e aqui o L1 honra ESSA autorização — e só
# ela. O plano que chega ao L1 é o plano APROVADO (workflows.py passa
# `input.plan_json`, e o gate grava o `plan_hash`), então `expected_files` é
# literalmente a lista que o humano leu na mensagem de aprovação.
#
# O teste que mais importa nesta seção é o segundo: ele é o que impede a porta
# de virar buraco.


def test_a_protected_file_the_human_approved_is_allowed_through():
    plan = PlanArtifact(
        work_item_id="wi_ci",
        expected_files=[".github/workflows/ci.yml"],
    )  # forbidden_paths shipped: .github/workflows/
    finding = forbidden_paths_finding(_mk_diff([".github/workflows/ci.yml"]), plan)
    assert finding.passed is True, finding.detail
    assert "ci.yml" in finding.detail, "a isenção tem que ficar LEGÍVEL na evidência"


def test_a_protected_file_nobody_approved_still_fails():
    """O buraco que não pode existir: aprovar `ci.yml` não autoriza o Coder a
    escrever OUTRO arquivo sob o mesmo caminho protegido."""
    plan = PlanArtifact(
        work_item_id="wi_ci",
        expected_files=[".github/workflows/ci.yml"],
    )
    diff = _mk_diff([".github/workflows/ci.yml", ".github/workflows/release.yml"])
    finding = forbidden_paths_finding(diff, plan)
    assert finding.passed is False, finding.detail
    assert "release.yml" in finding.detail
    assert "ci.yml" not in finding.detail.split("release.yml")[0], (
        "o arquivo autorizado não pode aparecer como violação"
    )
    assert finding.summary == "1 file(s) under a path forbidden by the plan"


def test_approving_one_protected_file_does_not_approve_another_protected_area():
    plan = PlanArtifact(
        work_item_id="wi_ci", expected_files=[".github/workflows/ci.yml"]
    )
    assert forbidden_paths_finding(_mk_diff(["migrations/0001.sql"]), plan).passed is False


def test_the_authorisation_reaches_a_monorepo_package():
    """Mesma normalização do matcher, à mesma profundidade em que ele acusa."""
    plan = PlanArtifact(
        work_item_id="wi_mono",
        expected_files=["packages/web/.github/workflows/ci.yml"],
    )
    diff = _mk_diff(["packages/web/.github/workflows/ci.yml"])
    assert forbidden_paths_finding(diff, plan).passed is True


def test_a_declared_directory_authorises_what_is_under_it():
    """Quando o plano declara um DIRETÓRIO (barra no fim), é isso que o humano
    leu no gate — e é isso que ele autorizou. Sem esta regra, um plano que
    declara `.github/workflows/` recria exatamente a armadilha de origem."""
    plan = PlanArtifact(
        work_item_id="wi_dir", expected_files=[".github/workflows/"]
    )
    diff = _mk_diff([".github/workflows/ci.yml", ".github/workflows/release.yml"])
    assert forbidden_paths_finding(diff, plan).passed is True


def test_a_declared_file_does_not_authorise_its_siblings():
    """A contrapartida do teste acima: SEM a barra, a autorização é do arquivo,
    não da pasta dele."""
    plan = PlanArtifact(
        work_item_id="wi_file", expected_files=["migrations/0001_init.sql"]
    )
    diff = _mk_diff(["migrations/0002_drop_audit.sql"])
    assert forbidden_paths_finding(diff, plan).passed is False


def test_without_a_protected_file_in_the_plan_nothing_changes():
    """Rede de segurança: o gate de sempre, para o diff de sempre."""
    plan = PlanArtifact(work_item_id="wi_plain", expected_files=["src/app.py"])
    assert forbidden_paths_finding(_mk_diff(["src/app.py"]), plan).passed is True
    assert forbidden_paths_finding(_mk_diff(["migrations/0001.sql"]), plan).passed is False
