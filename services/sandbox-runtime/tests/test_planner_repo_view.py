"""What the Planner can SEE before it decides — and whether the ledger tells the
truth about it (plan items 0.4, 3.1, 3.3).

Two measured failures are pinned here as tests:

  - the context was cut with a blind `[:8000]` prefix. Against the live
    fintex-poc registry that render is 10.351 chars, so 2.351 were dropped: 5
    skills never reached the model — `redacting-pii-and-secrets` among them, in
    a regulated fintech tenant — while `planner_turn_completed` recorded all 21
    as hydrated. A number that lies is worse than no number, because nobody
    investigates it.
  - the tree was `tree[:250]` then `[:8000]` chars: 219 of 7.079 blobs on
    django (3,09%), a window that ends inside Croatian locale files with nothing
    from django/db/, django/forms/ or tests/ — while the prompt told the model
    the window was authoritative.

The comparison against the OLD behaviour is COMPUTED here (`_old_window`), never
asserted from memory: a test that hardcodes "3,09%" proves nothing the day the
slicing changes.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import sandbox_runtime.activities as acts
from sandbox_runtime.sessions import PlannerContext
from sandbox_runtime.skill_registry import Skill


# ---------------------------------------------------------------------------
# Fixtures shaped like the real thing
# ---------------------------------------------------------------------------


def _django_shaped_tree() -> list[str]:
    """A tree with django's measured shape: 7.079 blobs, mean path 44,76 chars,
    the bulk of it in locale files that the old prefix window landed in."""
    tree = ["README.rst", "setup.py", "pyproject.toml", "package.json"]
    tree += [f".github/workflows/workflow_number_{i}.yml" for i in range(27)]
    tree += [f"django/conf/locale/lc{i % 60}/LC_MESSAGES/django_messages_{i}.po" for i in range(2500)]
    tree += [f"django/db/models/fields/related_descriptors_{i}.py" for i in range(300)]
    tree += [f"django/forms/widgets/form_widget_number_{i}.py" for i in range(200)]
    tree += [f"tests/suite_{i // 40}/test_regression_case_number_{i}.py" for i in range(2577)]
    tree += [f"docs/ref/topics/reference_document_number_{i}.rst" for i in range(738)]
    return tree


def _old_window(tree: list[str]) -> list[str]:
    """Exactly what the pre-change code delivered: `tree[:250]` joined, cut at
    8.000 chars. Only WHOLE lines count — the last one used to arrive mangled,
    which is part of what was wrong with it."""
    window = "\n".join(tree[:250])[:8000]
    lines = window.split("\n")
    return lines[:-1] if window and not window.endswith("\n") else lines


def _skill(key: str, title_chars: int = 200) -> Skill:
    return Skill(
        tenant_id="fintex-poc",
        skill_key=key,
        title=("how to " + key.replace("-", " ") + " ").ljust(title_chars, "."),
        body="x" * 4900,
        category="engineering",
        applies_to=["default"],
    )


_LIVE_REGISTRY_KEYS = [
    "redacting-pii-and-secrets",
    "writing-audit-trail-events",
    "designing-infrastructure-and-deployment",
    "authoring-playwright-demo-tests",
    "writing-effective-tests",
    "classifying-task-risk",
] + [f"engineering-skill-{i:02d}" for i in range(15)]


def _live_registry_context(title_chars: int = 341) -> PlannerContext:
    """The live fintex-poc shape: 21 served skills, mean title 341 chars, and a
    work_item_id of 67 chars (the render's first line carries it)."""
    return PlannerContext(
        work_item_id="wi_" + "a" * 64,
        tenant_id="fintex-poc",
        skills=[_skill(k, title_chars) for k in _LIVE_REGISTRY_KEYS],
        repo_map="## files\nsrc/app.py\nsrc/wallet.py\n",
    )


class _Ctx:
    """A context that is NOT a dataclass — the double used by the existing
    proposer tests. Nothing here may assume `dataclasses.replace` works."""

    def render(self, *, skill_body_chars: int | None = None) -> str:
        return "## Task\nFix DELETE /api/transactions/:id."


def _inp(instruction: str = "fix the DELETE handler for transactions"):
    return SimpleNamespace(instruction=instruction, work_item_id="wi-x", tenant_id="fintex-poc",
                           repo="django/django", base_branch="main")


class _Chat:
    """Records every gateway call so the number of passes, and what each pass
    carried, are facts of the test rather than assumptions."""

    def __init__(self, *, directories=None, plan=None):
        self.prompts: list[str] = []
        self._directories = directories
        self._plan = plan or {"steps": ["s1", "s2"], "expected_files": ["django/db/models/fields/related_descriptors_1.py"],
                              "test_plan": "t"}

    def __call__(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        self.prompts.append(prompt)
        if "Repository top level" in prompt:
            if isinstance(self._directories, Exception):
                raise self._directories
            body = json.dumps({"directories": self._directories or []})
        else:
            body = json.dumps(self._plan)
        return SimpleNamespace(content=body, model="anthropic/claude", cost_usd=0.002,
                               tokens_in=100, tokens_out=50, raw={})


def _patch_gateway(monkeypatch, chat: _Chat, tree: list[str] | Exception):
    import model_gateway_client.gateway_call as gc

    monkeypatch.setattr(gc, "chat_completion", chat)
    fetches: list[str] = []

    def fake_tree(repo, base_branch):
        fetches.append(repo)
        if isinstance(tree, Exception):
            raise tree
        return tree

    monkeypatch.setattr(acts, "_repo_tree_for_planner", fake_tree)
    return fetches


# ---------------------------------------------------------------------------
# The tree view: one pass when the tree fits, two when it does not
# ---------------------------------------------------------------------------


def test_a_tree_that_fits_is_delivered_whole_in_a_single_pass():
    """fintex-wallet is 12 files. The new route must not spend a second model
    call to look at a repository it can already see in full."""
    tree = [f"src/module_{i}.js" for i in range(12)]

    def never(summary, total):
        raise AssertionError("a tree that fits must not ask the model anything")

    view = acts._build_tree_view(tree, choose_dirs=never)

    assert view.telemetry["tree_mode"] == "full"
    assert view.telemetry["tree_selection_calls"] == 0
    assert view.telemetry["tree_coverage"] == 1.0
    assert view.telemetry["tree_delivered"] == view.telemetry["tree_total"] == 12
    assert "PARTIAL" not in view.section
    assert all(path in view.section for path in tree)


def test_a_large_tree_takes_two_passes_and_beats_the_prefix_it_replaces():
    tree = _django_shaped_tree()
    old = _old_window(tree)
    old_coverage = len(old) / len(tree)

    view = acts._build_tree_view(tree, choose_dirs=lambda summary, total: ["django/db", "tests"])

    assert view.telemetry["tree_mode"] == "expanded"
    assert view.telemetry["tree_selection_calls"] == 1
    # The point of the whole change, stated as a number the test computes on
    # both sides rather than quoting.
    assert view.telemetry["tree_coverage"] > old_coverage * 5
    assert view.telemetry["tree_delivered"] > len(old)
    # The old window reached NONE of django/db — it ended inside the locales.
    assert not [p for p in old if p.startswith("django/db/")]
    assert "django/db/models/fields/related_descriptors_7.py" in view.section
    # A1: the directories the diff would touch are delivered COMPLETE, and the
    # ledger says which ones, so the assertion is answerable by query.
    assert set(view.telemetry["tree_dirs_expanded"]) >= {"tests"}
    assert view.section.count("tests/suite_") >= 2577
    _assert_no_half_listings(view, tree)


def test_the_whole_shape_of_the_repo_is_visible_even_when_the_files_are_not():
    """Depth 1 is cheap (measured: 207 chars on django) and it is what keeps the
    model from believing the listing is the repository."""
    tree = _django_shaped_tree()
    summaries: list[str] = []

    def choose(summary, total):
        summaries.append(summary)
        return ["tests"]

    view = acts._build_tree_view(tree, choose_dirs=choose)

    assert len(summaries[0]) < 1000
    for top in ("django/", "tests/", "docs/", ".github/"):
        assert top in summaries[0]
        assert top in view.section
    assert "PARTIAL VIEW" in view.section
    assert "needs_paths" in view.files_rule


def _assert_no_half_listings(view, tree: list[str]) -> None:
    """The invariant: a directory the ledger calls expanded is delivered
    COMPLETE. Half a directory listing reads exactly like a complete one."""
    lines = set(view.section.split("\n"))
    for name in view.telemetry["tree_dirs_expanded"]:
        missing = [p for p in tree if p.startswith(name + "/") and p not in lines]
        assert not missing, f"{name} is on the ledger as expanded but {len(missing)} files are missing"


def test_a_directory_too_large_to_list_gives_shape_plus_whole_children():
    """An oversized selection must not collapse to nothing: `django/` alone is
    3.000 files, of which 2.500 are locale catalogues. Smallest child first, so
    the catalogues cannot starve db/ and forms/ — that starvation IS the window
    the old prefix produced."""
    tree = [f"monorepo/vendor/generated_{i}/bundle_file_number_{i}.ts" for i in range(6000)]
    tree += [f"monorepo/core/src/service_number_{i}.ts" for i in range(120)]
    tree += [f"monorepo/api/src/route_number_{i}.ts" for i in range(80)]

    view = acts._build_tree_view(tree, choose_dirs=lambda summary, total: ["monorepo"])

    assert view.telemetry["tree_dirs_summarized"] == ["monorepo"]
    assert view.telemetry["tree_dirs_expanded"] == ["monorepo/api", "monorepo/core"]
    assert "too many to list" in view.section
    assert "monorepo/vendor/ — 6000 files" in view.section
    assert 0 < view.telemetry["tree_delivered"] < len(tree)
    _assert_no_half_listings(view, tree)


# ---------------------------------------------------------------------------
# Degradation. Falling back to yesterday is fine; doing it quietly is not.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("choose", "expected_reason"),
    [
        (lambda summary, total: (_ for _ in ()).throw(TimeoutError("gateway")), "selection_failed: TimeoutError"),
        (lambda summary, total: ["src/", "app/"], "selection_named_no_real_directory"),
        (None, "selection_unavailable"),
    ],
)
def test_a_failed_first_pass_falls_back_to_the_old_prefix_and_says_so(choose, expected_reason):
    tree = _django_shaped_tree()

    view = acts._build_tree_view(tree, choose_dirs=choose)

    assert view.telemetry["tree_mode"] == "prefix_fallback"
    assert view.telemetry["tree_degraded_reason"].startswith(expected_reason)
    # Yesterday's behaviour: a prefix in tree order. Still produces a plan.
    assert view.section.startswith("\n## Repo tree (base branch) — PARTIAL: the first ")
    assert view.telemetry["tree_delivered"] > 0
    assert view.telemetry["tree_coverage"] < 1.0
    assert "needs_paths" in view.files_rule


def test_a_hallucinated_directory_is_never_expanded():
    tree = _django_shaped_tree()

    view = acts._build_tree_view(tree, choose_dirs=lambda summary, total: ["does/not/exist", "tests"])

    assert view.telemetry["tree_dirs_expanded"] == ["tests"]
    assert "does/not/exist" not in view.section


def test_the_fetch_asks_for_the_whole_tree_and_lets_failure_through(monkeypatch):
    """The client's own default is 300 paths. A directory summary computed from a
    300-path prefix would MISREPORT the repo's shape — the exact bug this route
    exists to fix — so the call site must ask for all of it, and must let a
    failed fetch reach the caller that records it."""
    import dse_validation.github.client as client_mod

    seen: dict = {}

    class _FakeClient:
        def get_tree_paths(self, repo, ref, *, limit):
            seen.update(repo=repo, ref=ref, limit=limit)
            return ["a.py"]

    monkeypatch.setattr(client_mod, "build_github_client", lambda cfg: _FakeClient())
    assert acts._repo_tree_for_planner("django/django", "main") == ["a.py"]
    assert seen["ref"] == "main"
    # torvalds/linux, the worst case measured, is 71.798 blobs.
    assert seen["limit"] >= 71_798

    def boom(cfg):
        raise RuntimeError("403")

    monkeypatch.setattr(client_mod, "build_github_client", boom)
    with pytest.raises(RuntimeError):
        acts._repo_tree_for_planner("django/django", "main")


def test_no_tree_at_all_is_recorded_as_such():
    view = acts._build_tree_view([], fetch_error="tree_fetch_failed: HTTPError")

    assert view.telemetry["tree_mode"] == "unavailable"
    assert view.telemetry["tree_degraded_reason"] == "tree_fetch_failed: HTTPError"
    assert view.section == ""
    assert "most likely paths" in view.files_rule


# ---------------------------------------------------------------------------
# The context budget (0.4): whole skills, or a name on the ledger
# ---------------------------------------------------------------------------


def test_the_live_registry_now_fits_whole():
    ctx = _live_registry_context()
    rendered, tel = acts._fit_planner_context(ctx, instruction="fix the DELETE handler")

    assert tel["skills_resolved"] == 21
    assert tel["skills_dropped"] == []
    assert len(tel["skills_delivered"]) == 21
    # The five that used to fall off the end are IN, by name.
    for key in ("redacting-pii-and-secrets", "writing-audit-trail-events"):
        assert key in rendered
    # And the section render() places after the skills survives — the old cut
    # let one section evict another.
    assert "## Repo map" in rendered


def test_the_old_blind_cut_is_what_this_replaces():
    """The bug, stated as a test: under the budget it used to run with, the live
    registry overflows and the cut lands in the middle of a title."""
    rendered = _live_registry_context().render(skill_body_chars=0)
    assert len(rendered) > 8000
    assert "## Repo map" not in rendered[:8000]
    assert not rendered[:8000].endswith("\n")  # mid-line, i.e. mid-title


def _title_chars_that_overflow() -> int:
    """Títulos grandes o bastante para o registry vivo (21 skills) estourar o
    budget — DERIVADO dele, não um número fixo.

    Eram 900 chars, calibrados contra o budget de 16.000. Quando ele subiu para
    40.000 (2026-08-26, para caber o AGENTS.md de um repositório real), o
    fixture parou de transbordar e o teste passou a provar nada — que é
    exatamente o que sua primeira asserção diz não aceitar."""
    return acts._PLANNER_CONTEXT_BUDGET_CHARS // 15


def test_a_skill_arrives_whole_or_is_named_on_the_ledger():
    ctx = _live_registry_context(title_chars=_title_chars_that_overflow())
    rendered, tel = acts._fit_planner_context(ctx, instruction="redacting pii and secrets in the audit trail")

    assert tel["skills_dropped"], "this fixture must overflow, or it proves nothing"
    assert len(tel["skills_delivered"]) + len(tel["skills_dropped"]) == tel["skills_resolved"]
    # Never half a skill: everything delivered is delivered with its full title.
    for skill in ctx.skills:
        if skill.skill_key in tel["skills_delivered"]:
            assert skill.title in rendered
        else:
            assert skill.skill_key not in rendered
    # Relevance decides who survives, so the skill the task is about is not the
    # one evicted by registry order.
    assert "redacting-pii-and-secrets" in tel["skills_delivered"]
    assert "writing-audit-trail-events" in tel["skills_delivered"]
    assert "## Repo map" in rendered


def test_dropping_skills_is_deterministic():
    ctx = _live_registry_context(title_chars=_title_chars_that_overflow())
    first = acts._fit_planner_context(ctx, instruction="anything at all")[1]
    second = acts._fit_planner_context(ctx, instruction="anything at all")[1]
    assert first["skills_dropped"] == second["skills_dropped"]


def test_a_context_with_no_skills_to_drop_is_cut_on_a_line_boundary():
    """The backstop for what dropping skills cannot buy back (a monorepo
    CODEOWNERS). Still never mid-line, and still counted."""
    ctx = PlannerContext(work_item_id="wi", tenant_id="t",
                         codeowners="\n".join(f"/path/{i}/ @team-{i}" for i in range(3000)))
    rendered, tel = acts._fit_planner_context(ctx, instruction="x", budget=2000)

    assert len(rendered) <= 2000
    assert tel["context_truncated_chars"] > 0
    assert rendered.endswith("[context truncated to the planner budget]")
    assert "@team-" in rendered


def test_a_non_dataclass_context_still_works():
    rendered, tel = acts._fit_planner_context(_Ctx(), instruction="x")
    assert "Fix DELETE" in rendered
    assert tel["skills_resolved"] == 0 and tel["skills_dropped"] == []


# ---------------------------------------------------------------------------
# The proposer: how many model calls, and what each one carries
# ---------------------------------------------------------------------------


def test_a_small_repo_spends_a_single_model_call(monkeypatch):
    chat = _Chat(plan={"steps": ["s"], "expected_files": ["src/module_1.js"], "test_plan": "t"})
    fetches = _patch_gateway(monkeypatch, chat, [f"src/module_{i}.js" for i in range(12)])
    tel: dict = {}

    proposal = acts._model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk", telemetry=tel)

    assert proposal is not None
    assert len(chat.prompts) == 1
    assert tel["planner_model_calls"] == 1
    assert tel["tree_coverage"] == 1.0
    assert len(fetches) == 1


def test_a_large_repo_spends_two_and_the_second_reuses_the_first(monkeypatch):
    chat = _Chat(directories=["django/db", "tests"])
    fetches = _patch_gateway(monkeypatch, chat, _django_shaped_tree())
    ctx = _live_registry_context()
    tel: dict = {}

    proposal = acts._model_plan_proposer(ctx, _inp(), headers=None, virtual_key="vk", telemetry=tel)

    assert proposal is not None
    assert len(chat.prompts) == 2
    assert tel["planner_model_calls"] == 2
    # The tree is fetched ONCE and both passes read the same list.
    assert len(fetches) == 1
    selection, plan = chat.prompts
    # Pass 1 is cheap on purpose: no skills context, no file listing.
    assert "redacting-pii-and-secrets" not in selection
    assert "django/db/models/fields/related_descriptors_1.py" not in selection
    assert len(selection) < 2000
    # Pass 2 carries the expansion AND the context, rendered once.
    assert "django/db/models/fields/related_descriptors_1.py" in plan
    assert "redacting-pii-and-secrets" in plan


def test_the_prompt_stops_claiming_the_window_is_authoritative(monkeypatch):
    chat = _Chat(directories=["tests"])
    _patch_gateway(monkeypatch, chat, _django_shaped_tree())

    acts._model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk", telemetry={})

    plan = chat.prompts[1]
    assert "Use ONLY paths from the tree below" not in plan
    assert "PARTIAL" in plan
    assert "needs_paths" in plan


def test_what_the_model_says_it_could_not_see_is_recorded(monkeypatch):
    chat = _Chat(directories=["tests"], plan={
        "steps": ["s"], "expected_files": ["django/db/models/x.py"], "test_plan": "t",
        "needs_paths": ["django/db/", "django/forms/"],
    })
    _patch_gateway(monkeypatch, chat, _django_shaped_tree())
    tel: dict = {}

    acts._model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk", telemetry=tel)

    assert tel["planner_unseen_paths_requested"] == ["django/db/", "django/forms/"]


def test_a_tree_fetch_failure_reaches_the_telemetry(monkeypatch):
    chat = _Chat(plan={"steps": ["s"], "expected_files": ["a.py"], "test_plan": "t"})
    _patch_gateway(monkeypatch, chat, RuntimeError("403 from the GitHub API"))
    tel: dict = {}

    proposal = acts._model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk", telemetry=tel)

    # The plan is still produced (the tree is context, not a requirement) —
    # but a Planner running blind no longer looks like one on a tiny repo.
    assert proposal is not None
    assert tel["tree_mode"] == "unavailable"
    assert tel["tree_degraded_reason"] == "tree_fetch_failed: RuntimeError"
    assert tel["tree_total"] == 0


def test_telemetry_survives_a_failed_plan_call(monkeypatch):
    """The case the ledger most needs to explain: the plan failed, and the
    question is what was in front of the model when it did."""
    chat = _Chat(directories=["tests"], plan={"steps": [], "expected_files": [], "test_plan": ""})
    _patch_gateway(monkeypatch, chat, _django_shaped_tree())
    tel: dict = {}

    assert acts._model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk", telemetry=tel) is None
    assert tel["tree_coverage"] > 0.0
    assert tel["planner_model_calls"] == 2


# ---------------------------------------------------------------------------
# The ledger (A1/A2): the fields have to be THERE, and findable
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        return None

    def fetchall(self):
        return self._rows


class _FakeSkillsConn:
    """The registry read without Postgres — `read_approved_skills` takes a conn."""

    def __init__(self, skills: list[Skill]):
        self._rows = [
            (s.tenant_id, s.skill_key, s.title, s.body, s.category, s.applies_to, None) for s in skills
        ]

    def cursor(self):
        return _FakeCursor(self._rows)


class _FakeRetrieval:
    def search(self, *args, **kwargs):
        return []

    def repo_map(self, *args, **kwargs):
        return ""


def _run_planner(monkeypatch, work_item_id, *, tree, chat, skills):
    import asyncio

    from dse_contracts import RunPlannerTurnInput

    rows: list[dict] = []
    monkeypatch.setenv(acts.SUBSTRATE_ENV_VAR, "claude-agent")
    monkeypatch.setattr(acts, "audit_emit", lambda **kw: rows.append(kw))
    monkeypatch.setattr(acts, "mint_virtual_key",
                        lambda headers: SimpleNamespace(virtual_key="vk", fixture=True))
    _patch_gateway(monkeypatch, chat, tree)

    plan = asyncio.run(acts._run_planner_turn_impl(
        RunPlannerTurnInput(work_item_id=work_item_id, tenant_id="fintex-poc",
                            instruction="fix the DELETE handler for transactions",
                            repo="django/django"),
        retrieval=_FakeRetrieval(),
        skills_conn=_FakeSkillsConn(skills),
    ))
    return plan, rows


def _details(rows, action):
    return [r["details"] for r in rows if r.get("action") == action]


@pytest.mark.usefixtures("state_dir")
def test_coverage_reaches_the_ledger(work_item_id, monkeypatch):
    """A1: planner_turn_completed carries tree_total, tree_delivered and
    tree_coverage. None of them existed — `jsonb_object_keys(details)` over
    every live row had no key matching %tree%."""
    tree = _django_shaped_tree()
    chat = _Chat(directories=["django/db", "tests"])

    plan, rows = _run_planner(monkeypatch, work_item_id, tree=tree, chat=chat,
                              skills=[_skill(k) for k in _LIVE_REGISTRY_KEYS])

    assert plan.expected_files
    details = _details(rows, "planner_turn_completed")[0]
    assert details["tree_total"] == len(tree)
    assert details["tree_delivered"] > len(_old_window(tree))
    assert 0.0 < details["tree_coverage"] <= 1.0
    assert details["tree_mode"] == "expanded"
    assert "tests" in details["tree_dirs_expanded"]
    assert details["planner_model_calls"] == 2
    # A2: and the skills tell the truth about delivery, not just resolution.
    assert details["skills_dropped"] == []
    assert len(details["skills_delivered"]) == len(details["skills_hydrated"]) == 21
    assert not _details(rows, "planner_tree_degraded")
    assert not _details(rows, "planner_context_truncated")


@pytest.mark.usefixtures("state_dir")
def test_a_degraded_tree_is_a_queryable_action(work_item_id, monkeypatch):
    """Falling back to yesterday's blind prefix is acceptable; needing to grep
    pod logs to find out it happened is not — hence an action, not a field."""
    chat = _Chat(directories=TimeoutError("gateway timed out"))

    _plan, rows = _run_planner(monkeypatch, work_item_id, tree=_django_shaped_tree(), chat=chat,
                               skills=[_skill(k) for k in _LIVE_REGISTRY_KEYS])

    degraded = _details(rows, "planner_tree_degraded")
    assert len(degraded) == 1
    assert degraded[0]["tree_mode"] == "prefix_fallback"
    assert degraded[0]["reason"].startswith("selection_failed: TimeoutError")
    assert degraded[0]["tree_delivered"] < degraded[0]["tree_total"]
    assert _details(rows, "planner_turn_completed")[0]["tree_mode"] == "prefix_fallback"


@pytest.mark.usefixtures("state_dir")
def test_a_dropped_skill_is_a_queryable_action(work_item_id, monkeypatch):
    chat = _Chat(plan={"steps": ["s"], "expected_files": ["a.py"], "test_plan": "t"})
    fat = [Skill(tenant_id="fintex-poc", skill_key=f"skill-{i:02d}", title="t" * 900,
                 body="b", category="engineering", applies_to=["default"]) for i in range(40)]

    _plan, rows = _run_planner(monkeypatch, work_item_id, tree=["a.py"], chat=chat, skills=fat)

    truncated = _details(rows, "planner_context_truncated")
    assert len(truncated) == 1
    assert truncated[0]["skills_resolved"] == 40
    assert truncated[0]["skills_dropped"]
    assert len(truncated[0]["skills_delivered"]) + len(truncated[0]["skills_dropped"]) == 40
    assert truncated[0]["context_chars"] <= truncated[0]["context_budget_chars"]


def test_the_escape_hatch_is_in_the_declared_schema_not_only_in_the_prose():
    """The partial rule tells the model to add "needs_paths". The format block
    above it declared a THREE-key object and the rules end with "Do not include
    anything besides the JSON" — so the one signal that distinguishes "planned
    with everything in view" from "planned around a gap" was contradicted by the
    schema the model is told to obey."""
    schema = acts._PLAN_PROMPT.split("Rules:")[0]
    assert "needs_paths" in schema
    assert "needs_paths" in acts._partial_files_rule(10, 100)


@pytest.mark.usefixtures("state_dir")
def test_a_directory_that_did_not_fit_is_a_queryable_degradation(work_item_id, monkeypatch):
    """`budget_exhausted` happens with tree_mode="expanded": the model named a
    directory and it did NOT arrive. That is a degradation like a failed pass 1,
    and gets the same action rather than a field nobody queries."""
    big = [f"a/{i:06d}_a_file_with_a_reasonably_long_name.py" for i in range(3300)]
    crowded = [f"b/subdirectory_number_{j:05d}/module_file_{k}.py" for j in range(900) for k in range(2)]
    chat = _Chat(directories=["a", "b"],
                 plan={"steps": ["s"], "expected_files": [big[0]], "test_plan": "t"})

    _plan, rows = _run_planner(monkeypatch, work_item_id, tree=big + crowded, chat=chat,
                               skills=[_skill(k) for k in _LIVE_REGISTRY_KEYS])

    degraded = _details(rows, "planner_tree_degraded")
    assert len(degraded) == 1
    assert degraded[0]["tree_mode"] == "expanded"
    assert degraded[0]["reason"] == "budget_exhausted: b"
    assert degraded[0]["tree_delivered"] < degraded[0]["tree_total"]


@pytest.mark.usefixtures("state_dir")
def test_a_backstop_cut_with_no_skill_dropped_is_still_queryable(work_item_id, monkeypatch):
    """A tenant with NO skills can still lose text (a monorepo CODEOWNERS, a
    large repo map): dropping whole skills cannot buy anything back, so the
    backstop cuts. Firing the action only on `skills_dropped` left exactly that
    case findable by reading rows instead of querying them."""
    real = acts._fit_planner_context

    def cut(ctx, *, instruction, budget=acts._PLANNER_CONTEXT_BUDGET_CHARS):
        rendered, tel = real(ctx, instruction=instruction, budget=budget)
        tel["skills_dropped"] = []
        tel["context_truncated_chars"] = 4321
        return rendered, tel

    monkeypatch.setattr(acts, "_fit_planner_context", cut)
    chat = _Chat(plan={"steps": ["s"], "expected_files": ["a.py"], "test_plan": "t"})

    _plan, rows = _run_planner(monkeypatch, work_item_id, tree=["a.py"], chat=chat, skills=[])

    truncated = _details(rows, "planner_context_truncated")
    assert len(truncated) == 1
    assert truncated[0]["skills_dropped"] == []
    assert truncated[0]["context_truncated_chars"] == 4321
