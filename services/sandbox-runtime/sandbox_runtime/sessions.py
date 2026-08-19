"""Phase 2 stage-scoped sessions (WSC-E3-T3/T4/T5): Planner context hydration,
DETERMINISTIC risk classifier, scripted session runner (which ALWAYS executes
tools through the toolset), and the fresh-context Reviewer session.

P1 (no flow decision made by an LLM): the PlanArtifact's `risk_class` — which
drives WS-B's approval gate — is derived by `classify_risk_class`
(deterministic code over the declared blast radius), NOT by the LLM's word. The
Planner session (LLM) proposes steps/expected_files/test_plan; the risk
classification is a deterministic floor the LLM cannot lower. That way the
downstream gate never depends on a model's judgement.

P3 (no producer approves its own work): `FreshReviewerSession` is built ONLY
from `PlanArtifact` + diff — there is no parameter, attribute or channel that
carries the Coder's history into it (proven by construction in
`tests/test_reviewer_fresh_context.py`).
"""
from __future__ import annotations

import fnmatch
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dse_contracts import L2Verdict, PlanArtifact
from dse_contracts.paths import first_forbidden_match

from .retrieval import RetrievalHit, RetrievalService, render_untrusted_context
from .skill_registry import Skill, read_approved_skills
from .toolsets import ReviewerToolset, Toolset, ToolInvocation


# ---------------------------------------------------------------------------
# Deterministic risk classifier (risk floor — P1)
# ---------------------------------------------------------------------------
# Globs whose touch raises the risk. Sensitive = requires human plan approval in
# WS-B (the risk-class gate). These are conservative patterns from the regulated
# fintech domain; a per-tenant access bundle (WS-F) may extend them.
_HIGH_RISK_GLOBS = [
    ".github/workflows/*",
    "**/migrations/*",
    "migrations/*",
    "**/*auth*",
    "**/*payment*",
    "**/*billing*",
    "**/secrets*",
    "infra/*",
    "**/Dockerfile*",
]
_MEDIUM_RISK_GLOBS = [
    "**/*.sql",
    "**/config*",
    "**/settings*",
    "pyproject.toml",
    "requirements*.txt",
    "package.json",
]


def _matches_any(path: str, globs: list[str]) -> bool:
    p = path.replace("\\", "/")
    return any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(p, g.lstrip("*/")) for g in globs)


def classify_risk_class(
    expected_files: list[str],
    estimated_lines: int | None,
    forbidden_paths: list[str] | None = None,
) -> str:
    """Derive risk_class ('low'|'medium'|'high') DETERMINISTICALLY from the
    declared blast radius. Rules (a floor — WS-B's gate decides what to do with
    each level):
      - high  : touches any forbidden_path OR any _HIGH_RISK_GLOB, OR the
                Planner's estimate > 800 lines;
      - medium: touches any _MEDIUM_RISK_GLOB OR estimate > 300 lines OR
                > 15 files;
      - low   : otherwise.

    `estimated_lines` é a estimativa DECLARADA pelo Planner (rc.89). `None` =
    sem estimativa: os critérios de linhas são PULADOS e o risco vem só de
    arquivos/globs — não se inventa um número para classificar. Antes daqui, o
    parâmetro era `diff_budget_lines`, a constante 400 do contrato: `400 > 300`
    era sempre verdadeiro, todo plano saía >= medium, e os ramos `> 800 → high`
    e `return "low"` eram código morto.
    """
    forbidden = forbidden_paths or []
    for f in expected_files:
        fp = f.replace("\\", "/")
        # Um matcher só, e ele mora no contrato (2026-08-19). O `startswith` +
        # `fnmatch` que vivia aqui estava preso à raiz, enquanto o gate L1 casa
        # segmento em qualquer profundidade: num monorepo,
        # `packages/web/.github/workflows/ci.yml` saía "low" daqui e violação de
        # lá. Como a política só parqueia "high", o plano nem chegava ao gate — o
        # humano nunca era perguntado sobre o caminho protegido que o L1 ia
        # reprovar depois.
        if first_forbidden_match(fp, forbidden):
            return "high"
        if _matches_any(fp, _HIGH_RISK_GLOBS):
            return "high"
    if estimated_lines is not None and estimated_lines > 800:
        return "high"
    if (estimated_lines is not None and estimated_lines > 300) or len(expected_files) > 15:
        return "medium"
    for f in expected_files:
        if _matches_any(f, _MEDIUM_RISK_GLOBS):
            return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Planner context hydration (WSC-E3-T3)
