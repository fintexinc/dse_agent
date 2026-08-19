"""The DSE writes a repository's FIRST `.dse/validation.json` — as a PR.

A repository without the manifest cannot be validated (every L1 gate reads it)
and, before this module, burned Planner + sandbox + one Coder turn + one Tester
turn before L1 discovered the absence and escalated. The operator hand-wrote
the calculation-engine-service manifest; that does not scale past the pilot.

The flow is `repo_doc.propose_agents_md`'s (single-file PR, no sandbox, refusal
guards), with one rule that module does not need: **the model's output only
becomes a PR after the REAL parser accepts it** (`L1Config._from_manifest_payload`
must return PASS). The DSE never proposes a manifest the DSE itself would
reject — prompt quality affects the retry rate, never the safety.

Grounding is deterministic and mirrored, not invented: the file tree's build
markers (`repo_doc.derive_facts`) plus the repository's OWN CI files
(azure-pipelines.yml / .github/workflows), so the proposed commands are the
commands the repo already runs elsewhere.
"""
from __future__ import annotations

import json
import logging

from .repo_doc import derive_facts

logger = logging.getLogger("sandbox_runtime.manifest_bootstrap")

#: Idempotency by branch name, like repo_doc: an open PR is reused, never
#: duplicated — resending the task while it is open costs zero model calls.
BRANCH = "dse/bootstrap-validation-manifest"

MANIFEST_PATH = ".dse/validation.json"

#: CI files worth mirroring, in the order we look for them.
_CI_CANDIDATES = ("azure-pipelines.yml", ".azuredevops/azure-pipelines.yml",
                  "Jenkinsfile", ".gitlab-ci.yml")
_CI_MAX_CHARS = 4_000
_CI_MAX_FILES = 3

# A distilled spec of docs/DSE-VALIDATION-MANIFEST.md. Embedded because the
# orchestrator image does not ship docs/ — and the REAL guarantee is the parser
# gate below, not this prose.
_SPEC = """The manifest is a JSON object. Rules, all binding:
- "version" must be exactly 1.
- "commands" is an object with any of: lint, typecheck, test, build. Each is an
  ARGV ARRAY of strings, never a shell string. A command you cannot ground in
  the repository's own CI or build files must be OMITTED, not invented.
- Write "build" as ["sh", "-c", "<the whole line>"]: the preview recipe
  executes it, and chained commands must use "&&", never ";" (";" discards the
  exit status of everything but the last command).
- Optional: "timeout_seconds" (int, 1..3600) when the CI shows long builds;
  "timeouts" object (lint/typecheck/test/build/sast/secret_scan -> seconds).
- Do NOT include "preview", "forbidden_paths" or "disabled_stages" unless the
  facts demand them — the maintainer adds those deliberately.
- Mirror the repository's own CI commands wherever they exist. Prefer the
  repo's wrapper (./mvnw, ./gradlew) over a bare tool when the tree has one.
Return ONLY the JSON object. No markdown fences, no prose."""

_PROMPT = """You are drafting `.dse/validation.json` for a repository the DSE
is onboarding. This file is the repository's validation contract: the DSE runs
exactly these commands as merge gates on every change it proposes.

{spec}

These facts were derived from the repository's file tree. They are true.

{facts}

{ci_block}"""

_PR_BODY = """This repository has no `.dse/validation.json`, and the DSE cannot
validate its own changes here without one — every gate (lint, typecheck, test,
build) reads its command from this file, at the base commit.

This is a **first draft, not a decision**. Every command in it was mirrored
from this repository's own CI and build files where they exist; nothing was
invented. The draft was accepted by the same parser that will enforce it — but
the commands deserve your eye: they will run as merge gates on every DSE task
in this repository.

Review it, adjust it, merge it — then resend the task that triggered this PR.
Reference for every field: `docs/DSE-VALIDATION-MANIFEST.md` in the DSE
repository.

Facts this draft was derived from:

{facts}
"""


def probe_manifest(client, repo: str, base_branch: str) -> dict:
    """Three distinct answers, because two of them look alike and must not:
    `present` (manifest exists), absent (confirmed 404 — the only trigger for
    a bootstrap PR), and `reachable=False` (could not ask — fail-open, the
    flow proceeds and L1 keeps being the one that breaks the hard news)."""
    try:
        texto = client.get_file_text(repo, MANIFEST_PATH, base_branch)
    except Exception as exc:  # noqa: BLE001 — API hiccup must not kill a task
        logger.info("manifest probe failed for %s@%s: %s", repo, base_branch, exc)
        return {"present": False, "reachable": False}
    return {"present": texto is not None, "reachable": True}


