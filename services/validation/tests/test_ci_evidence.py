"""O fix de CI recebe o que o CI DISSE — nome do check, conclusão, link e o
fim do log do job.

Medido no wi_f1f27266 (2026-08-31): 8 rodadas de fix (~US$ 19) cujo texto
inteiro era "ci red: fix the pipeline", todas com `files_changed: []` — o
modelo, corretamente, não achou o que mudar. Os três checks vermelhos tinham
mensagens exatas ("Contract or generated client is stale. Run 'npm run
gen:contract'…", "Configuration key AUTH_ISSUER does not exist"); nenhuma
cruzou para o turno.

Best-effort por construção: o log do job exige `actions:read` no App (não
obrigatório); sem ele, as annotations do check run (que o App já lê) e, sem
elas, só nome/conclusão/link. Nunca bloqueia; sempre diz de onde veio.
"""
from __future__ import annotations

from dse_validation.github.ci_evidence import fetch_ci_failure_evidence_core
from dse_validation.github.client import FakeGitHubClient

_RUNS = [
    {"id": 11, "name": "lint", "status": "completed", "conclusion": "success",
     "html_url": "https://github.com/acme/repo/runs/11"},
    {"id": 22, "name": "unit (API)", "status": "completed", "conclusion": "failure",
     "html_url": "https://github.com/acme/repo/runs/22"},
    {"id": 33, "name": "leak gate", "status": "completed", "conclusion": "timed_out",
     "html_url": "https://github.com/acme/repo/runs/33"},
]


def _client() -> FakeGitHubClient:
    github = FakeGitHubClient()
    github.set_check_runs("acme/repo", "abc123", list(_RUNS))
    return github


def test_the_evidence_carries_the_log_tail_of_each_failing_check():
    github = _client()
    github.set_job_log("acme/repo", 22, "...\nFAIL src/contract/contract-drift.test.ts\nAssertionError: contract/openapi.json is stale\n")
    github.set_job_log("acme/repo", 33, "\n".join(f"line {i}" for i in range(500)))

    ev = fetch_ci_failure_evidence_core(github_client=github, work_item_id="wi_x", repo="acme/repo", ref="abc123")

    nomes = [c.name for c in ev.checks]
    assert nomes == ["unit (API)", "leak gate"], "só os vermelhos, na ordem do CI"
    api = ev.checks[0]
    assert api.conclusion == "failure" and api.url == "https://github.com/acme/repo/runs/22"
    assert "openapi.json is stale" in api.log_tail and api.source == "job_log"


def test_without_actions_read_it_falls_back_to_annotations_and_says_so():
    github = _client()
    github.job_logs_forbidden = True  # 403: o App não tem actions:read
    github.set_check_run_annotations("acme/repo", 22, [
        {"path": "contract/openapi.json", "annotation_level": "failure",
         "message": "Contract or generated client is stale. Run 'npm run gen:contract'."},
    ])

    ev = fetch_ci_failure_evidence_core(github_client=github, work_item_id="wi_x", repo="acme/repo", ref="abc123")

    api = next(c for c in ev.checks if c.name == "unit (API)")
    assert api.source == "annotations" and "gen:contract" in api.log_tail
    leak = next(c for c in ev.checks if c.name == "leak gate")
    assert leak.source == "none" and leak.log_tail == "", "sem log e sem annotation: diz que não veio"


def test_evidence_is_bounded_per_check():
    github = _client()
    github.set_job_log("acme/repo", 22, "\n".join(f"line {i}" for i in range(2000)))
    github.set_job_log("acme/repo", 33, "x" * 20_000)

    ev = fetch_ci_failure_evidence_core(github_client=github, work_item_id="wi_x", repo="acme/repo", ref="abc123")

    for c in ev.checks:
        assert len(c.log_tail) <= 4_000
        assert c.log_tail.count("\n") <= 80
    assert "line 1999" in ev.checks[0].log_tail, "é o FIM do log que importa"


def test_a_green_ref_yields_no_checks():
    github = FakeGitHubClient()
    github.set_check_runs("acme/repo", "abc123", [_RUNS[0]])
    ev = fetch_ci_failure_evidence_core(github_client=github, work_item_id="wi_x", repo="acme/repo", ref="abc123")
    assert ev.checks == []
