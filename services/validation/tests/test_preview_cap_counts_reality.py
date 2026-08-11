"""O teto de previews conta o que EXISTE, não o que a tabela lembra.

Medido em produção (2026-08-11, 19:30:32, PR #4). O item foi degradado com

    preview_evicted_lru  {"cap":3, "namespace":"preview-wi-2202bae8…"}
    preview_degraded     {"cap":3, "active":3, "reason":"concurrency_cap"}

com ZERO namespaces de preview existindo no cluster naquele instante. Duas
falhas independentes se somaram:

**1. Contagem e evicção usam filtros diferentes.**
`count_active_previews` (db.py) exige `expires_at > now()`;
`list_oldest_active_previews` NÃO filtra TTL e ainda ordena expiradas primeiro.
Com o limite de evicção em `active - cap + 1` = 1, a única evicção é gasta numa
linha que já não era contada — o recount não cai e o fluxo desce para
`degraded`. É exatamente a assinatura acima: a evicção RODOU e `active`
continuou 3.

**2. Nada reconcilia a tabela com o cluster.**
O CronJob `preview-reaper` apaga namespaces e, por decisão declarada, nunca
escreve no Postgres. `reap_expired_previews` — o único código que marca
`reaped` — não tem call site nenhum (admitido no README e no BACKLOG). Então
apagar um namespace com `kubectl`, um GC do nó, ou um reboot deixam a linha
viva: a vaga só volta quando o `expires_at` passa, e o TTL na VPS é de 6 horas.

No banco de produção havia 7 linhas `created` e 0 namespaces — quatro delas de
29 a 31 de julho, penduradas havia duas semanas.

A ordem que estes testes fixam: colher expiradas (é de graça), reconciliar com
o cluster, recontar, e só então evictar entre as que de fato estão vivas.
Evicção passa a ser o último recurso, não o primeiro.
"""
from __future__ import annotations

from dse_contracts.activities import TriggerPreviewInput

from dse_validation import db
from dse_validation.config import PreviewConfig
from dse_validation.preview.argocd import trigger_preview_core


def _linha(work_item_id: str, tenant_id: str, ns: str, *, ttl: int) -> None:
    db.upsert_preview(
        work_item_id=work_item_id, tenant_id=tenant_id, pr_number=1,
        repo="acme/app", status="created", namespace=ns, ttl_seconds=ttl,
    )


def _cfg(tmp_path) -> PreviewConfig:
    cfg = PreviewConfig()
    cfg.repo_dir = str(tmp_path / "preview-repo")
    cfg.kube_context = "k3d-cluster-that-does-not-exist"
    return cfg


def _dispara(work_item_id: str, tenant_id: str, cfg):
    return trigger_preview_core(
        TriggerPreviewInput(
            work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/app",
            pr_number=99, files_changed=["frontend/app.tsx"],
        ),
        cfg=cfg,
    )


def test_an_expired_row_does_not_eat_the_only_eviction(work_item_id, tenant_id, tmp_path,
                                                       monkeypatch):
    """O caso literal da PR #4: 2 linhas contadas + 1 já expirada, teto 2.

    A expirada não conta para o teto, então colhê-la não libera nada — e era
    justamente nela que a única evicção era gasta."""
    db.set_preview_cap(tenant_id, 2)
    _linha(f"{work_item_id}-viva-0", tenant_id, "preview-viva-0", ttl=3600)
    _linha(f"{work_item_id}-viva-1", tenant_id, "preview-viva-1", ttl=3600)
    _linha(f"{work_item_id}-expirada", tenant_id, "preview-expirada", ttl=-60)
    assert db.count_active_previews(tenant_id) == 2

    # o cluster diz que os namespaces das VIVAS ainda existem
    monkeypatch.setattr(
        "dse_validation.preview.argocd.namespace_exists", lambda cfg, ns: True)

    ref = _dispara(work_item_id, tenant_id, _cfg(tmp_path))

    assert "cap" not in (ref.detail or ""), (
        f"degradou por teto: {ref.detail!r}. A evicção foi gasta na linha "
        "expirada, que nunca contou — o recount não podia cair"
    )
    assert db.get_preview(f"{work_item_id}-expirada")["status"] == "reaped", (
        "a linha expirada continua pendurada; foi assim que 4 linhas de julho "
        "sobreviveram duas semanas"
    )
    assert db.get_preview(f"{work_item_id}-viva-1")["status"] == "created", (
        "a mais nova foi sacrificada tendo lixo expirado disponível"
    )