def _ci_snippets(client, repo: str, base_branch: str, tree: list[str]) -> str:
    """The repository's OWN CI, verbatim (capped), so the draft mirrors real
    commands instead of inventing plausible ones."""
    paths = [p for p in _CI_CANDIDATES if p in tree]
    paths += sorted(p for p in tree
                    if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml")))
    trechos: list[str] = []
    for path in paths[:_CI_MAX_FILES]:
        try:
            texto = client.get_file_text(repo, path, base_branch)
        except Exception:  # noqa: BLE001 — grounding é best-effort
            texto = None
        if texto:
            trechos.append(f"--- {path} ---\n{texto[:_CI_MAX_CHARS]}")
    if not trechos:
        return ("This repository has no CI configuration to mirror. Derive the "
                "commands from the build system facts alone, and omit any "
                "command you cannot ground.")
    return ("The repository's existing CI, to MIRROR (these are the commands "
            "that already run):\n\n" + "\n\n".join(trechos))


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`\n")
        text = text[4:] if text.startswith("json") else text
    return text.strip()


def _parser_verdict(payload_text: str) -> tuple[dict | None, str]:
    """(payload, "") when the REAL parser accepts; (None, reason) otherwise."""
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        return None, f"the draft is not valid JSON: {exc.msg}"
    try:
        from dse_validation.config import L1Config, L1ManifestError
    except ImportError:  # pragma: no cover — dse_validation ships with the worker
        return None, "dse_validation unavailable to validate the draft"
    try:
        cfg = L1Config._from_manifest_payload(payload, source="bootstrap-draft")
    except L1ManifestError as exc:
        return None, f"the real manifest parser rejected the draft: {exc.detail}"
    if cfg.manifest_status.value != "PASS":
        return None, f"the real manifest parser rejected the draft: {cfg.manifest_detail}"
    return payload, ""


def bootstrap_manifest(client, repo: str, base_branch: str, *, complete) -> dict:
    """Draft, validate, and open the bootstrap PR. `complete` is the model
    call (prompt -> text) — injected so the gate logic is testable without a
    gateway.

    Returns {"ok", "pr_number", "url", "existing", "reason"} and NEVER raises
    for content reasons: the caller decides what a refusal means (today:
    escalate pointing the human at the manifest reference doc)."""
    existing = client.get_open_pr_for_branch(repo, BRANCH)
    if existing:
        return {"ok": True, "pr_number": existing["number"],
                "url": existing.get("html_url"), "existing": True, "reason": ""}

    tree = client.get_tree_paths(repo, base_branch, limit=200_000)
    facts = derive_facts(tree)
    prompt = _PROMPT.format(
        spec=_SPEC,
        facts=facts.as_bullets(),
        ci_block=_ci_snippets(client, repo, base_branch, tree),
    )
    draft = _strip_fences(complete(prompt))
    payload, reason = _parser_verdict(draft)
    if payload is None:
        logger.warning("manifest bootstrap refused for %s: %s", repo, reason)
        return {"ok": False, "pr_number": None, "url": None,
                "existing": False, "reason": reason}

    base_sha = client.get_ref_sha(repo, base_branch)
    if not base_sha:
        return {"ok": False, "pr_number": None, "url": None, "existing": False,
                "reason": f"base branch {base_branch!r} has no resolvable SHA"}
    client.create_branch(repo, BRANCH, base_sha)
    client.put_file(
        repo, MANIFEST_PATH,
        content=json.dumps(payload, indent=2) + "\n",
        message="Bootstrap the DSE validation manifest (proposed by the DSE)",
        branch=BRANCH,
    )
    pr = client.create_pr(
        repo, BRANCH, base_branch,
        "Bootstrap .dse/validation.json — the DSE's validation contract",
        _PR_BODY.format(facts=facts.as_bullets()),
    )
    return {"ok": True, "pr_number": pr["number"], "url": pr.get("html_url"),
            "existing": False, "reason": ""}
