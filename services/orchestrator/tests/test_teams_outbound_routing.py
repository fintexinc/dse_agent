"""O orchestrator sabe endereçar a superfície do Teams.

`post_tracking_comment` resolve (adapter_url, campos de correlação) a partir de
`work_items.source` — e `_resolve_comment_target` conhecia github/slack/jira.
Um work item nascido no Teams caía no `return None` final, que a Activity trata
como "sem alvo": `{"ok": True, "skipped": ...}`. Silencioso por construção — o
adapter estaria no ar, o item andaria, e NADA apareceria no canal.

Correlação do Teams (ver `adapter_teams/events.py`): o par
(`service_url`, `conversation_id`) é o que endereça uma mensagem — o
`service_url` é regional (chega em toda Activity recebida) e o
`conversation_id` carrega canal + mensagem raiz da thread, que é o análogo do
`thread_ts` do Slack.
"""
from __future__ import annotations

import os

from dse_orchestrator.local_activities import _resolve_comment_target

_SOURCE_REF = {
    "service_url": "https://smba.trafficmanager.net/emea/",
    "conversation_id": "19:abc123@thread.tacv2;messageid=1700000000000",
}


def test_a_teams_work_item_resolves_to_the_teams_adapter(monkeypatch):
    monkeypatch.setenv("DSE_ADAPTER_TEAMS_URL", "http://adapter-teams:8808")
    target = _resolve_comment_target("teams", "acme/repo", dict(_SOURCE_REF))
    assert target is not None, (
        "source=teams não resolve alvo — o status some em silêncio"
    )
    url, fields = target
    assert url == "http://adapter-teams:8808"
    assert fields["conversation_id"] == _SOURCE_REF["conversation_id"]
    assert fields["service_url"] == _SOURCE_REF["service_url"]


def test_teams_without_a_conversation_is_unaddressable():
    """Mesma disciplina do github sem issue_number e do slack sem channel:
    sem conversa não há alvo, e inventar um é pior que não postar."""
    assert _resolve_comment_target("teams", "acme/repo", {}) is None
    assert _resolve_comment_target(
        "teams", "acme/repo", {"service_url": _SOURCE_REF["service_url"]}) is None


def test_the_teams_adapter_url_has_a_default():
    """Sem env var o alvo continua resolvendo (o service do cluster), como nos
    outros adapters — o chart não precisa injetar a URL para funcionar."""
    os.environ.pop("DSE_ADAPTER_TEAMS_URL", None)
    target = _resolve_comment_target("teams", "acme/repo", dict(_SOURCE_REF))
    assert target is not None and target[0].startswith("http")
