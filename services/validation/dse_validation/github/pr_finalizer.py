"""WSE-E3-T6 — deterministic PR finalizer: once L1 is green, pushes the branch
under the GitHub App's identity and opens EXACTLY 1 PR per WorkItem from a fixed
template. P1 (deterministic-or-human): no part of this function uses an LLM to
decide anything — title/body come from a fixed template, and the "create or not"
decision is purely based on database/GitHub state.

Idempotency (killing the process midway and re-running must not create a 2nd PR):
  1. check `wse_pr_tracking` (our fast source of truth);
  2. if absent, check the GitHub API for an open PR with head=branch — this covers
     the case where the process dies AFTER `create_pr()` and BEFORE
     `db.save_tracked_pr()` (exactly the window this module's crash-test
     exercises, see tests/test_pr_finalizer_idempotent.py);
  3. only create a new PR if neither source already has one open.
"""
from __future__ import annotations

import re

from dse_contracts import PrRef
from dse_contracts.paths import is_test_path

from dse_validation import db
from dse_validation.github.client import GitHubClient
from dse_validation.sandbox_exec import SandboxExecutor

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

STRICT_COMMENT_TEMPLATE = """### Fintex DSE — branch ready for review (strict mode)

The automated gates passed (L1 + L2). In this repository the **PR is opened by a
human** — no agent opens the PR (P1/strict mode, WSE-E3-T8).

- **WorkItem**: `{work_item_id}`
- **Branch**: `{branch}` (already pushed)
- **Open the PR (1 click)**: {compare_url}

When the PR is opened from this link, Fintex DSE correlates and adopts it
automatically (same WorkItem) — the rest of the flow (CI/status, human merge)
stays the same.
"""

PR_TITLE_TEMPLATE = "[DSE {work_item_id}] {summary}"

# Slack encodes mentions and links in its own markup. That markup only renders
# inside Slack: a mention arrives here as `<@U0BJR6U90UF>` and, spelled out in a
# PR title, reads as a raw id nobody can resolve.
_SLACK_MENTION_RE = re.compile(r"<[@#][UWBC][A-Z0-9]+(?:\|[^>]*)?>")
_SLACK_LABELED_LINK_RE = re.compile(r"<https?://[^|>]+\|([^>]+)>")
_SLACK_BARE_LINK_RE = re.compile(r"<(https?://[^>]+)>")


def clean_summary(summary: str) -> str:
    """Strip source-platform markup from text that becomes a PR title or body.

    The summary is the task text exactly as the human typed it on the origin
    platform, so it carries whatever markup that platform uses. Cleaning happens
    at PRESENTATION time only — the faithful snapshot stays in the
    ConversationEvent, which is what the TOCTOU defense (WSA-E2-T2) protects.
    """
    text = _SLACK_MENTION_RE.sub(" ", summary)
    text = _SLACK_LABELED_LINK_RE.sub(r"\1", text)
    text = _SLACK_BARE_LINK_RE.sub(r"\1", text)
    cleaned = " ".join(text.split())
    # A summary that is nothing but markup (a bare "@dse") would clean down to
    # an empty string, and GitHub rejects a PR with an empty title. Keeping the
    # original is ugly but always valid — never trade a cosmetic win for a
    # failed PR creation at the very end of a successful run.
    return cleaned or " ".join(summary.split())


def short_work_item_id(work_item_id: str) -> str:
    """Readable id prefix for the PR title.

    A work item id is `wi_` plus 64 hex chars. Spelled out in the title it eats
    the whole line in GitHub's PR list and buries what the change actually does.
    Nothing correlates by title: the branch name, the PR body and `tracked_prs`
    all carry the full id.
    """
    if work_item_id.startswith("wi_") and len(work_item_id) > 11:
        return work_item_id[:11]
    return work_item_id
PR_BODY_TEMPLATE = """### Fintex DSE — automatically generated PR

- **WorkItem**: `{work_item_id}`
- **Risk class**: `{risk_class}`
- **Summary**: {summary}
- **Test evidence (L1)**: {evidence_url}
{touched_tests_notice}
{issue_link}

---
This PR was opened by an autonomous agent (Fintex DSE). No agent session
approves or merges its own work (P3) — human review is mandatory before
merge.
"""


