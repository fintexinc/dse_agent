"""Git ops executed INSIDE the sandbox (runner `--op bootstrap|checkpoint`).

On Docker the worker can still drive git through the bind mount; on K8s the
workspace is a Pod volume and EVERY git operation happens here, via exec. Both
routes use the SAME discipline as the worker (`scoped_git`): fixed refspec
`HEAD:refs/heads/<branch>`, never force, and the scope `pre-receive` hook
installed on the bare checkpoint repo BEFORE the first push — enforcement lives
on the remote, not on the caller's good will.

Single source of truth: sandbox_runtime's `scoped_git.py` is vendored into the
image at build time (`_scoped_git.py`); in dev/test the import resolves
straight from the package installed in the venv.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from dse_contracts import (
    CheckpointOpRequest,
    CheckpointOpResult,
    WorkspaceBootstrapRequest,
    WorkspaceBootstrapResult,
)

try:  # dev/test: worker package in the venv
    from sandbox_runtime.scoped_git import (
        NO_CUSTOMER_HOOKS,
        ScopedGitSession,
        install_pre_receive_guard,
        write_task_branch_marker,
    )
except ImportError:  # image: copy vendored at build time (Dockerfile)
    from ._scoped_git import (  # type: ignore[no-redef]
        NO_CUSTOMER_HOOKS,
        ScopedGitSession,
        install_pre_receive_guard,
        write_task_branch_marker,
    )


def _git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """`NO_CUSTOMER_HOOKS` na frente de tudo: este wrapper recebe o subcomando de
    fora, e roda DENTRO do sandbox, onde o repositório do cliente e os hooks que
    o `npm ci` dele instalou estão a um `core.hooksPath` de distância."""
    proc = subprocess.run(
        ["git", *NO_CUSTOMER_HOOKS, *args], cwd=cwd, capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc


def _ensure_safe_directory() -> None:
    """Bind mounts arrive owned by a uid != the sandbox uid; without this git
    refuses to operate ('dubious ownership'). Scope: only the ephemeral exec
    process."""
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", "*"],
        capture_output=True, text=True,
    )


def _configure_git_dependency_transport() -> None:
    """Ensina o git GLOBAL do pod a alcançar dependências git de github.com.

    Medido em wi_ef766cdd: `git+ssh://git@github.com/...` no package-lock — o
    npm invoca git, ssh não atravessa o egress-proxy, o pod não segura
    credencial por desenho (P2/ADR-12), e o `npm install` morre; sem
    node_modules o typecheck cai em `tsc: not found` (rc=127) e o laço escala
    culpando ninguém.

    A máquina já existia: o proxy TERMINA o `http://`, deriva o repo do PATH e
    injeta o installation token (auditado; o residual "qualquer repo da mesma
    instalação" está documentado no proxy). Aqui só se reescreve TODA forma de
    github.com para essa porta: ssh, scp-like e https (que viraria CONNECT
    opaco). O header é PLACEHOLDER — nenhuma credencial toca este container.
    Global de propósito, e isso conserta de passagem o residual antigo do
    clone ("um git fetch simples sai sem proxy"): o config vive em $HOME
    (/tmp, emptyDir) e morre com o Pod. Sem proxy no ambiente (docker/dev
    local), nada é escrito."""
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
    if not proxy:
        return
    pares = [
        ("url.http://github.com/.insteadOf", "ssh://git@github.com/"),
        ("url.http://github.com/.insteadOf", "git@github.com:"),
        ("url.http://github.com/.insteadOf", "https://github.com/"),
    ]
    for chave, valor in pares:
        subprocess.run(["git", "config", "--global", "--add", chave, valor],
                       capture_output=True, text=True)
    for chave, valor in (
        ("http.proxy", proxy),
        ("http.extraHeader", "X-Dse-Inject-Credential: github"),
        ("http.followRedirects", "false"),
    ):
        subprocess.run(["git", "config", "--global", chave, valor],
                       capture_output=True, text=True)


def _is_git_workspace(workspace_dir: str) -> bool:
    return (Path(workspace_dir) / ".git").exists()


def _checkpoint_has_branch(checkpoint_path: str, branch: str) -> bool:
    if not (Path(checkpoint_path) / "HEAD").is_file():
        return False
    proc = subprocess.run(
        ["git", "ls-remote", "--heads", checkpoint_path, branch],
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _clone_target_repo(req: WorkspaceBootstrapRequest) -> str:
    """Clones the customer's real repo INSIDE the Pod, through the egress-proxy.
    The URL carries NO credential, and the Pod never holds one.

    The scheme is `http://` on purpose, and it is the whole mechanism. Over
    `https://` git asks the proxy for a CONNECT tunnel, and a tunnel is opaque:
    the proxy can allow or deny it but cannot add an Authorization header, so a
    private repo answered `could not read Username for 'https://github.com'`
    after eight retries. Over `http://` the proxy TERMINATES the request, injects
    the installation token, and re-originates to github.com:443 over TLS
    (`tls_upgrade` on the allowlist entry). The plaintext hop is sandbox→proxy
    inside the Pod network; nothing leaves the cluster unencrypted, and the token
    exists only in the proxy's memory — never in this container's env, argv or
    git config.

    `http.extraHeader` is how the sandbox ASKS for injection without holding
    anything: it is a placeholder the proxy swaps for the real token.

    It then re-points `origin` at the local checkpoint (the GitHub URL disappears
    from the config), creates the task branch off the base and does the first
    scoped push. Returns the sha of the task tip."""
    url = f"http://{req.repo_host}/{req.repo}.git"
    # `http.proxy` is set EXPLICITLY rather than relying on the environment, and
    # this is not belt-and-braces — it is load-bearing. libcurl reads only the
    # LOWERCASE `http_proxy` for http:// URLs; the uppercase form it honours for
    # every other scheme is deliberately ignored here, because an inbound
    # `Proxy:` header would otherwise set it (httpoxy, CVE-2016-5385). The Pod
    # exports HTTP_PROXY uppercase, so relying on the env sent this clone
    # DIRECTLY to github.com:80 — no proxy, no credential, and GitHub's 301 to
    # https turned into a hard failure by followRedirects=false. Verified by
    # running git against a listener: uppercase 0 requests, lowercase 1.
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
    if not proxy:
        raise RuntimeError(
            "no HTTP proxy configured in the sandbox: the clone would go direct "
            "and no credential could be injected"
        )
    # --depth: shallow history is enough for the turn; pushing the tip to the
    # local checkpoint carries the objects that are needed.
    # As opções de transporte vivem no COMANDO (`-c`), não no `.git/config` —
    # de propósito: o token não pode ficar escrito no clone. A consequência é
    # que TODA operação de rede posterior precisa repeti-las; um `git fetch`
    # simples sai sem proxy e sem credencial, e o egress é default-deny.
    transporte = [
        "-c", f"http.proxy={proxy}",
        "-c", "http.extraHeader=X-Dse-Inject-Credential: github",
        "-c", f"http.extraHeader=X-Dse-Repo: {req.repo}",
        "-c", f"http.extraHeader=X-Dse-Branch: {req.branch}",
        # git would otherwise follow GitHub's 301 to https:// and land back on a
        # CONNECT tunnel with no credential — the exact failure being fixed.
        "-c", "http.followRedirects=false",
    ]
    _git([
        *transporte,
        "clone", "--depth", "50", "--branch", req.base_branch, url, req.workspace_dir,
    ])
    # Completa o histórico AQUI: daqui a poucas linhas o `origin` vira o
    # checkpoint local, que não tem o histórico do cliente — e um clone raso é
    # recusado pelo `git-receive-pack` do checkpoint com `shallow update not
    # allowed` (medido 2026-08-18, repo de 147 commits contra `--depth 50`).
    if (Path(req.workspace_dir) / ".git" / "shallow").is_file():
        _git([*transporte, "fetch", "--unshallow"], cwd=req.workspace_dir)
    session = ScopedGitSession(workspace_dir=req.workspace_dir, branch=req.branch)
    session.ensure_identity()
    # AQUI, e não na hora do push: daqui a três linhas o `origin` passa a ser o
    # checkpoint local, que não tem o histórico do cliente — depois disso não há
    # de onde completar o clone. Repo mais fundo que `--depth` empurra linhas
    # `shallow` e o receive-pack recusa (medido 2026-08-18, 147 commits).
    session.unshallow_if_needed()
    _git(["checkout", "-b", req.branch], cwd=req.workspace_dir)
    write_task_branch_marker(req.workspace_dir, req.branch)
    # origin becomes the checkpoint (never GitHub again) — the turn's
    # commit/push goes to the local bare repo, under the scope hook; the final
    # PR is opened deterministically by the Activity, never from here.
    _git(["remote", "set-url", "origin", req.checkpoint_path], cwd=req.workspace_dir)
    session.push()
    return session.current_sha()


def bootstrap_workspace(req: WorkspaceBootstrapRequest) -> WorkspaceBootstrapResult:
    """Idempotent across the runtime's possible states:
      1. workspace is already a git repo → return its HEAD (post-restart
         resume);
      2. checkpoint already has the branch → clone (+checkout) — this is the
         chaos test's rebuild, now INSIDE the sandbox;
      3. `repo` requested → clone the customer's real repo via the egress-proxy
         (`_clone_target_repo`); failing here is FAIL-CLOSED (never fall back to
         an empty workspace when a repo was explicitly requested);
      4. no repo → init from scratch (mirrors
         `git_checkpoint.init_task_workspace`).
    """
    try:
        _ensure_safe_directory()
        _configure_git_dependency_transport()
        ws = Path(req.workspace_dir)
        ws.mkdir(parents=True, exist_ok=True)

        if req.provision_checkpoint and not (Path(req.checkpoint_path) / "HEAD").is_file():
            Path(req.checkpoint_path).mkdir(parents=True, exist_ok=True)
            _git(["init", "--bare", req.checkpoint_path])
        # hook ALWAYS (re)installed before any push — idempotent
        install_pre_receive_guard(req.checkpoint_path, req.branch)

        if _is_git_workspace(req.workspace_dir):
            sha = _git(["rev-parse", "HEAD"], cwd=req.workspace_dir).stdout.strip()
            return WorkspaceBootstrapResult(sha=sha, created=False)

        if _checkpoint_has_branch(req.checkpoint_path, req.branch):
            _git(["clone", "--branch", req.branch, req.checkpoint_path, req.workspace_dir])
            # The identity has to be re-established HERE. `git config user.*`
            # lives in the workspace's own .git/config, and a rebuild throws
            # that workspace away and clones a fresh one from the checkpoint —
            # so the identity `_clone_target_repo` set on the first bootstrap is
            # gone. Every commit after a rebuild then died on "Please tell me
            # who you are", and because the fix loop rebuilds before retrying,
            # the retry could never succeed: seen at attempt 14 on the VPS,
            # failing `git commit --allow-empty` for the turn-start checkpoint.
            ScopedGitSession(
                workspace_dir=req.workspace_dir, branch=req.branch
            ).ensure_identity()
            write_task_branch_marker(req.workspace_dir, req.branch)
            sha = _git(["rev-parse", "HEAD"], cwd=req.workspace_dir).stdout.strip()
            return WorkspaceBootstrapResult(sha=sha, created=False)

        if req.repo:
            # Clone of the REAL repo. Failure does not fall back to an empty
            # init (P6/fail-closed): a repo was requested; an empty workspace
            # would mask the problem.
            try:
                sha = _clone_target_repo(req)
            except Exception as exc:  # noqa: BLE001
                return WorkspaceBootstrapResult(
                    error=f"clone of {req.repo!r} failed: {type(exc).__name__}: {str(exc)[:300]}",
                    error_kind="clone_error",
                )
            return WorkspaceBootstrapResult(sha=sha, created=True)

        _git(["init"], cwd=req.workspace_dir)
        _git(["checkout", "-b", req.branch], cwd=req.workspace_dir)
        session = ScopedGitSession(workspace_dir=req.workspace_dir, branch=req.branch)
        session.ensure_identity()
        write_task_branch_marker(req.workspace_dir, req.branch)
        session.commit(f"chore(dse): initialize the task workspace on branch {req.branch}")
        _git(["remote", "add", "origin", req.checkpoint_path], cwd=req.workspace_dir)
        session.push()
        return WorkspaceBootstrapResult(sha=session.current_sha(), created=True)
    except Exception as exc:  # noqa: BLE001 — P6: structured result
        return WorkspaceBootstrapResult(
            error=f"{type(exc).__name__}: {str(exc)[:400]}", error_kind="gitops_error"
        )


#: Onde o git guarda ignores LOCAIS — invisível para o repositório do
#: cliente, que é exatamente o ponto: o DSE não edita a política dele.
logger = logging.getLogger(__name__)

_LOCAL_EXCLUDE = ("info", "exclude")
_EXCLUDE_MARCA = "# dse: generated test reports (declared in reports.junit)"


def _exclude_declared_reports(workspace_dir: str) -> None:
    """Mantém fora do commit o relatório que a PLATAFORMA pediu.

    Medido em wi_e9764c2d: o checkpoint commitou um `junit.xml` de 12.733
    linhas, maior que a mudança inteira, e ele iria para a PR que um humano
    revisa. O caminho não é escolha do cliente — é o gate que exige
    `reports.junit` para ler contagem em vez de adivinhar por prosa; então a
    sujeira é nossa, e nós a limpamos.

    Best-effort e idempotente: manifesto ausente ou torto não pode derrubar o
    checkpoint, que é o que preserva o trabalho PAGO do turno."""
    try:
        manifesto = Path(workspace_dir) / ".dse" / "validation.json"
        if not manifesto.is_file():
            return
        payload = json.loads(manifesto.read_text(encoding="utf-8", errors="ignore"))
        glob = ((payload.get("reports") or {}) if isinstance(payload, dict) else {}).get("junit")
        if not isinstance(glob, str) or not glob.strip():
            return
        destino = Path(workspace_dir, ".git", *_LOCAL_EXCLUDE)
        destino.parent.mkdir(parents=True, exist_ok=True)
        atual = destino.read_text() if destino.is_file() else ""
        linha = glob.strip()
        if linha in atual.splitlines():
            return
        with destino.open("a", encoding="utf-8") as fh:
            if atual and not atual.endswith("\n"):
                fh.write("\n")
            fh.write(f"{_EXCLUDE_MARCA}\n{linha}\n")
    except Exception as exc:  # noqa: BLE001 — ver docstring: nunca derruba o checkpoint
        logger.info("could not exclude the declared test report (%s)", exc)


def checkpoint_workspace(req: CheckpointOpRequest) -> CheckpointOpResult:
    try:
        _ensure_safe_directory()
        _exclude_declared_reports(req.workspace_dir)
        session = ScopedGitSession(workspace_dir=req.workspace_dir, branch=req.branch)
        session.ensure_identity()
        if session.has_changes():
            session.commit(f"checkpoint({req.phase}): {req.work_item_id}")
        session.push()
        return CheckpointOpResult(sha=session.current_sha(), phase=req.phase)
    except Exception as exc:  # noqa: BLE001 — P6 (includes GitScopeViolation from the hook)
        return CheckpointOpResult(
            phase=req.phase,
            error=f"{type(exc).__name__}: {str(exc)[:400]}",
            error_kind="gitops_error",
        )


__all__ = ["bootstrap_workspace", "checkpoint_workspace"]