# ---------------------------------------------------------------------------
@dataclass
class PlannerContext:
    """Planner context bundle. `skills`/`agents_md`/`codeowners` are TRUSTED
    (curated/versioned in the tenant's repo). `retrieval_hits` and `tickets` are
    UNTRUSTED (external/code input) and are rendered inside a block marked by
    `render_untrusted_context`."""

    work_item_id: str
    tenant_id: str
    agents_md: str = ""
    codeowners: str = ""
    skills: list[Skill] = field(default_factory=list)
    tickets: list[str] = field(default_factory=list)
    retrieval_hits: list[RetrievalHit] = field(default_factory=list)
    repo_map: str = ""
    #: The diário de bordo (migration 0036), pre-rendered. Machine-authored and
    #: therefore NOT trusted the way AGENTS.md and the skills are: it is a record
    #: of what happened, ranked below curated guidance, and the first block
    #: `_fit_planner_context` drops when the budget binds.
    run_episodes: str = ""
    #: `repo@ref` the trusted docs were read at, rendered into their header. The
    #: model is told WHICH commit-ish it is being shown, because "the repo says"
    #: and "the repo said on this branch at this moment" are different claims.
    doc_ref: str = ""

    def render(self, *, skill_body_chars: int | None = None) -> str:
        """`skill_body_chars` is forwarded to each skill's context block — see
        Skill.as_context_block. The default keeps the full bodies so no existing
        caller changes behaviour; the Planner passes 0 because its context is
        cut to a fixed budget and full bodies would evict everything below."""
        parts: list[str] = [f"# Planner context — work_item {self.work_item_id} (tenant {self.tenant_id})"]
        at_ref = f" — {self.doc_ref}" if self.doc_ref else ""
        if self.agents_md:
            parts.append(f"## AGENTS.md (trusted{at_ref})\n" + self.agents_md)
        if self.codeowners:
            parts.append(f"## CODEOWNERS (trusted{at_ref})\n" + self.codeowners)
        if self.skills:
            parts.append(
                "## Approved tenant skills (trusted)\n"
                + "\n\n".join(s.as_context_block(body_chars=skill_body_chars) for s in self.skills)
            )
        # After the curated blocks and before the repo map: it outranks a machine
        # index of the code, and never outranks a human's conventions or skills.
        if self.run_episodes:
            parts.append(self.run_episodes)
        if self.repo_map:
            parts.append("## Repo map\n" + self.repo_map)
        # Untrusted last, clearly demarcated.
        untrusted_blocks: list[str] = []
        if self.tickets:
            untrusted_blocks.append("Related tickets:\n" + "\n---\n".join(self.tickets))
        if self.retrieval_hits:
            untrusted_blocks.append(render_untrusted_context(self.retrieval_hits))
        if untrusted_blocks:
            parts.append("\n".join(untrusted_blocks))
        return "\n\n".join(parts)


def _read_repo_file(workspace_dir: str, rel: str) -> str:
    p = Path(workspace_dir) / rel
    try:
        return p.read_text()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""


def hydrate_planner_context(
    *,
    work_item_id: str,
    tenant_id: str,
    repo: str,
    instruction: str,
    task_class: str = "default",
    related_tickets: list[str] | None = None,
    retrieval: RetrievalService | None = None,
    skills_conn=None,
    agents_md: str | None = None,
    codeowners: str | None = None,
    doc_ref: str = "",
    run_episodes: str = "",
) -> PlannerContext:
    """Assemble the `PlannerContext`: AGENTS.md + CODEOWNERS, the tenant's
    approved skills (skill_registry, E4), related tickets, and the top-k snippets
    from the retrieval index (E5) relevant to the instruction. The Planner is
    read-only — nothing here mutates the repo.

    `agents_md`/`codeowners` are supplied by the caller, read from the base ref
    through the GitHub API. They used to be read off `workspace_dir` — which is
    created by `provision_sandbox`, and the workflow runs the Planner BEFORE that
    (workflows.py:1693 vs :1697), so on the docker profile the directory did not
    exist yet and under `sandboxDriver: k8s` it is never created on the worker at
    all. Both docs were therefore '' on every production turn since the block was
    written. Reading at the ref also means an agent-authored file in a workspace
    can never surface under a `(trusted)` header.

    None means the caller could not ask (no GitHub App, transport failure); '' or
    a value means it asked and this is the answer. The Planner distinguishes the
    two on the ledger — see `_repo_docs_for_planner`."""
    agents_md = agents_md or ""
    codeowners = codeowners or ""
    # Per-repo checkboxes from the console (repo_scope, migration 0029): the
    # Planner only sees skills that are global or ticked for THIS repo.
    skills = read_approved_skills(tenant_id, task_class=task_class, repo=repo or None, conn=skills_conn)

    hits: list[RetrievalHit] = []
    repo_map = ""
    if retrieval is not None:
        hits = retrieval.search(tenant_id, instruction, k=5, repo=repo)
        repo_map = retrieval.repo_map(tenant_id, repo)

    return PlannerContext(
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        agents_md=agents_md,
        codeowners=codeowners,
        skills=skills,
        tickets=list(related_tickets or []),
        retrieval_hits=hits,
        repo_map=repo_map,
        doc_ref=doc_ref,
        run_episodes=run_episodes,
    )


