"""WSE-E1 — joins the 3 sub-tasks (T1 lint/typecheck/test/build, T2 SAST +
secret-scan, T3 diff vs PlanArtifact) into a single `L1Result`.

Failure on any check => `L1Result.passed=False`. Phase 1 has no L2 — a failure
here goes back to the Coder by decision of the WS-B workflow; this module only
reports pass/fail with evidence, it never decides what to do next (P1)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime as _datetime, timezone as _timezone

from dse_contracts import GateStatus, L1Finding, L1Result, PlanArtifact

from dse_validation import db
from dse_validation.config import L1Config
from dse_validation.l1 import plan_compliance, quality_checks, sast, secret_scan
from dse_validation.sandbox_exec import SandboxExecutor

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover - defensive, dse_audit is always installed in venv-wse
    audit_emit = None


def _timed(step, name: str, produce):
    """Run one gate, and record what it cost.

    The pipeline reported which stage was running — the heartbeat carries that —
    but nothing durable said how long any of them took. So `run_l1_pipeline`
    was 638 seconds of which 55 were explainable (`npm ci` said so itself in its
    own output) and 583 were not, and every proposal to speed the gate up was a
    guess about which stage to attack."""
    import time as _time

    step(name)
    t0 = _time.monotonic()
    finding = produce()
    elapsed = _time.monotonic() - t0
    if isinstance(finding, list):
        for f in finding:
            f.duration_seconds = round(elapsed / max(len(finding), 1), 2)
        return finding
    finding.duration_seconds = round(elapsed, 2)
    return finding


def _ignore_step(_name: str) -> None:
    """Default `on_step`: the core stays a plain synchronous function for the
    tests and for any caller that has nothing to report progress to."""


#: What an operator sees when a gate failed and its branch declared no summary.
#: It is deliberately useless: silence here means a NEW branch forgot to author
#: one, and the operator should read `validation_runs`, not be handed whatever
#: that branch happened to interpolate.
_NO_SUMMARY = "(the check reported no publishable reason — see validation_runs)"


def _audit_safe_summary(summary: str | None) -> str:
    """Bound and flatten a platform-authored summary for the ledger.

    `summary` is already value-free by construction (see `L1Finding.summary`);
    this only stops a malformed one from being unbounded or multi-line, because
    `audit_log` is append-only and a bad write can never be amended."""
    first = (summary or "").replace("\x00", "").splitlines()
    return first[0][:600] if first else ""


def run_l1_pipeline_core(
    executor: SandboxExecutor,
    work_item_id: str,
    tenant_id: str,
    plan: PlanArtifact,
    base_sha: str,
    head_sha: str,
    target_dir: str = ".",
    cfg: L1Config | None = None,
    actor: str = "system:validation",
    persist: bool = True,
    on_step: Callable[[str], None] | None = None,
) -> L1Result:
    # `on_step` is called with the name of each stage BEFORE that stage runs.
    # This is the pipeline's only progress signal, and the Activity wrapper needs
    # it for two things it cannot get any other way (see `activities.py`):
    #   1. the Temporal heartbeat carries the stage that is ACTUALLY running, so
    #      an L1 that dies reads "test, 412s in" instead of "the Activity died";
    #   2. it is the only point where an already-cancelled Activity can stop the
    #      pipeline — `asyncio.to_thread` cannot interrupt this thread, so the
    #      wrapper's callback raises here rather than letting the remaining
    #      checks keep burning the sandbox's single vCPU.
    # Anything raised by `on_step` therefore propagates on purpose.
    step = on_step or _ignore_step

    step("l1_manifest")
    cfg = cfg or L1Config.from_trusted_manifest(executor, base_sha)

    findings: list[L1Finding] = []
    if cfg.manifest_status != GateStatus.PASS:
        findings.append(
            L1Finding(
                check="l1_manifest",
                passed=False,
                status=cfg.manifest_status,
                detail=cfg.manifest_detail,
                summary=cfg.manifest_summary,
            )
        )
    # The diff is computed FIRST, because the per-file gates are scoped by it.
    # A gate that judges the whole repository judges the customer's history:
    # measured on the Angular testbed, `tsc --noEmit` reported 262 errors, all
    # in `.spec.ts` files the DSE never opened, against a change that added one
    # CONTRIBUTING.md. The work item failed for them, the fix loop spent paid
    # Coder turns repairing someone else's specs, and no number of rounds could
    # have passed. `None` on failure means "no diff available" and the gates
    # fall back to judging everything — losing a real finding is worse than
    # reporting one that is not ours.
    step("diff")
    diff = plan_compliance.compute_diff_or_none(executor, base_sha, head_sha)
    changed_files = None if diff is None else {f.lstrip("./") for f in diff.files_changed}

    # A janela começa AQUI: se o lint morrer sem diagnóstico legível, o que o
    # egress recusou desde este instante é a única pista que a plataforma tem
    # sobre a causa — e ela custou três releases para chegar ao operador uma
    # vez (o P2 do Eclipse consulta dois hosts; abrir um deixa o erro idêntico).
    _lint_inicio = _datetime.now(_timezone.utc)
    findings.append(_timed(step, "lint", lambda: quality_checks.lint_check(
        executor, cfg, changed_files,
        egress_denials=lambda: db.egress_denials_since(_lint_inicio),
    )))
    findings.append(_timed(step, "typecheck", lambda: quality_checks.typecheck_check(executor, cfg, changed_files)))
    findings.append(_timed(step, "test", lambda: quality_checks.test_check(
        executor, cfg, changed_files, base_sha=base_sha)))
    findings.append(_timed(step, "build", lambda: quality_checks.build_check(executor, cfg, changed_files)))
    findings.append(_timed(step, "sast", lambda: sast.sast_check(
        executor, target_dir, cfg.sast_severity_gate, cfg.timeout_for("sast")
    )))
    findings.append(_timed(step, "secret_scan", lambda: secret_scan.secret_scan_check(
        executor, target_dir, cfg.timeout_for("secret_scan")
    )))
    findings.extend(_timed(step, "plan_compliance", lambda: plan_compliance.plan_compliance_findings(
        executor, plan, base_sha, head_sha, diff=diff
    )))

    passed = all(f.passed for f in findings)
    result = L1Result(
        work_item_id=work_item_id,
        passed=passed,
        findings=findings,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    step("persist")
    if persist:
        db.record_validation_run(
            work_item_id, tenant_id, passed, [f.model_dump() for f in findings]
        )
    if audit_emit is not None:
        audit_emit(
            actor=actor,
            action="l1_pipeline_run",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={
                "passed": passed,
                "status": result.status.value if result.status else None,
                "base_sha": base_sha,
                "head_sha": head_sha,
                "checks": {
                    f.check: f.status.value if f.status else None for f in findings
                },
                # Durations in the ledger, not only in the logs: "which stage is
                # slow" has to be answerable from a run that already finished,
                # by whoever asks next, without a live heartbeat.
                "durations": {f.check: f.duration_seconds for f in findings},
                # WHY, not just WHICH — from `summary` and never from `detail`.
                #
                # A gate reading ERROR with no reason anywhere is a gate nobody
                # can debug; diagnosing one cost two wrong guesses before anyone
                # noticed the reason was discarded at write time. But `detail`
                # carries what the gate SAW — scanner output, compiler output,
                # matched source lines — and audit_log is the one store that
                # cannot take that: append-only (REVOKE plus the 0028 trigger),
                # refused by retention.py by design, and copied verbatim into
                # console_rm.timeline_events.data by the projector. A value
                # written here can be rotated, never scrubbed.
                #
                # The first attempt published `detail`'s first line and skipped
                # the checks named "secret_scan" and "sast". That was wrong in
                # BOTH directions, which is why the rule now lives on the
                # branch: it silenced sast's ERROR reason — the very incident
                # this exists for — while still publishing lint's exit-127
                # branch, whose first line is raw stderr from a command the
                # customer's own manifest chose. A denylist of check names has
                # to predict every future branch that forgets to prepend a
                # summary; it had already missed four.
                #
                # `detail` keeps living in validation_runs, which is where
                # debugging happens and which retention can still clean.
                "failures": {
                    f.check: _audit_safe_summary(f.summary) or _NO_SUMMARY
                    for f in findings
                    if not f.passed
                },
                # Vermelho HERDADO do repositório, em CONTAGEM (os nomes são
                # caminhos de arquivo e ficam no `detail`/validation_runs, pela
                # mesma razão da nota acima: audit_log não se limpa). Sem esta
                # linha um gate que passou por NOT_OUR_FAILURE seria
                # indistinguível de um verde legítimo no ledger.
                "inherited_red": {
                    f.check: len(f.inherited_failures)
                    for f in findings
                    if f.inherited_failures
                },
            },
        )
    return result