#: Quantos caminhos de teste o aviso lista antes de resumir. Curto de
#: propósito: o aviso aponta para onde olhar, não substitui o diff.
_MAX_LISTED_TEST_EDITS = 10


def touched_tests_notice(files_changed: list[str] | None) -> str:
    """Uma linha no corpo da PR quando o item alterou arquivo de teste.

    Desde 2026-08-10 o DSE altera qualquer teste — inclusive as specs que o
    próprio laço escreveu para julgar o Coder — e nada mais o contém: o revert
    pós-turno, o oráculo de autoria e o parque saíram juntos. A decisão de
    operador disse que a supervisão "muda de lugar: passa a ser o diff da PR".

    Uma linha de teste alterada no meio de um diff de 300 não é supervisão, é
    sorte. E o que está em jogo é o invariante que o repositório declara — *o
    DSE nunca aprova o próprio trabalho*: se o Coder pode afrouxar a asserção
    que o julga e ninguém aponta para isso, o gate humano existe no papel e não
    na prática. O L2 é vácuo e não há detector de spec enfraquecida, então este
    aviso é hoje a única coisa que dá lugar à supervisão que migrou.

    NÃO bloqueia nada: editar teste é trabalho legítimo, e o sistema pede isso
    ao Coder explicitamente. O aviso só faz o revisor começar sabendo onde
    olhar."""
    tests = [p for p in (files_changed or []) if is_test_path(p)]
    if not tests:
        return ""
    shown = sorted(tests)[:_MAX_LISTED_TEST_EDITS]
    listed = ", ".join(f"`{p}`" for p in shown)
    if len(tests) > len(shown):
        listed += f" (+{len(tests) - len(shown)} more)"
    return (
        f"- **⚠️ Test files changed by this item ({len(tests)})**: {listed}\n"
        "  Please review these first: does any assertion end up WEAKER than it "
        "was? Updating a test whose assertion this task deliberately changed is "
        "legitimate work; weakening one to make the suite pass is not, and "
        "nothing in the pipeline blocks it."
    )


#: Every git command this module runs, with the repository's own hooks turned
#: off ON THE COMMAND LINE.
#:
#: `-c` beats repo config, and that is the whole point. `ScopedGitSession`
#: already writes `core.hooksPath` into the workspace, but the L1 gate runs
#: `npm ci` long after that, the customer's `prepare` script is `husky`, and
#: husky points hooksPath straight back at `.husky/`. This module then pushes
#: without going through that session at all.
#:
#: Measured: `finalize_pr` died three times on
#: `git push failed (exit=-1): - Finding files` — the repository's `pre-push`
#: hook running `ng lint` on OUR push until the 60s clock ran out. L1 had
#: passed, L2 had passed, and the pull request was never opened.
#:
#: A nonexistent hooksPath is a harmless no-op to git, so this needs nothing
#: to exist on disk.
_GIT = ("git", "-c", "core.hooksPath=/nonexistent/dse-no-hooks")


