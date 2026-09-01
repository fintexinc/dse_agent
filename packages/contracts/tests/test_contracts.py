
import pytest
from dse_contracts import (
    Actor,
    ConversationEvent,
    EventKind,
    Platform,
    WorkItemStatus,
    to_public_status,
)


def test_conversation_event_build_is_deterministic_for_dedup():
    kwargs = dict(
        platform=Platform.slack,
        thread_key="C123:171234.5678",
        message_id="171234.5678",
        kind=EventKind.task_request,
        source_ref={"channel": "C123", "thread_ts": "171234.5678"},
        actor=Actor(platform_user_id="U1"),
        content_snapshot="@fintex-dse fix the flaky test",
        signature_verified=True,
    )
    ev1 = ConversationEvent.build(**kwargs)
    ev2 = ConversationEvent.build(**kwargs)
    assert ev1.event_id == ev2.event_id, "the same platform+thread+message must collide (dedup)"

    ev3 = ConversationEvent.build(**{**kwargs, "message_id": "other"})
    assert ev3.event_id != ev1.event_id


def test_conversation_event_is_frozen():
    ev = ConversationEvent.build(
        platform=Platform.github,
        thread_key="acme/repo#42",
        message_id="c1",
        kind=EventKind.review_comment,
        source_ref={"repo": "acme/repo", "issue_number": 42},
        actor=Actor(platform_user_id="gh:bob"),
        content_snapshot="please fix the typo",
        signature_verified=True,
    )
    with pytest.raises(Exception):
        ev.content_snapshot = "tampered"  # type: ignore[misc]


def test_every_internal_status_has_a_public_projection():
    # Key WSA-E1-T4 regression: adding a WorkItemStatus without updating the
    # public map must break this test, not silently leak "None".
    for status in WorkItemStatus:
        projected = to_public_status(status)
        assert projected in ("running", "blocked", "done", "failed")


def test_plan_artifact_estimated_lines_is_additive():
    """rc.89: `estimated_lines` entra como campo OPCIONAL — payload histórico
    (sem a chave) revalida com None; com a chave, faz roundtrip; e o dump de um
    plano novo CARREGA a chave (o que muda o plan_hash só de planos novos — a
    decisão pinada aqui). `diff_budget_lines` fica como legado ignorado."""
    from dse_contracts import PlanArtifact

    historico = PlanArtifact.model_validate({
        "work_item_id": "wi_old", "steps": ["s"], "expected_files": ["a.py"],
        "diff_budget_lines": 400,
    })
    assert historico.estimated_lines is None

    novo = PlanArtifact(work_item_id="wi_new", steps=["s"],
                        expected_files=["a.py"], estimated_lines=380)
    assert PlanArtifact.model_validate(novo.model_dump()).estimated_lines == 380
    assert "estimated_lines" in novo.model_dump()


def test_ci_status_result_carries_failing_checks_and_old_payloads_decode_empty():
    """rc.130: a evidência do CI vermelho viaja no contrato. Aditivo: um payload
    histórico (sem a chave) decodifica com lista vazia; um novo faz roundtrip."""
    from dse_contracts import CiStatusResult, FailingCheck

    velho = CiStatusResult.model_validate(
        {"work_item_id": "wi_x", "pr_number": 1, "status": "red"}
    )
    assert velho.failing_checks == []

    novo = CiStatusResult(
        work_item_id="wi_x", pr_number=1, status="red",
        failing_checks=[FailingCheck(name="unit (API)", conclusion="failure",
                                     url="https://github.com/acme/repo/runs/2")],
    )
    de_volta = CiStatusResult.model_validate(novo.model_dump())
    assert de_volta.failing_checks[0].name == "unit (API)"


def test_the_public_projection_of_cancelled_is_failed():
    """rc.130: `cancelled` entra no enum. Medido: 33 linhas em produção já
    carregavam o valor por SQL de operador, e o sweep de encalhados — que não o
    reconhecia como terminal — re-escalava cada uma 6 h depois."""
    assert to_public_status(WorkItemStatus.cancelled) == "failed"


# ---------------------------------------------------------------------------
# rc.131 — o preview provado FORA de um item (o smoke): sem PR, com branch e
# kind explícitos. Aditivo: o payload de sempre decodifica igual.
# ---------------------------------------------------------------------------

def test_a_preview_can_be_triggered_without_a_pr_for_the_smoke():
    from dse_contracts.activities import PreviewRef, TriggerPreviewInput

    inp = TriggerPreviewInput(
        work_item_id="wi_smoke", tenant_id="t", repo="acme/app",
        pr_number=None, branch="main", kind="ui", ttl_seconds=1800,
    )
    assert inp.pr_number is None and inp.branch == "main" and inp.kind == "ui"
    assert inp.ttl_seconds == 1800
    # o payload de um item de verdade não muda de forma
    velho = TriggerPreviewInput(work_item_id="wi_x", tenant_id="t", repo="acme/app", pr_number=7)
    assert velho.branch is None and velho.kind is None and velho.ttl_seconds is None
    ref = PreviewRef(work_item_id="wi_smoke", pr_number=None, status="created", url="https://p.example")
    assert ref.pr_number is None
