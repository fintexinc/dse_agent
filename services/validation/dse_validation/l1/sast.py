"""WSE-E1-T2 (part 1) — SAST via `bandit` (self-hostable, real, no license
cost) running inside the sandbox against the diff directory. Normalizes
bandit's JSON output into `L1Finding` (the same shape as the other checks)."""
from __future__ import annotations

import json

from dse_contracts import GateStatus, L1Finding

from dse_validation.config import default_scan_timeout_seconds
from dse_validation.l1.quality_checks import _infra_failure
from dse_validation.sandbox_exec import SandboxExecutor

_SEVERITY_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


#: The probe is one pruned `find` that stops at the first hit.
_PY_PROBE_TIMEOUT_SECONDS = 30


def sast_check(
    executor: SandboxExecutor,
    target_dir: str = ".",
    severity_gate: str = "MEDIUM",
    timeout: int | None = None,
) -> L1Finding:
    gate = _SEVERITY_ORDER.get(severity_gate.upper(), 2)
    # `None` — how the pipeline calls it — resolves the platform default instead
    # of the 120 s that used to be frozen in this signature, out of reach of both
    # the manifest and the environment. At the 20 files/s bandit measures inside
    # the sandbox pod, 120 s stops covering a repository at ~2.400 `.py`.
    if timeout is None:
        timeout = default_scan_timeout_seconds("sast")

    # bandit reads PYTHON. Pointed at a repository that has none it finds no
    # targets, exits 1 and prints no JSON — which this gate then reported as
    # "bandit did not produce valid JSON", failing the work item on the
    # Angular testbed for the crime of not being a Python project.
    #
    # Asking first is cheap and unambiguous: a tree with no `.py` in it has
    # nothing for a Python scanner to say. `find -prune` keeps the question
    # from walking node_modules to answer it.
    probe = executor.run(
        [
            "sh", "-c",
            "find . \\( -name node_modules -o -name .git -o -name .venv -o -name venv "
            "-o -name __pycache__ \\) -prune -o -name '*.py' -print -quit",
        ],
        timeout=_PY_PROBE_TIMEOUT_SECONDS,
    )
    if probe.returncode == 0 and not (probe.stdout or "").strip():
        return L1Finding(
            check="sast",
            passed=True,
            detail="not run: bandit scans Python and this repository has none",
            summary="not run: bandit scans Python and this repository has none",
        )

    result = executor.run(["bandit", "-r", target_dir, "-f", "json", "-q"], timeout=timeout)

    if result.returncode == 127:
        return L1Finding(
            check="sast",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"bandit not found: {result.stderr.strip()}",
            summary="bandit not found (exit 127)",
        )

    # A killed bandit prints NOTHING, and `json.loads("{}")` below yields zero
    # findings — i.e. a silent PASS on a security gate that never ran. It was
    # unreachable while 120 s was frozen in the signature; now that the budget
    # comes from the environment and the manifest, a short clock would BUY a
    # green SAST. Same shape as `secret_scan_check`: no verdict is an ERROR.
    if result.timed_out:
        return L1Finding(
            check="sast",
            passed=False,
            status=GateStatus.ERROR,
            detail=(
                f"bandit timed out after {timeout}s — no SAST verdict was produced "
                f"(scanning {target_dir}); raise the sast timeout or scan less"
            ),
            summary=(
                f"bandit timed out after {timeout}s — no SAST verdict was "
                "produced; raise the sast timeout or scan less"
            ),
        )

    # Infra ANTES do parse: um OOM ou um exec quebrado não são veredito sobre o
    # código do cliente, e a mensagem nomeada vale mais que "JSON inválido".
    # Este gate era o ÚNICO dos seis que não consultava `_infra_failure`.
    if (infra := _infra_failure(result)) is not None:
        return L1Finding(
            check="sast",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"bandit: {infra} (exit={result.returncode})\n{(result.stderr or '')[:2000]}",
            summary=f"bandit could not run: {infra}",
        )

    # bandit exits with returncode 1 when it finds issues (not an execution error).
    #
    # SEM `or "{}"` (2026-08-20). Aqueles seis caracteres eram o mecanismo
    # inteiro do falso verde que o comentário acima já descrevia: stdout vazio
    # virava `{}`, que virava zero findings, que virava PASS num gate de
    # segurança que não escaneou uma linha. O tratamento correto já existia
    # logo abaixo — bastava deixar a ausência de saída chegar até ele. Um
    # bandit que RODOU sempre imprime um objeto JSON.
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return L1Finding(
            check="sast",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"bandit did not produce valid JSON (exit={result.returncode}): {result.stderr[:2000]}",
            summary=f"bandit did not produce valid JSON (exit={result.returncode})",
        )

    findings = payload.get("results", [])
    gating = [
        f for f in findings if _SEVERITY_ORDER.get(f.get("issue_severity", "LOW"), 1) >= gate
    ]

    if not gating:
        detail = f"{len(findings)} SAST finding(s) in total, none >= {severity_gate}"
        return L1Finding(check="sast", passed=True, detail=detail, summary=detail)

    lines = [
        f"- [{f.get('issue_severity')}] {f.get('test_id')} {f.get('filename')}:{f.get('line_number')} — {f.get('issue_text')}"
        for f in gating[:20]
    ]
    summary = f"{len(gating)} SAST finding(s) >= {severity_gate}"
    # `lines` holds bandit's issue_text, and B105/B106/B107 render that as
    # "Possible hardcoded password: '<value>'" — the credential itself. It
    # stays in `detail`, which only reaches validation_runs.
    detail = summary + ":\n" + "\n".join(lines)
    return L1Finding(check="sast", passed=False, detail=detail, summary=summary)