def push_branch(executor: SandboxExecutor, github_client: GitHubClient, repo: str, branch: str, timeout: int = 60):
    """Push the task branch, refusing to clobber a commit we have not seen.

    The lease has to name the sha EXPLICITLY. `--force-with-lease` on its own
    leases against the remote-TRACKING ref, and there is none here: the remote
    is a bare URL, built fresh for each call so the token never lands in a git
    config. Git then treats the absent tracking ref as "the branch should not
    exist" and rejects every push after the first with `stale info`.

    Reproduced (git 2.50.1): first push to a new branch succeeds; the second,
    to the same branch, fails `! [rejected] HEAD -> feat (stale info)`. That
    breaks exactly the two paths this function exists for — re-finalizing after
    a review fix, and the activity's own retry after a partial failure — and it
    surfaces as a git error that hides whatever actually went wrong first.

    So: read the remote tip, and lease against THAT. The safety property is
    unchanged — a push still fails if someone moved the branch under us — and a
    branch that does not exist yet is created with a plain push."""
    remote_url = github_client.authenticated_remote_url(repo)
    ref = f"refs/heads/{branch}"

    ls = executor.run([*_GIT, "ls-remote", remote_url, ref], timeout=timeout)
    if ls.returncode != 0:
        raise RuntimeError(
            f"could not read the remote branch (exit={ls.returncode}): {ls.stderr.strip()[:300]}"
        )
    remote_sha = ls.stdout.split()[0] if ls.stdout.strip() else ""

    if remote_sha:
        # Leasing against the sha we just READ would be theatre: if someone else
        # moved the branch, we would read THEIR commit and lease against it, and
        # the push would sail through and discard their work. The property we
        # actually want is that the remote tip is something we already have —
        # i.e. this push fast-forwards.
        fetched = executor.run([*_GIT, "fetch", "-q", remote_url, ref], timeout=timeout)
        if fetched.returncode != 0:
            raise RuntimeError(
                f"could not fetch the remote branch (exit={fetched.returncode}): "
                f"{fetched.stderr.strip()[:300]}"
            )
        ancestry = executor.run(
            [*_GIT, "merge-base", "--is-ancestor", remote_sha, "HEAD"], timeout=timeout
        )
        if ancestry.returncode != 0:
            raise RuntimeError(
                f"git push failed (exit={ancestry.returncode}): refusing to overwrite "
                f"{ref}@{remote_sha[:12]} — it carries commits this branch does not have"
            )

    # No force. The DSE only ever appends to its own branch (the scope hook on
    # the checkpoint refuses non-fast-forwards), so a rejection here means the
    # remote genuinely diverged and must not be discarded. This is also what
    # makes the call idempotent: `--force-with-lease` with no argument leases
    # against a remote-TRACKING ref, and a bare URL remote has none, so git read
    # the absent ref as "this branch should not exist" and refused every push
    # after the first with `stale info` — breaking the re-finalize path and the
    # activity's own retry, with a git error that hid the original cause.
    result = executor.run([*_GIT, "push", remote_url, f"HEAD:{ref}"], timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"git push failed (exit={result.returncode}): {result.stderr.strip()}")
    return result


def compare_url_for(repo: str, base_branch: str, branch: str) -> str:
    """GitHub compare URL that opens the new-PR form already filled in
    (base <- head). `?expand=1` opens the form instead of the diff view (WSE-E3-T8)."""
    return f"https://github.com/{repo}/compare/{base_branch}...{branch}?expand=1"


def _adopt_and_ref(work_item_id: str, existing: dict, *, adopt: bool) -> PrRef:
    if adopt:
        db.adopt_tracked_pr(work_item_id, existing["number"], existing["html_url"])
    return PrRef(work_item_id=work_item_id, pr_number=existing["number"], url=existing["html_url"])


def adopt_pr_core(
    *,
    github_client: GitHubClient,
    work_item_id: str,
    tenant_id: str,
    repo: str,
    branch: str,
    pr_number: int | None = None,
    pr_url: str | None = None,
    actor: str = "system:validation",
) -> PrRef | None:
    """WSE-E3-T8 — called when the workflow detects that a human opened the PR
    from strict mode's compare link. Correlates by branch/WorkItem and ADOPTS the
    PR (fills pr_number on the same tracking row — same WorkItem). Idempotent: it
    only adopts if there was no pr_number yet. Returns the adopted PR's `PrRef`,
    or `None` if no open PR was found yet."""
    if pr_number is None:
        found = github_client.get_open_pr_for_branch(repo, branch)
        if found is None:
            return None
        pr_number, pr_url = found["number"], found["html_url"]
    if pr_url is None:
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"

    tracked = db.get_tracked_pr(work_item_id)
    already = tracked is not None and tracked.get("pr_number") is not None
    if tracked is None:
        db.save_tracked_pr(work_item_id, tenant_id, repo, branch, pr_number, pr_url)
    else:
        db.adopt_tracked_pr(work_item_id, pr_number, pr_url)

    if not already and audit_emit is not None:
        audit_emit(
            actor=actor,
            action="pr_adopted",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"repo": repo, "branch": branch, "pr_number": pr_number, "url": pr_url},
        )
    return PrRef(work_item_id=work_item_id, pr_number=pr_number, url=pr_url)