def test_rows_whose_namespace_is_gone_free_their_slot(work_item_id, tenant_id, tmp_path,
                                                      monkeypatch):
    """O teto tem de contar RECURSO, não memória. Namespace apagado — na mão,
    por GC, por reboot do nó — libera a vaga na hora, não em 6 horas."""
    db.set_preview_cap(tenant_id, 2)
    _linha(f"{work_item_id}-fantasma-0", tenant_id, "preview-fantasma-0", ttl=3600)
    _linha(f"{work_item_id}-fantasma-1", tenant_id, "preview-fantasma-1", ttl=3600)
    assert db.count_active_previews(tenant_id) == 2

    monkeypatch.setattr(
        "dse_validation.preview.argocd.namespace_exists", lambda cfg, ns: False)

    ref = _dispara(work_item_id, tenant_id, _cfg(tmp_path))

    assert "cap" not in (ref.detail or ""), (
        f"degradou por teto com o cluster vazio: {ref.detail!r}"
    )
    for i in (0, 1):
        assert db.get_preview(f"{work_item_id}-fantasma-{i}")["status"] == "reaped"


def test_a_genuinely_full_cap_still_evicts_the_oldest(work_item_id, tenant_id, tmp_path,
                                                      monkeypatch):
    """PIN: com previews de verdade vivos, o comportamento não muda — LRU, o mais
    velho cede a vaga. Sem isto a correção teria transformado o teto em ficção."""
    db.set_preview_cap(tenant_id, 2)
    _linha(f"{work_item_id}-velha", tenant_id, "preview-velha", ttl=3600)
    _linha(f"{work_item_id}-nova", tenant_id, "preview-nova", ttl=3600)
    monkeypatch.setattr(
        "dse_validation.preview.argocd.namespace_exists", lambda cfg, ns: True)

    _dispara(work_item_id, tenant_id, _cfg(tmp_path))

    assert db.get_preview(f"{work_item_id}-velha")["status"] == "reaped"
    assert db.get_preview(f"{work_item_id}-nova")["status"] == "created"


def test_a_cluster_that_does_not_answer_never_frees_a_slot(work_item_id, tenant_id,
                                                           tmp_path, monkeypatch):
    """PIN de fail-closed. Se a consulta ao cluster falhar, a linha continua
    contando. Liberar vaga por ERRO DE LEITURA seria trocar um bloqueio visível
    por dois previews disputando o mesmo recurso — e o segundo modo de falha é
    muito mais difícil de enxergar."""
    db.set_preview_cap(tenant_id, 1)
    _linha(f"{work_item_id}-viva", tenant_id, "preview-viva", ttl=3600)

    def _explode(cfg, ns):
        raise RuntimeError("kubectl: connection refused")

    monkeypatch.setattr("dse_validation.preview.argocd.namespace_exists", _explode)

    # não pode estourar, e não pode tratar a linha como morta
    _dispara(work_item_id, tenant_id, _cfg(tmp_path))
    assert db.get_preview(f"{work_item_id}-viva")["status"] == "reaped", (
        "com teto cheio e leitura falha, o caminho correto é a evicção LRU de "
        "sempre — não é liberar por engano nem travar o tenant"
    )


def test_the_lru_query_agrees_with_the_count(tenant_id):
    """A raiz do defeito, isolada: as duas consultas têm de enxergar o MESMO
    conjunto. Enquanto uma filtrava TTL e a outra não, a evicção mirava linhas
    que a contagem ignorava."""
    _linha("wi-conta-viva", tenant_id, "ns-viva", ttl=3600)
    _linha("wi-conta-expirada", tenant_id, "ns-expirada", ttl=-60)

    contadas = db.count_active_previews(tenant_id)
    candidatas = db.list_oldest_active_previews(tenant_id, limit=10)

    assert contadas == 1
    assert len(candidatas) == contadas, (
        f"a evicção vê {len(candidatas)} candidata(s) e a contagem vê {contadas}. "
        "Uma linha expirada na lista de evicção consome a vaga sem baixar o "
        "contador — é o defeito aritmético da PR #4"
    )
    assert all(c["work_item_id"] != "wi-conta-expirada" for c in candidatas)
