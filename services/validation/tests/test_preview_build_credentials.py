"""A credencial de build chega ao pod de preview — para QUALQUER ecossistema.

Medido no wi_7163dd36 (`calculation-engine-service`, PR #150): o build dentro
do pod de preview tomou `401 Unauthorized` do feed Maven privado
(`pkgs.dev.azure.com`), o container morreu em CrashLoop e o Traefik devolveu
`404 page not found` no link da PR. A credencial EXISTE na plataforma desde a
rc.98 — mas só chega ao pod do SANDBOX (settings.xml via exec stdin); o pod de
preview clona e roda o build cru. O triage agent diagnosticou sozinho a mesma
causa. Efeito colateral: o item ficou ~15 min parado dentro da espera de
prontidão antes de seguir para o CI — o Slack "congelado" era isso.

O desenho (unânime, decisão de operador 2026-08-19):

  - UM builder de settings (`dse_validation.build_credentials`) para sandbox e
    preview — o do sandbox importa de lá. Diferença única: o preview NÃO passa
    pelo egress proxy, então `egress_proxy_url=None` omite o bloco <proxies>
    (injetá-lo mudaria o roteamento do build por efeito colateral).
  - Um Secret `dse-preview-build-credentials` por namespace, semeado FORA do
    manifest set (mesmo padrão e mesma razão da deploy key: em modo gitops o
    set vira commit). Uma chave por arquivo de ecossistema: hoje
    `maven-settings.xml`; um registry npm privado amanhã acrescenta `npmrc`,
    não outra arquitetura.
  - Prelúdio ecosystem-neutro nos DOIS scripts (deployable e ui): copia do
    volume o que existir. Repo sem credencial configurada → NENHUMA mudança
    (manifests byte-idênticos — é o pin que protege os outros repos).
  - O token nunca aparece no spec do pod — a mesma invariante do sandbox.
"""
from __future__ import annotations

from dse_validation.config import PreviewConfig
from dse_validation.preview import argocd

try:  # o vermelho: o módulo compartilhado ainda não existe
    from dse_validation.build_credentials import (
        maven_proxy_settings_xml,
        preview_credential_files,
    )
except ImportError:  # pragma: no cover
    maven_proxy_settings_xml = None  # type: ignore[assignment]
    preview_credential_files = None  # type: ignore[assignment]

_LABELS = {"dse.fintex/work-item": "wi_teste"}


def _cfg(**maven) -> PreviewConfig:
    cfg = PreviewConfig()
    cfg.mode = "source"
    for k, v in maven.items():
        setattr(cfg, k, v)
    return cfg


# ---------------------------------------------------------------------------
# O builder compartilhado
# ---------------------------------------------------------------------------

def test_the_shared_builder_exists_in_the_layer_both_sides_can_import():
    assert maven_proxy_settings_xml is not None, (
        "dse_validation.build_credentials não existe — o builder continua "
        "preso no k8s_driver, onde o preview (dse_validation) não pode "
        "importá-lo sem inverter a camada"
    )


def test_without_a_proxy_url_there_is_no_proxies_block():
    """O pod de preview vai direto à internet; um <proxies> apontando para o
    egress-proxy mudaria o roteamento do build por efeito colateral."""
    xml = maven_proxy_settings_xml(
        None, feed_id="fintexincorporated", feed_username="dse", feed_token="s3gr3d0",
    )
    assert "<servers>" in xml and "<id>fintexincorporated</id>" in xml
    assert "<proxies>" not in xml


def test_with_a_proxy_url_the_sandbox_document_is_unchanged():
    """Regressão do sandbox: o documento com proxy continua o de sempre."""
    xml = maven_proxy_settings_xml(
        "http://dse-egress-proxy:8806", feed_id="f", feed_username="u", feed_token="t",
    )
    assert "<proxies>" in xml and "dse-egress-https" in xml
    assert "<servers>" in xml


def test_the_sandbox_driver_still_exports_the_same_builder():
    """O k8s_driver importa do módulo novo; os testes de contrato do sandbox
    seguem valendo pelo mesmo nome."""
    from sandbox_runtime.k8s_driver import maven_proxy_settings_xml as do_sandbox

    a = do_sandbox("http://p:1", feed_id="f", feed_username="u", feed_token="t")
    b = maven_proxy_settings_xml("http://p:1", feed_id="f", feed_username="u", feed_token="t")
    assert a == b, "dois builders de settings.xml — era exatamente o defeito"