def finalize_pr_core(
    *,
    executor: SandboxExecutor,
    github_client: GitHubClient,
    work_item_id: str,
    tenant_id: str,
    repo: str,
    branch: str,
    base_branch: str,
    summary: str,
    risk_class: str,
    evidence_url: str = "",
    issue_ref: dict | None = None,
    actor: str = "system:validation",
    push: bool = True,
    strict_mode: bool = False,
    comment_writer=None,
    surface_ref: dict | None = None,
    files_changed: list[str] | None = None,
) -> PrRef:
    # 1) idempotency — our table is the fast source of truth.
    tracked = db.get_tracked_pr(work_item_id)
    if tracked is not None:
        if tracked.get("pr_number") is not None:
            # PR already open (by us in normal mode, or adopted in strict mode).
            # REFINALIZE (review fix cycle, observed live): this path arrives with
            # NEW commits on the branch — without a push, the Coder's fix never
            # showed up on the PR (silent no-op). The push is idempotent
            # (same tip -> up-to-date), so always pushing is safe.
            if push:
                push_branch(executor, github_client, repo, branch)
            return PrRef(work_item_id=work_item_id, pr_number=tracked["pr_number"], url=tracked["pr_url"])
        # Strict mode: compare link already posted, PR not open yet. A human may
        # have opened the PR in the meantime — detect and adopt it.
        existing = github_client.get_open_pr_for_branch(repo, branch)
        if existing is not None:
            return adopt_pr_core(
                github_client=github_client, work_item_id=work_item_id, tenant_id=tenant_id,
                repo=repo, branch=branch, pr_number=existing["number"], pr_url=existing["html_url"],
                actor=actor,
            )
        # Nobody has opened it yet — idempotent: return the SAME compare link.
        cmp_url = tracked.get("compare_url") or compare_url_for(repo, base_branch, branch)
        return PrRef(work_item_id=work_item_id, pr_number=None, url=cmp_url, compare_url=cmp_url)

    # 2) defense in depth: GitHub may already have the PR if a previous run died
    #    between create_pr() and save_tracked_pr() (or a human already opened it).
    existing = github_client.get_open_pr_for_branch(repo, branch)
    if existing is not None:
        db.save_tracked_pr(work_item_id, tenant_id, repo, branch, existing["number"], existing["html_url"])
        return PrRef(work_item_id=work_item_id, pr_number=existing["number"], url=existing["html_url"])

    # 3) push the branch under the GitHub App's identity (both modes push).
    if push:
        push_branch(executor, github_client, repo, branch)

    # 3b) STRICT MODE (WSE-E3-T8): does NOT open the PR — posts a compare link and stops.
    if strict_mode:
        cmp_url = compare_url_for(repo, base_branch, branch)
        db.save_tracked_pr(
            work_item_id, tenant_id, repo, branch, pr_number=None, pr_url=cmp_url, compare_url=cmp_url
        )
        if comment_writer is not None and surface_ref is not None:
            comment_writer.upsert(
                work_item_id,
                surface_ref,
                STRICT_COMMENT_TEMPLATE.format(
                    work_item_id=work_item_id, branch=branch, compare_url=cmp_url
                ),
            )
        if audit_emit is not None:
            audit_emit(
                actor=actor,
                action="pr_compare_link_posted",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"repo": repo, "branch": branch, "compare_url": cmp_url},
            )
        return PrRef(work_item_id=work_item_id, pr_number=None, url=cmp_url, compare_url=cmp_url)

    # 4) create the PR from the fixed template — back-links the originating issue.
    issue_link = ""
    if issue_ref and issue_ref.get("issue_number"):
        issue_link = f"Closes #{issue_ref['issue_number']}"
    # Title carries the SHORT id (readable in a PR list); the body keeps the full
    # one, which is what a human copies to look the work item up.
    title = PR_TITLE_TEMPLATE.format(
        work_item_id=short_work_item_id(work_item_id), summary=clean_summary(summary)
    )
    body = PR_BODY_TEMPLATE.format(
        work_item_id=work_item_id,
        risk_class=risk_class,
        summary=clean_summary(summary),
        evidence_url=evidence_url or "(no evidence link)",
        issue_link=issue_link or "(no linked source issue)",
        touched_tests_notice=touched_tests_notice(files_changed),
    )
    pr = github_client.create_pr(repo, head=branch, base=base_branch, title=title, body=body)

    db.save_tracked_pr(work_item_id, tenant_id, repo, branch, pr["number"], pr["html_url"])

    if audit_emit is not None:
        audit_emit(
            actor=actor,
            action="pr_finalized",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"repo": repo, "branch": branch, "pr_number": pr["number"], "url": pr["html_url"]},
        )

    return PrRef(work_item_id=work_item_id, pr_number=pr["number"], url=pr["html_url"])
