"""Scope-limited git for the Coder session (WSC-E3-T2).

Two enforcement layers, independent of each other:

1. **Toolset** — `ScopedGitSession` is the ONLY way the sandbox code (the
   `run_coder_turn` Activity, never the LLM/substrate directly — see
   `activities.py`) writes to git. It exposes only `.commit()` and `.push()`;
   `.push()` has the refspec (`HEAD:refs/heads/<branch>`) *hardcoded* — there is
   no parameter to pass `--force` or another branch. There is no escape-hatch
   `run_git_command(*args)` method, and no `open_pull_request()`. The LLM never
   receives a git tool — it only edits files in the workspace (P1: no flow
   decision made by an LLM).

2. **Remote scope (server-side)** — the checkpoint "origin" (a local bare repo
   standing in for the real remote at this phase, see `git_checkpoint.py`) has a
   real `pre-receive` hook installed (`install_pre_receive_guard`) that rejects:
   (a) any ref other than the task's allowed branch, (b) any non-fast-forward
   update (force-push). This holds even if someone bypasses `ScopedGitSession`
   and runs a raw `git push --force` — the hook runs on the "server" side (the
   bare repo), not the client side, so it applies regardless of which code
   performed the push.

   In production (a real push to GitHub through the egress-proxy) the equivalent
   is the scope of the GitHub App token injected by the proxy
   (`egress_proxy.credentials.ScopedCredential`) — the token never carries the
   `pull_requests:write` permission, so a `gh pr create`/`POST /repos/.../pulls`
   attempt made from inside the sandbox fails on the token's own missing
   permission, not on the code's "good will". `ScopedCredential.create_pull_request()`
   (see `egress_proxy/credentials.py`) models exactly that, even in local
   fixture mode.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("sandbox_runtime.scoped_git")

#: O hook `pre-receive` se ANUNCIA em toda recusa (`PRE_RECEIVE_HOOK_TEMPLATE`).
#: É esse marcador — e não "o push falhou" — que distingue violação de escopo de
#: falha de mecânica do git.
_SCOPE_MARKER = "dse-scope:"


def _looks_like_scope_refusal(message: str) -> bool:
    return _SCOPE_MARKER in message

#: Hooks do repositório do cliente desligados NA LINHA DE COMANDO, em todo
#: comando que esta sessão executa.
#:
#: `_disable_repo_hooks()` já escreve `core.hooksPath` na config do workspace, e
#: isso continua ali de propósito (é o que a suíte de escopo verifica). Só que
#: config é reversível por quem vier depois: o gate L1 roda `npm ci`, o
#: `prepare` do cliente é `husky`, husky reaponta `core.hooksPath` para
#: `.husky/`, e a partir daí todo `git commit` desta sessão executa o `ng lint`
#: do cliente dentro do sandbox. Foi assim que o turno morreu em OOM a cada ~45s
#: em `wi_pr21`. `-c` vence config de repositório e não é desarmável por código
#: não confiável — é a diferença entre uma convenção e um controle.
#:
#: Não enfraquece a guarda `pre-receive` do bare repo de checkpoint: aquele hook
#: roda em `git-receive-pack`, que lê a config do PRÓPRIO bare repo. Medido —
#: um push com `-c core.hooksPath=/nonexistent` continua sendo rejeitado pela
#: guarda de escopo.
#:
#: Um caminho inexistente é no-op para o git; nada precisa existir no disco.
NO_CUSTOMER_HOOKS = ("-c", "core.hooksPath=/nonexistent/dse-no-hooks")

PRE_RECEIVE_HOOK_TEMPLATE = """#!/usr/bin/env python3
import sys

ALLOWED_REF = "refs/heads/{branch}"

def main():
    rejected = False
    for line in sys.stdin:
        old_sha, new_sha, refname = line.strip().split()
        if refname != ALLOWED_REF:
            sys.stderr.write(
                f"dse-scope: refused — ref {{refname}} outside the allowed branch "
                f"{{ALLOWED_REF}}\\n"
            )
            rejected = True
            continue
        is_force = (
            old_sha != "0" * 40
            and new_sha != "0" * 40
            and not _is_fast_forward(old_sha, new_sha)
        )
        if is_force:
            sys.stderr.write(
                "dse-scope: refused — non-fast-forward (force-push) blocked "
                "by the task scope\\n"
            )
            rejected = True
    if rejected:
        sys.exit(1)
    sys.exit(0)