def test_credential_files_by_ecosystem():
    """Sem credencial → dict vazio (nada muda em lugar nenhum). Com feed Maven
    → a chave do arquivo de ecossistema, com o documento sem proxies."""
    assert preview_credential_files(feed_id="", feed_username="", feed_token="") == {}
    files = preview_credential_files(
        feed_id="fintexincorporated", feed_username="dse", feed_token="s3gr3d0",
    )
    assert set(files) == {"maven-settings.xml"}
    assert "<proxies>" not in files["maven-settings.xml"]
    assert "s3gr3d0" in files["maven-settings.xml"]


# ---------------------------------------------------------------------------
# O pod: volume + prelúdio, e o pin de que sem credencial NADA muda
# ---------------------------------------------------------------------------

def _deployment(*, files=(), kind="deployable") -> str:
    return argocd._source_deployment(
        "preview-wi", _LABELS, _cfg(), repo="acme/svc", branch="dse/wi",
        kind=kind, build_credential_files=tuple(files),
    )


def test_with_a_maven_credential_the_pod_mounts_and_copies_it():
    y = _deployment(files=("maven-settings.xml",))
    assert argocd.BUILD_CREDENTIALS_SECRET in y, "o volume do Secret não entrou"
    assert "/preview-build-creds" in y
    assert 'settings.xml' in y, "o prelúdio não copia para ~/.m2/settings.xml"


def test_the_ui_recipe_gets_the_same_prelude():
    """Unânime = os dois branches. Um registry npm privado amanhã só precisa da
    chave `npmrc` — não de outra receita."""
    y = _deployment(files=("maven-settings.xml",), kind="ui")
    assert "/preview-build-creds" in y


def test_without_credentials_the_manifests_are_byte_identical():
    """O pin que protege todos os outros repos: sem credencial configurada, o
    Deployment de hoje é o Deployment de amanhã."""
    y = _deployment(files=())
    assert "preview-build-creds" not in y
    assert argocd.BUILD_CREDENTIALS_SECRET not in y


def test_the_token_value_never_reaches_the_pod_spec():
    """`kubectl get pod -o yaml` é leitura banal; o valor viaja no OBJETO
    Secret, o spec só carrega a referência — a invariante do sandbox, agora
    pinada no preview."""
    y = _deployment(files=("maven-settings.xml",))
    assert "s3gr3d0" not in y


def test_the_secret_is_seeded_outside_the_manifest_set():
    """Mesma razão da deploy key: em modo gitops o manifest set vira commit."""
    cfg = _cfg(maven_feed_id="f", maven_feed_username="u", maven_feed_token="tok")
    manifests = argocd.build_manifests(
        "preview-wi", "wi_x", "tenant", __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc),
        3600, cfg, repo="acme/svc", branch="dse/wi", kind="deployable",
    )
    corpo = "\n".join(manifests.values())
    assert "tok" not in corpo, "credencial dentro do manifest set"
    assert "kind: Secret" not in corpo


def test_token_mode_rewrites_git_ssh_dependencies_to_the_stored_credential():
    """Dependência `git+ssh://git@github.com/...` no lockfile (medido no
    glide-path: wealth-components privado) morre no npm install do preview —
    não há chave ssh lá. No modo token o credential helper já guarda o token
    para https://github.com; a receita reescreve as formas ssh/scp-like para
    https e o npm passa a alcançar a dependência com a credencial que já
    existe. No modo ssh a deploy key só abre o próprio repo — residual."""
    y = argocd._source_deployment(
        "preview-wi", _LABELS, _cfg(), repo="acme/svc", branch="dse/wi",
        kind="deployable", auth_mode="token",
    )
    assert "insteadOf" in y
    assert "ssh://git@github.com/" in y
    assert "git@github.com:" in y


def test_apply_build_credentials_writes_the_secret_via_kubectl(monkeypatch):
    chamadas: list[str] = []
    monkeypatch.setattr(
        argocd, "_kubectl",
        lambda cfg, args, input_text=None, timeout=60: chamadas.append(input_text or ""),
    )
    argocd.apply_build_credentials(
        _cfg(), "preview-wi", {"maven-settings.xml": "<settings>x</settings>"},
    )
    assert chamadas, "nenhum kubectl apply"
    assert argocd.BUILD_CREDENTIALS_SECRET in chamadas[0]
    assert "preview-wi" in chamadas[0]