# ---------------------------------------------------------------------------
# Scripted session runner — runs every tool through the toolset (test P0)
# ---------------------------------------------------------------------------
@dataclass
class ToolResult:
    tool: str
    ok: bool
    detail: Any = None


class ScriptedAgentSession:
    """Scripted substrate for Planner/Tester (analogous to the Coder's
    FakeSubstrate): it calls no LLM, it executes a list of `ToolInvocation`s.
    EVERY invocation goes through `toolset.check` BEFORE dispatch — that is what
    makes the conformance test real (a write in the Planner raises an actual
    `ToolPermissionError`, not one by convention).

    In production the OpenHands adapter registers only the tools whose names are
    in the toolset's allowlist and routes every tool-call through the same
    `check` — see the README (the same pattern as the Coder's
    `OpenHandsSubstrate` in Phase 1).
    """

    def __init__(
        self,
        *,
        toolset: Toolset,
        workspace_dir: str,
        retrieval: RetrievalService | None = None,
        tenant_id: str = "",
        repo: str = "",
        context_reads: dict[str, str] | None = None,
    ):
        self.toolset = toolset
        self.workspace_dir = workspace_dir
        self._retrieval = retrieval
        self._tenant_id = tenant_id
        self._repo = repo
        self._context_reads = context_reads or {}
        self.log: list[ToolResult] = []

    def invoke(self, tool: str, **args: Any) -> ToolResult:
        inv = ToolInvocation(tool=tool, args=args)
        # 1) enforcement — raises ToolPermissionError when denied.
        self.toolset.check(inv)
        # 2) dispatch of the permitted tool.
        detail = self._dispatch(inv)
        res = ToolResult(tool=tool, ok=True, detail=detail)
        self.log.append(res)
        return res

    def run_script(self, script: list[dict[str, Any]]) -> list[ToolResult]:
        results = []
        for step in script:
            tool = step["tool"]
            args = {k: v for k, v in step.items() if k != "tool"}
            results.append(self.invoke(tool, **args))
        return results

    def _dispatch(self, inv: ToolInvocation) -> Any:
        t, a = inv.tool, inv.args
        if t == "read_file":
            return _read_repo_file(self.workspace_dir, a["path"])
        if t == "write_file":
            p = Path(self.workspace_dir) / a["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(a["content"])
            return str(p)
        if t == "search_code":
            if self._retrieval is None:
                return []
            return self._retrieval.search(self._tenant_id, a["query"], k=a.get("k", 5), repo=self._repo or None)
        if t == "repo_map":
            if self._retrieval is None:
                return ""
            return self._retrieval.repo_map(self._tenant_id, self._repo)
        if t == "run_tests":
            return self._run_tests(a.get("paths"))
        if t in ("read_ticket", "read_agents_md", "read_codeowners", "list_skills"):
            return self._context_reads.get(t, "")
        # read_plan/read_diff are served by FreshReviewerSession, not here.
        return None

    def _run_tests(self, paths: list[str] | None) -> dict[str, Any]:
        """Actually RUN the tests inside the workspace (WSC-E3-T4: the written
        tests run in L1, they are not merely generated). DETERMINISTIC runner
        detection (P1): Node repo (package.json with a `test` script) →
        `npm test`; otherwise pytest. Returns rc + a trimmed stdout."""
        import json as _json
        import os as _os

        pkg = _os.path.join(self.workspace_dir, "package.json")
        cmd: list[str]
        env = {**_os.environ, "CI": "1"}
        if _os.path.isfile(pkg):
            try:
                has_test = "test" in (_json.load(open(pkg)).get("scripts") or {})
            except Exception:  # noqa: BLE001 — invalid package.json => pytest
                has_test = False
            if has_test:
                # deps first (a fresh clone has no node_modules); best-effort —
                # if it fails, the npm test below reports the real error.
                if not _os.path.isdir(_os.path.join(self.workspace_dir, "node_modules")):
                    subprocess.run(
                        ["npm", "install", "--no-audit", "--no-fund"],
                        cwd=self.workspace_dir, capture_output=True, text=True, timeout=600,
                    )
                cmd = ["npm", "test", "--silent"]
            else:
                cmd = [sys.executable, "-m", "pytest", "-q"]
        else:
            cmd = [sys.executable, "-m", "pytest", "-q"]
            if paths:
                cmd += paths
        try:
            proc = subprocess.run(
                cmd, cwd=self.workspace_dir, capture_output=True, text=True,
                timeout=600, env=env,
            )
            rc, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc, out, err = 124, (exc.stdout or ""), f"timeout 600s: {exc.cmd}"
        return {
            "returncode": rc,
            "passed": rc == 0,
            "command": " ".join(cmd),
            "stdout_tail": (out or "")[-4000:],
            "stderr_tail": (err or "")[-2000:],
        }


# ---------------------------------------------------------------------------
# Fresh-context Reviewer session (WSC-E3-T5) — P3 by construction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReviewerContext:
    """IMMUTABLE, MINIMAL context of the L2 session: ONLY the plan + the final
    diff.

    There is deliberately NO field that carries the Coder's history, turn logs,
    thoughts, tool-calls, or any transcript of the producing session. This is the
    "by construction" part of P3: the only way to give the Reviewer context is
    through these two fields.
    """

    work_item_id: str
    plan: PlanArtifact
    diff: str

    def render(self) -> str:
        # rc.89: `diff_budget_lines` saiu daqui — era a constante 400 do
        # contrato apresentada ao Reviewer sob "must adhere to", um teto FALSO
        # (nunca dimensionado; o gate L1 de diff já é informativo). A estimativa
        # do Planner entra como INFORMAÇÃO quando existe; sem ela, nenhuma
        # linha sobre tamanho.
        est = (
            f"planner_estimated_lines: {self.plan.estimated_lines} "
            "(informational estimate, not a limit)\n"
            if getattr(self.plan, "estimated_lines", None)
            else ""
        )
        return (
            f"# L2 review — work_item {self.work_item_id}\n\n"
            f"## Plan the diff must adhere to\n"
            f"steps: {self.plan.steps}\n"
            f"expected_files (declared blast radius): {self.plan.expected_files}\n"
            f"{est}"
            f"test_plan: {self.plan.test_plan}\n"
            f"risk_class: {self.plan.risk_class}\n"
            f"forbidden_paths: {self.plan.forbidden_paths}\n\n"
            f"## Final diff to review\n{self.diff}"
        )


class FreshReviewerSession:
    """A FRESH session (no state shared with the Coder) that judges the diff's
    adherence to the plan/conventions. It receives ONLY `ReviewerContext`
    (plan+diff).

    The judgement itself (producing objections) belongs to the substrate — in
    production, a fresh OpenHands conversation seeded solely with
    `context.render()`. Here, for testing, `review()` accepts a deterministic
    `verdict_fn(context) -> (passed, objections)` (the scripted "model"), proving
    all the plumbing without real inference. P1/P3 note: the L2 verdict is a
    RECOMMENDATION that gates progression — the merge remains human (no flow
    decision made by an LLM); and because the session is fresh, it is never the
    producer approving its own work.
    """

    def __init__(self, context: ReviewerContext, toolset: ReviewerToolset | None = None):
        self.context = context
        self.toolset = toolset or ReviewerToolset()

    def read_plan(self) -> PlanArtifact:
        self.toolset.check(ToolInvocation(tool="read_plan", args={}))
        return self.context.plan

    def read_diff(self) -> str:
        self.toolset.check(ToolInvocation(tool="read_diff", args={}))
        return self.context.diff

    def review(self, verdict_fn) -> L2Verdict:
        passed, objections, cost = verdict_fn(self.context)
        return L2Verdict(
            work_item_id=self.context.work_item_id,
            passed=passed,
            objections=list(objections),
            cost_usd=float(cost),
        )