def _is_fast_forward(old_sha, new_sha):
    import subprocess as sp

    try:
        merge_base = sp.check_output(
            ["git", "merge-base", "--is-ancestor", old_sha, new_sha]
        )
        return True
    except sp.CalledProcessError:
        return False


if __name__ == "__main__":
    main()
"""


class GitCommandError(RuntimeError):
    """A git command failed, carrying what git said about it."""


class GitScopeViolation(GitCommandError):
    """Raised when a git operation tries to leave the task's scope."""


def install_pre_receive_guard(bare_repo_path: str, allowed_branch: str) -> None:
    """Install a real `pre-receive` hook in the checkpoint bare repo, rejecting
    pushes outside the task branch or non-fast-forward (force) ones.
    Idempotent — it overwrites any existing hook."""
    hooks_dir = Path(bare_repo_path) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-receive"
    hook_path.write_text(PRE_RECEIVE_HOOK_TEMPLATE.format(branch=allowed_branch))
    hook_path.chmod(0o755)
    # Pin where this repo looks for hooks, in the repo's OWN config.
    #
    # `git-receive-pack` reads repo + global + system config, and repo-local
    # wins. Without this line the guard is disarmed by a single line of
    # untrusted code: the sandbox runs the customer's `postinstall`/`prepare`
    # under the same uid and HOME as our git commands, so
    # `git config --global core.hooksPath /tmp/anything` in their package.json
    # moves the lookup away from this directory and the push is accepted with
    # the hook never running. Measured: an out-of-scope ref was created, rc=0.
    #
    # A security control that untrusted input can switch off is not a control.
    subprocess.run(
        ["git", "-C", bare_repo_path, "config", "core.hooksPath", str(hooks_dir)],
        capture_output=True,
        text=True,
        check=True,
    )


def write_task_branch_marker(workspace_dir: str, branch: str) -> None:
    """plan 08 §F (F6) — writes the `.dse-task-branch` marker (used by
    resume/checkpoint to rediscover the task branch) AND excludes it from EVERY
    commit via `.git/info/exclude`. The marker is DSE infrastructure — it must
    not leak into the customer's PR. Because `commit()` uses `--allow-empty`, the
    initial commit (empty workspace) still creates a valid HEAD even with its
    only file excluded. The exclude is best-effort (if it fails, the marker still
    exists and resume works; only the PR could end up carrying it)."""
    ws = Path(workspace_dir)
    exclude = ws / ".git" / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        if ".dse-task-branch" not in existing.split():
            prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
            exclude.write_text(prefix + ".dse-task-branch\n")
    except OSError:
        pass  # best-effort — never fail the clone/checkpoint because of the exclude
    (ws / ".dse-task-branch").write_text(branch)


