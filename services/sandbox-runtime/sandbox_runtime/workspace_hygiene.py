"""Deterministic post-turn workspace hygiene (P1) — VENDORABLE module.

Extracted from `activities.py` (Phase 1, plan 09) so it can run in TWO places
from a single source of truth: in the worker (Docker runtime, git over the bind
mount) and inside the agent-runner (K8s runtime, git in-pod via
`--op post_turn`). That is why the dependencies are stdlib + `dse_contracts`
only (both present in the runner image) — no docker/temporal/audit here; the
caller is the one that audits, using the returned lists.

The three operations and the real incidents that produced them:
  - `prune_disposable_artifacts`: the CLI creates unsolicited reports
    (BUG_FIX_REPORT.md) — deletes ONLY new disposable junk; legitimate new
    source outside the plan survives (expected_files is advisory since
    2026-07-22).
  - `restore_lockfile_churn`: npm rewrote 16 lines of package-lock.json with no
    change to the paired manifest, and diff_budget failed the task.

Havia uma terceira, `revert_test_edits`, REMOVIDA em 2026-08-10 com o reauthor.
Ela nasceu do issue #1 (o Coder editou um seed compartilhado e quebrou um teste
irmão) e revertia toda edição de teste; na rc.76 passou a reverter só o
INSTRUMENTO do laço. Sair inteira foi decisão de operador: o DSE pode alterar
qualquer teste, e a supervisão é o diff da PR. Enquanto ela existiu, o Coder não
podia consertar a spec do Tester — e era isso que obrigava o `reauthor`, o
parque `spec_conflict` e todo o encanamento de veredito a existirem.

A proteção do issue #1 não sumiu, mudou de lugar: o L1 reprova no mesmo turno
e a mudança aparece no diff da PR.
"""
from __future__ import annotations

import logging
import os
import subprocess

from dse_contracts.paths import (
    is_disposable_artifact,
    is_test_path,
    lockfile_manifest_for,
)

try:
    from .scoped_git import NO_CUSTOMER_HOOKS
except ImportError:  # vendorizado na imagem do agent-runner: scoped_git vira _scoped_git
    from ._scoped_git import NO_CUSTOMER_HOOKS  # type: ignore[no-redef]

logger = logging.getLogger("sandbox_runtime.workspace_hygiene")


def prune_disposable_artifacts(
    workspace_dir: str, expected_files: list[str], work_item_id: str
) -> tuple[list[str], list[str]]:
    """Delete NEW (untracked) files that are obvious CLI JUNK before the commit.
    Never touches: what the plan asked for, tests, or the work item's demo.
    Best-effort (L1 is the hard gate). Returns `(pruned, kept_out_of_plan)`."""
    try:
        # `-uall`: list EVERY untracked path individually (without collapsing a
        # new directory into `?? src/`); .gitignore is still respected.
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "-uall"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — prune is best-effort; L1 is the hard gate
        logger.warning("post-coder prune failed (git status); L1 remains the gate")
        return [], []

    expected = set(expected_files)
    pruned: list[str] = []
    kept_out_of_plan: list[str] = []
    for line in porcelain.splitlines():
        if not line.startswith("??"):
            continue
        rel = line[3:].strip().strip('"')
        if rel in expected or is_test_path(rel) or rel.startswith(f"demos/{work_item_id}"):
            continue
        if not is_disposable_artifact(rel):
            kept_out_of_plan.append(rel)  # legitimate new source outside the plan — STAYS
            continue
        try:
            os.remove(os.path.join(workspace_dir, rel))
            pruned.append(rel)
        except OSError:
            pass
    return pruned, kept_out_of_plan


def restore_lockfile_churn(workspace_dir: str) -> list[str]:
    """The lockfile changed but the paired manifest (package.json…) did NOT →
    restore (if modified) or remove (if new). Returns the paths handled."""
    try:
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — best-effort; L1 has the same exemption
        return []
    status_by_path: dict[str, str] = {}
    for line in porcelain.splitlines():
        if len(line) > 3:
            status_by_path[line[3:].strip().strip('"')] = line[:2]
    restored: list[str] = []
    for rel, st in sorted(status_by_path.items()):
        manifest = lockfile_manifest_for(rel)
        if manifest is None or manifest in status_by_path:
            continue
        try:
            if st == "??":
                os.remove(os.path.join(workspace_dir, rel))
                restored.append(rel)
            else:
                proc = subprocess.run(
                    ["git", *NO_CUSTOMER_HOOKS, "checkout", "--", rel], cwd=workspace_dir,
                    capture_output=True, text=True, timeout=30,
                )
                if proc.returncode == 0:
                    restored.append(rel)
        except OSError:
            continue
    return restored
