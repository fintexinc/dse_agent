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
  - `revert_test_edits`: the Coder edited a shared test seed and broke a sibling
    test (issue #1). Originally EVERY test edit reverted; desde a decisão de
    operador de 2026-08-10, o revert protege apenas o INSTRUMENTO do laço — as
    specs que o Tester autorou (autoria via histórico git, mesmo oráculo do
    reauthor). Edição de spec pré-existente do CLIENTE sobrevive e entra no
    diff da PR, onde o revisor humano a vê.
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


def _has_tester_marker(rel: str) -> bool:
    """O marcador de nome do Tester (`-dse`/`_dse`) — o fallback de autoria
    quando não há histórico git (arquivo novo, git indisponível).

    É uma CONVENÇÃO DE SUFIXO do stem, não uma substring: a regra antiga casava
    `-dse` em qualquer posição do nome, então uma spec de cliente chamada
    `parse-dserializer.spec.ts` era tratada como instrumento e revertida."""
    name = rel.rsplit("/", 1)[-1]
    stem = name.split(".", 1)[0]
    return stem.endswith("-dse") or stem.endswith("_dse")


def _is_tester_instrument(
    workspace_dir: str, rel: str, work_item_id: str | None = None
) -> bool:
    """O arquivo é o INSTRUMENTO DESTE laço — algo que o Tester escreveu AQUI?

    A pergunta certa é sobre ESTE item, não sobre a plataforma em geral, e o
    repositório já carrega a resposta: todo commit da plataforma é
    `<stage>(<work_item_id>): …` (`scoped_git.commit`). Perguntar pelo id deste
    item é preciso e — o que importa — **imune à profundidade do clone**.

    O que a regra anterior ("todos os commits são de plataforma E algum é
    `tester(`") errava: o clone do sandbox é `--depth 50`, então numa spec de
    CLIENTE cujos commits humanos ficaram fora da janela o `git log` mostra
    apenas o `tester(` recém-criado — e a edição do Coder era revertida em
    silêncio, contra a política de 2026-08-10. Defeito latente que a autonomia
    de edição desta sessão só faria disparar com mais frequência.

    Polaridade preservada (wi_fadd43185): `coder(` também é subject de
    plataforma, mas arquivo escrito pelo próprio Coder entre turnos NÃO é
    instrumento — revertê-lo mata o trabalho dele. Sem histórico, sem git, ou
    sem saber o id: o marcador de nome decide."""
    if work_item_id:
        try:
            out = subprocess.run(
                ["git", "log", "--format=%s", "--", rel],
                cwd=workspace_dir, capture_output=True, text=True, timeout=15,
            )
            subjects = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        except Exception:  # noqa: BLE001 — git indisponível: o marcador decide
            subjects = []
        if subjects:
            return any(s.startswith(f"tester({work_item_id})") for s in subjects)
    return _has_tester_marker(rel)


def revert_test_edits(
    workspace_dir: str, turn_start_sha: str, work_item_id: str | None = None
) -> list[str]:
    """Revert Coder changes to the tests the LOOP ITSELF authored — and only
    those. Best-effort. Returns the reverted paths.

    Decisão de operador (2026-08-10): specs pré-existentes do CLIENTE são
    editáveis pelo Coder — a mudança entra no diff da PR e o revisor humano a
    vê (o gate "o DSE nunca aprova o próprio trabalho" segue na PR). O que
    permanece intocável pelo Coder é o INSTRUMENTO DE VERIFICAÇÃO do laço: as
    specs que o Tester autorou (histórico git só com subjects da plataforma,
    ou marcador `-dse` sem histórico) — sem isso o Coder pode enfraquecer no
    meio do laço o teste que valida o próprio código. Arquivo NOVO de teste do
    Coder em convenção de cliente é engenharia legítima e fica; arquivo novo
    FORJANDO o marcador do Tester é removido."""
    try:
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "-uall"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("revert of the coder's tests failed (git status)")
        return []

    reverted: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        status, rel = line[:2], line[3:].strip().strip('"')
        if "->" in rel:  # rename: "old -> new" — take the destination
            rel = rel.split("->")[-1].strip()
        if not is_test_path(rel):
            continue
        try:
            if status.strip() == "??":
                # Novo e sem história: só o marcador denuncia forja de autoria.
                if not _has_tester_marker(rel):
                    continue
                os.remove(os.path.join(workspace_dir, rel))
                reverted.append(rel)
            else:
                if not _is_tester_instrument(workspace_dir, rel, work_item_id):
                    continue  # do cliente OU do próprio Coder: a edição sobrevive
                proc = subprocess.run(
                    ["git", *NO_CUSTOMER_HOOKS, "checkout", turn_start_sha, "--", rel], cwd=workspace_dir,
                    capture_output=True, text=True, timeout=30,
                )
                if proc.returncode == 0:
                    reverted.append(rel)
        except OSError:
            continue
    return reverted