@dataclass
class ScopedGitSession:
    """The only git write surface available to the `run_coder_turn` Activity.
    It exposes no force-push, no PR creation, and no generic `run(*args)`."""

    workspace_dir: str
    branch: str
    remote_name: str = "origin"

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        """Raises with git's OWN message, not just the exit code.

        `check=True` raises CalledProcessError, whose str() is
        "Command '[...]' returned non-zero exit status 1." — the stderr is
        captured and then thrown away. A real failure on the VPS surfaced as
        exactly that string, fourteen times, and the diagnosis had to be guessed
        from it. It was guessed wrong. Whatever git actually printed is the
        cheapest evidence in the system and it was being discarded.

        Todo comando sai com `NO_CUSTOMER_HOOKS` na frente: este wrapper recebe
        o subcomando de fora, então um `commit`/`push`/`checkout` novo chega aqui
        sem passar por revisão de segurança nenhuma."""
        proc = subprocess.run(
            ["git", *NO_CUSTOMER_HOOKS, *args],
            cwd=self.workspace_dir,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise GitCommandError(
                f"git {' '.join(args)} failed (exit {proc.returncode}) in "
                f"{self.workspace_dir}: {detail or '<no output>'}"
            )
        return proc

    def ensure_identity(self, name: str = "dse-coder", email: str = "coder@dse.local") -> None:
        """Make this workspace usable for DSE commits: an identity, and none of
        the repository's own hooks."""
        self._run(["config", "user.name", name])
        self._run(["config", "user.email", email])
        self._disable_repo_hooks()

    def _disable_repo_hooks(self) -> None:
        """Point `core.hooksPath` at an empty directory, so no hook the CUSTOMER
        ships ever runs on a DSE commit.

        A checkpoint commit is our infrastructure, not a developer's commit, and
        it must not execute code from the repository being worked on. On the
        Angular testbed this was not theoretical: the L1 gate runs `npm ci`, the
        repo's `prepare` script is `husky`, and husky installs `.husky/` as the
        hooks path — so every subsequent `git commit` ran the project's `ng lint`
        INSIDE the sandbox, where it exhausted the V8 heap and died. The
        turn-start checkpoint could then never be written, and since the fix loop
        rebuilds and retries, every attempt re-ran the same OOM. Seen climbing
        one retry per ~45s on wi_pr21.

        This is set on the WORKSPACE repo only. The scope `pre-receive` guard
        lives in the checkpoint bare repo and runs in `git-receive-pack`, which
        reads that repo's own config — so this cannot weaken it. There is a test
        that holds this apart."""
        hooks_dir = Path(self.workspace_dir) / ".git" / "dse-empty-hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        self._run(["config", "core.hooksPath", str(hooks_dir)])

    def has_changes(self) -> bool:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.workspace_dir,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())

    def commit(self, message: str) -> str:
        self._run(["add", "-A"])
        self._run(["commit", "-m", message, "--allow-empty"])
        sha = self._run(["rev-parse", "HEAD"]).stdout.strip()
        return sha

    def unshallow_if_needed(self) -> None:
        """Um clone raso não pode ser empurrado para o checkpoint.

        Medido 2026-08-18 no primeiro repo de cliente com histórico real (147
        commits): o clone é `--depth 50`, o push manda linhas `shallow` e o
        `git-receive-pack` do bare repo recusa com `shallow update not
        allowed`. Repo com menos commits que a profundidade recebe o histórico
        inteiro, não tem fronteira e sempre funcionou — por isso o defeito
        ficou invisível durante todo o POC.

        O atalho seria `receive.shallowUpdate=true` no bare repo. Recusado: o
        checkpoint viraria raso e `_is_fast_forward` decide por `merge-base
        --is-ancestor`, cuja resposta muda ao atravessar fronteira rasa — a
        guarda anti-force-push ficaria mais fraca em silêncio. Preenchemos o
        histórico uma vez, e a guarda continua exata.
        """
        if not (Path(self.workspace_dir) / ".git" / "shallow").is_file():
            return
        # `origin` neste ponto já pode ser o checkpoint (que não tem o
        # histórico): o unshallow tem que ir ao remote de ONDE veio o clone.
        for remote in ("upstream", "origin"):
            try:
                self._run(["fetch", "--unshallow", remote])
                return
            except GitCommandError:
                continue
        logger.warning(
            "workspace is shallow and could not be deepened; the push to the "
            "checkpoint will be refused with 'shallow update not allowed'"
        )

    def push(self) -> None:
        """Push hardcoded to `HEAD:refs/heads/<branch>` on the configured remote
        — there is no way to pass `--force` or another refspec through this API.
        Server-side `pre-receive` hook failures propagate as `GitScopeViolation`
        (P6: clean failure, not swallowed)."""
        self.unshallow_if_needed()
        try:
            self._run(["push", self.remote_name, f"HEAD:refs/heads/{self.branch}"])
        except GitCommandError as e:
            # `_run` used to raise CalledProcessError and this caught that. When
            # it started raising GitCommandError the conversion stopped matching,
            # so a push REFUSED BY THE SCOPE HOOK would have escaped as a plain
            # git failure instead of the scope violation callers act on — a
            # security guarantee turned into a generic error by a refactor.
            #
            # A correção passou do ponto na direção oposta: TODA falha de git
            # virava violação de escopo. Um push recusado por mecânica (shallow,
            # remote inexistente, disco cheio) mandava a investigação para
            # credencial e allowlist — custou uma auditoria inteira em
            # 2026-08-18. Violação de escopo é o que o HOOK recusa; ele se
            # anuncia, e é só isso que reetiquetamos.
            if _looks_like_scope_refusal(str(e)):
                raise GitScopeViolation(f"push refused by the remote (scope): {e}") from e
            raise

    def current_sha(self) -> str:
        return self._run(["rev-parse", "HEAD"]).stdout.strip()

    def files_changed_against(self, base_sha: str) -> list[str]:
        result = self._run(["diff", "--name-only", base_sha, "HEAD"])
        return [line for line in result.stdout.splitlines() if line]


# "Safe toolset" signature: used by the adversarial test to prove that
# ScopedGitSession's public API contains no force-push / PR / generic-command
# escape hatch.
FORBIDDEN_METHOD_NAMES = {
    "force_push",
    "push_force",
    "create_pull_request",
    "open_pr",
    "run_git_command",
    "run",
    "exec",
}
