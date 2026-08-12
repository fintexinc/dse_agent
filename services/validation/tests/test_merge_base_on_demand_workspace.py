"""O merge-base encontra um pod sem workspace — e provisiona, em vez de morrer.

Medido em produção (2026-08-12, wi_a8b760de…, PR #6 do bmo-test-dse-fe): o
review `changes_requested` chegou às 10:41Z e o fix cycle NUNCA começou. A
activity `update_base_branch` retentou por horas com

    FileNotFoundError: .../merge_base_repos/wi_a8b760de…/workspace

`MergeBaseConfig.locations()` devolve dois caminhos possíveis e, no driver k8s,
nenhum existe no pod do orchestrator: o workspace do sandbox vive DENTRO do pod
do sandbox (outro pod), e `merge_base_repos/` aponta para dentro da imagem —
que roda com `readOnlyRootFilesystem: true`. Ninguém provisiona nada, o
wrapper passa o caminho ao core, e o core morre no primeiro `git` com cwd
inexistente. O caminho `changes_requested` inteiro nunca funcionou na VPS.

O que este arquivo fixa: quando o workspace não existe, a activity o PROVISIONA
(clone autenticado, task branch em checkout, identidade própria), usa, e limpa —
funciona em qualquer réplica do worker, sem estado entre chamadas. E os limites:
o workspace do sandbox continua tendo precedência; raiz não-gravável cai para o
tmp do pod; sem origem nenhuma o erro é NOSSO e nomeado, nunca o
`FileNotFoundError` mudo que segurou um item por horas.

REAL git + REAL Postgres, como o vizinho test_merge_base.py — nada mockado.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from dse_contracts.activities import UpdateBaseBranchInput

from dse_validation.activities import _update_base_branch

BASE = "main"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _config_identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "coder@dse.local")
    _git(repo, "config", "user.name", "DSE Coder")


def _bare_files(origin: Path, branch: str) -> set[str]:
    proc = subprocess.run(
        ["git", "--git-dir", str(origin), "ls-tree", "-r", "--name-only", branch],
        capture_output=True, text=True, check=True,
    )
    return set(proc.stdout.split())


def _make_origin_only(tmp_path: Path, wi: str) -> tuple[Path, str]:
    """Bare origin + task branch + drift na base — e NENHUM workspace.

    É o estado do pod do orchestrator em produção: o repo existe (no GitHub),
    a branch existe, o drift existe, e localmente não há nada."""
    base_dir = tmp_path / wi
    base_dir.mkdir(parents=True, exist_ok=True)
    origin = base_dir / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", BASE, str(origin)], check=True)

    seed = tmp_path / f"seed-{wi}"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", BASE)
    _config_identity(seed)
    (seed / "shared.py").write_text("base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base: shared.py")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", BASE)

    branch = f"dse/{wi}"
    _git(seed, "checkout", "-q", "-b", branch)
    (seed / "feature.py").write_text("def f():\n    return 1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "feat: feature")
    _git(seed, "push", "-q", "origin", branch)

    # o drift: a base avança depois que a branch nasceu
    _git(seed, "checkout", "-q", BASE)
    (seed / "drift.py").write_text("# base moved forward\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base: drift.py")
    _git(seed, "push", "-q", "origin", BASE)
    return origin, branch


@pytest.fixture
def ids():
    return f"acme-{uuid.uuid4().hex[:8]}", f"wi_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def isolado(tmp_path, monkeypatch):
    """O ambiente do pod: raiz do merge-base apontada para o tmp do teste e
    NENHUM workspace de sandbox neste host."""
    monkeypatch.setenv("DSE_WSE_GIT_ROOT", str(tmp_path))
    monkeypatch.setenv("DSE_SANDBOX_STATE_DIR", str(tmp_path / "sem-sandboxes"))
    return tmp_path


def _inp(wi: str, tenant: str, branch: str) -> UpdateBaseBranchInput:
    return UpdateBaseBranchInput(
        work_item_id=wi, tenant_id=tenant, repo="acme/repo",
        branch=branch, base_branch=BASE, first_human_review_done=True,
    )


def test_update_base_branch_provisions_workspace_when_missing(isolado, ids):
    """O vermelho principal — o caso literal de produção. Hoje: FileNotFoundError
    e retry infinito; o item ficou horas parado com o review na mão."""
    tenant, wi = ids
    origin, branch = _make_origin_only(isolado, wi)

    result = _update_base_branch(_inp(wi, tenant, branch))

    assert result.conflict is False
    assert result.strategy == "merge_base", (
        f"com drift e review feito, a estratégia é merge — veio {result.strategy!r}"
    )
    assert result.orphaned_threads == 0
    files = _bare_files(origin, branch)
    assert "drift.py" in files and "feature.py" in files, (
        f"o merge não chegou ao origin (push ausente?): {sorted(files)}"
    )


def test_provisioned_workspace_is_cleaned_up_after_use(isolado, ids):
    """O /tmp do pod tem 256Mi para TODAS as réplicas e chamadas: o workspace
    provisionado é descartável por construção — usa e some. E a chamada
    seguinte re-provisiona do zero, agora sem drift → noop."""
    tenant, wi = ids
    origin, branch = _make_origin_only(isolado, wi)

    _update_base_branch(_inp(wi, tenant, branch))
    workspace = isolado / wi / "workspace"
    assert not workspace.exists(), (
        "o workspace provisionado sobreviveu à chamada — em produção isso "
        "acumula clones no emptyDir de 256Mi até estourar"
    )
    assert origin.exists(), "a limpeza levou o origin junto — em dev ele é a origem"

    second = _update_base_branch(_inp(wi, tenant, branch))
    assert second.strategy == "noop_no_drift", (
        f"a segunda chamada devia re-provisionar e ver a base já contida: {second.strategy!r}"
    )


def test_existing_sandbox_workspace_is_preferred_no_reclone(tmp_path, monkeypatch, ids):
    """PIN de fronteira (já passa hoje): quando o workspace REAL do sandbox
    existe neste host (driver local/in-process), é ELE que o merge-base usa —
    nada de re-clonar, e nada de apagar o que é do sandbox."""
    tenant, wi = ids
    origin, branch = _make_origin_only(tmp_path, wi)

    state = tmp_path / "state"
    ws = state / wi / "workspace"
    ws.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(ws)], check=True)
    _config_identity(ws)
    _git(ws, "checkout", "-q", branch)

    monkeypatch.setenv("DSE_SANDBOX_STATE_DIR", str(state))
    monkeypatch.setenv("DSE_WSE_GIT_ROOT", str(tmp_path / "raiz-nao-usada"))

    result = _update_base_branch(_inp(wi, tenant, branch))

    assert result.strategy == "merge_base"
    assert ws.exists(), "o workspace do sandbox foi apagado — ele pertence ao sandbox"


def test_unwritable_git_root_falls_back_to_tmp(tmp_path, monkeypatch, ids, request):
    """A raiz default aponta para dentro da imagem, e a imagem roda com
    readOnlyRootFilesystem — makedirs morre em EROFS/EACCES. O provisionamento
    cai para o tmp gravável em vez de re-morrer no mesmo lugar."""
    tenant, wi = ids
    rooty = tmp_path / "raiz-selada"
    rooty.mkdir()
    origin, branch = _make_origin_only(rooty, wi)
    rooty.chmod(0o500)
    request.addfinalizer(lambda: rooty.chmod(0o700))

    monkeypatch.setenv("DSE_WSE_GIT_ROOT", str(rooty))
    monkeypatch.setenv("DSE_SANDBOX_STATE_DIR", str(tmp_path / "sem-sandboxes"))

    result = _update_base_branch(_inp(wi, tenant, branch))

    assert result.conflict is False
    assert result.strategy == "merge_base", (
        "com a raiz configurada ilegível para escrita, o workspace tem de "
        f"nascer noutra raiz gravável — veio {result.strategy!r}"
    )
    assert "drift.py" in _bare_files(origin, branch)


def test_provisioned_workspace_checks_out_the_task_branch(isolado, ids):
    """Num clone fresco a task branch não existe LOCALMENTE, e o
    `_ensure_on_branch` do core só troca para branch local que já resolve —
    sem o `checkout -B` do provisionamento, o merge rodaria em cima da default
    branch e empurraria a base para a branch errada."""
    from dse_validation.merge_base import ensure_workspace

    tenant, wi = ids
    _origin, branch = _make_origin_only(isolado, wi)

    ws = ensure_workspace(work_item_id=wi, repo="acme/repo", branch=branch)

    assert ws.provisioned is True
    head = _git(Path(ws.workspace_dir), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == branch, (
        f"o workspace provisionado está em {head!r}, não na task branch {branch!r}"
    )


def test_provisioning_never_persists_a_credentialed_url(isolado, ids, monkeypatch):
    """O mecanismo de credencial: o fetch fala com a URL AUTENTICADA por
    comando, e o que fica gravado como `remote.origin.url` é a URL LIMPA.
    Clonar-da-autenticada-e-trocar-depois deixaria o token em .git/config por
    uma janela — aqui ele nunca toca disco."""
    import dse_validation.merge_base as mb

    tenant, wi = ids
    origin, branch = _make_origin_only(isolado, wi)
    com_credencial = str(origin)  # faz as vezes da URL autenticada: o fetch é real
    limpa = "https://github.com/acme/repo.git"
    monkeypatch.setattr(
        mb, "_resolve_origin", lambda repo, od: (com_credencial, limpa, com_credencial)
    )

    ws = mb.ensure_workspace(work_item_id=wi, repo="acme/repo", branch=branch)

    config = (Path(ws.workspace_dir) / ".git" / "config").read_text()
    assert limpa in config, "o remote gravado é a URL limpa"
    assert com_credencial not in config, (
        "a URL 'autenticada' (a que carrega credencial em produção) foi parar "
        "no .git/config — `kubectl exec` + cat exporia o token"
    )
    assert ws.remote_url == com_credencial, (
        "o core precisa receber a URL autenticada para fetch/push por comando"
    )


def test_git_error_redacts_the_secret_remote(isolado, ids):
    """A mensagem do GitError vai para o log do Temporal e para o ledger — e o
    git ecoa a URL do remoto no stderr de um fetch falho. Com a URL autenticada
    em uso, isso seria o installation token em texto plano no log."""
    import subprocess as sp

    from dse_validation.merge_base import GitError, _git

    ws = isolado / "ws-redact"
    ws.mkdir()
    sp.run(["git", "init", "-q", str(ws)], check=True)
    segredo = str(isolado / "x-access-token-SEGREDO123.git")  # não existe: falha rápida, sem rede

    with pytest.raises(GitError) as ei:
        _git(ws, "fetch", "--quiet", segredo, redact=(segredo,))

    assert "SEGREDO123" not in str(ei.value), (
        f"o segredo sobreviveu à redação: {ei.value}"
    )
    assert "<redacted-remote>" in str(ei.value)


def test_missing_workspace_and_no_origin_fails_with_named_error(isolado, ids):
    """Sem workspace, sem GitHub App e sem bare local não há de onde clonar.
    A falha tem de ser NOSSA e dizer isso — o FileNotFoundError cru de hoje
    segurou um item por horas sem que o ledger soubesse por quê."""
    tenant, wi = ids
    # nenhum _make_origin_only: não existe origem nenhuma

    with pytest.raises(RuntimeError, match="provision|origem|origin"):
        _update_base_branch(_inp(wi, tenant, f"dse/{wi}"))
