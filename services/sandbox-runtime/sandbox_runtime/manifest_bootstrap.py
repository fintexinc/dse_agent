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
#: Branch PRÓPRIA da emenda: bootstrap CRIA o arquivo, emenda o ALTERA, e um
#: repositório pode precisar dos dois em momentos diferentes.
BRANCH_AMEND = "dse/amend-validation-manifest"

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
- "commands.test_subset" is the SAME suite restricted to given files: the DSE
  appends the paths of the tests it just wrote. Declare it whenever the runner
  accepts file paths as trailing arguments (jest, vitest, pytest, go test,
  rspec, phpunit) and include whatever flag that repository needs for a PARTIAL
  run to be meaningful — a jest with `collectCoverage: true` and global
  thresholds needs "--coverage=false", or every subset fails on coverage while
  every test passes. Omit it for runners that take filters rather than paths
  (maven, dotnet, cargo): the DSE then runs the whole "test" command.
- "install" (TOP LEVEL, not inside "preview") is the dependency step, an ARGV
  ARRAY: ["npm","install","--no-audit","--no-fund"], ["go","mod","download"],
  ["pip","install","-r","requirements.txt"]. Both the test sandbox and the
  preview Pod run it. Omit it when the tree needs no install step.
- "commands.lint_fix" is the command that FIXES what "lint" refuses — the
  formatter's write mode, not its check mode: ["./mvnw","-B","-q",
  "spotless:apply"], ["ruff","format","."], ["npx","prettier","--write","."],
  ["gofmt","-w","."], ["dotnet","format"], ["bundle","exec","rubocop","-a"],
  ["cargo","fmt"]. Declare it whenever "lint" is a formatter that has one.
  Why it matters: when the lint gate refuses, the DSE runs THIS command before
  spending a model turn. Without it, a model rewrites the file by hand to match
  a formatter — measured at four paid turns without converging on what the
  formatter fixes in seconds. Omit it when "lint" is a pure analyser with no
  write mode (a type checker, a security linter): a command that cannot fix
  anything only costs time.
- "reports": {"junit": "<relative glob>"} — where the test run leaves its JUnit
  XML. Declare it whenever the repository's test command ALREADY writes JUnit
  (maven/surefire and gradle always do: "target/surefire-reports/*.xml",
  "build/test-results/test/*.xml"), or when the runner writes it with a
  BUILT-IN flag you add to "commands.test": pytest ("--junitxml=reports/
  junit.xml"), phpunit ("--log-junit=reports/junit.xml"), dotnet ("--logger",
  "junit;LogFilePath=reports/junit.xml"). NEVER add a reporter that needs a
  package the repository does not already depend on. The glob is plain: letters,
  digits, . _ - / * ? only — no spaces, quotes or shell characters.
  Why it matters: without it this platform reads the run's counts out of stdout,
  and it only understands pytest, jest and surefire prose. A green Go, cargo,
  rspec or phpunit suite then produces no readable count at all.
- Optional: "timeout_seconds" (int, 1..3600) when the CI shows long builds;
  "timeouts" object (lint/typecheck/test/build/sast/secret_scan -> seconds).
- Do NOT include "forbidden_paths" or "disabled_stages" unless the facts demand
  them — the maintainer adds those deliberately.
- Mirror the repository's own CI commands wherever they exist. Prefer the
  repo's wrapper (./mvnw, ./gradlew) over a bare tool when the tree has one.
- When this repository is a deployable app or a frontend, declare a "preview"
  object with "start" (an ARGV array — how the process boots and serves, e.g.
  ["sh","-c","java -jar bootstrap/target/*.jar"] or ["npx","vite","preview",
  "--host","0.0.0.0"]) and "image" when the default toolchain would be wrong.
  Omit the whole "preview" object for a library with nothing to serve — do NOT
  invent a start command.
- "services" (TOP LEVEL): the backing services the test suite or the app needs
  running — an object of name -> {"image","port","env","ready","user",
  "writable"}. Declare it ONLY when the tree shows the dependency: a
  docker-compose with a database, testcontainers in the tests, a supabase/
  directory, a CI job that boots postgres/redis, a DATABASE_URL in .env files.
  Do NOT declare one for a unit suite that mocks its storage. The platform
  runs each service as a sidecar reachable on localhost:<port>; there is no
  docker daemon, so compose/testcontainers themselves never run here. Fields:
  "image" a public reference with tag ("postgres:16-alpine"); "port" the
  service's own, 1024-65535; "env" plain strings — write the literal
  $DSE_SERVICE_PASSWORD wherever a password belongs (the platform generates
  one per run and injects it, and the app's own env may embed it too:
  "postgresql://postgres:$DSE_SERVICE_PASSWORD@localhost:5432/app"); "ready"
  the image's OWN health argv (["pg_isready","-U","postgres"],
  ["redis-cli","ping"]) — never a tool the image does not ship; "user" the
  image's non-root uid when it needs one (70 = postgres-alpine, 999 =
  redis-alpine); "writable" the paths the image writes to (postgres:
  ["/var/lib/postgresql/data","/var/run/postgresql"]) — and for postgres set
  env PGDATA to a SUBDIRECTORY of the data mount
  ("/var/lib/postgresql/data/pgdata"), or initdb refuses the mount point.
- "prepare" (TOP LEVEL): the ARGV that creates schema and base data against
  the running services BEFORE anything is tested or served — the repository's
  own migrate+seed recipe, e.g. ["sh","-c","npx prisma migrate deploy && npx
  prisma db seed"]. It must be idempotent (safe on an empty database, safe
  twice) and self-sufficient (no docker, nothing beyond the declared services
  on localhost). Omit it when there is no schema to create. Only meaningful
  next to "services".
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
        return {"present": False, "reachable": False, "missing": []}
    if texto is None:
        return {"present": False, "reachable": True, "missing": []}
    # Tema 1: o probe é quem carrega a declaração de serviços até o provision —
    # validada pelo parser REAL, na forma normalizada (`as_payload`). Manifesto
    # inválido é fail-open: o probe devolve vazio e a má notícia, com a mensagem
    # certa, continua sendo do L1.
    servicos: dict = {}
    prepare: list[str] = []
    try:
        import json as _json

        from dse_validation.config import parse_repo_prepare, parse_repo_services

        payload = _json.loads(texto)
        servicos = {name: decl.as_payload()
                    for name, decl in parse_repo_services(payload, source="probe").items()}
        prepare = parse_repo_prepare(payload, source="probe")
    except Exception:  # noqa: BLE001 — inválido aqui = vazio; o L1 dá a notícia
        servicos, prepare = {}, []
    return {"present": True, "reachable": True,
            "missing": missing_declarations(texto),
            "services": servicos, "prepare": prepare}


#: O que a plataforma precisa que o repositório declare, e o que ela é obrigada
#: a adivinhar sem isso. SÓ entra aqui o que degrada de verdade — um campo
#: "seria bom ter" viraria uma PR de emenda por repositório por release, que é
#: ruído, não onboarding.
_REQUIRED = (
    (
        "preview.start",
        "how the application boots — without it the platform falls back to "
        "`java -jar <artifact>` (JVM) or an npm dev-server ladder, which is "
        "wrong for every other ecosystem",
    ),
)


def missing_declarations(manifest_text: str) -> list[str]:
    """As declarações que faltam NESTE manifesto, na ordem de `_REQUIRED`.

    Manifesto ilegível devolve lista vazia — quem reprova manifesto inválido é
    o L1, com mensagem própria; não é papel da emenda dar essa notícia.

    Repo SEM bloco `preview` não é cobrado: quem não tem preview não precisa
    declarar como sobe."""
    try:
        payload = json.loads(manifest_text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    preview = payload.get("preview")
    faltando: list[str] = []
    if isinstance(preview, dict) and preview and not preview.get("start"):
        faltando.append("preview.start")
    return faltando


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


_AMEND_PROMPT = """This repository already has a `.dse/validation.json`, but it
does not declare everything the DSE needs. Amend it.

Missing, and why each one matters:
{faltando}

The current file, VERBATIM — keep every key it already has, change nothing you
were not asked to change:

{atual}

{spec}

These facts were derived from the repository's file tree. They are true.

{facts}

{ci_block}"""

_AMEND_PR_BODY = """The DSE needs one declaration this repository's
`.dse/validation.json` does not have yet:

{faltando}

Without it the platform has to GUESS, and its guess is the shape of another
ecosystem — `java -jar <artifact>` for a service, an npm dev-server ladder for a
frontend. That is why this file, and not a platform release, is the right place
to answer.

This is a **draft, not a decision**. Everything in it was mirrored from this
repository's own CI and build files where they exist, the rest of the file was
left untouched, and the result was accepted by the same parser that enforces it.

Review it, adjust it, merge it. Nothing breaks while it is open — the preview
keeps working the way it works today.
"""


def amend_manifest(
    client, repo: str, base_branch: str, *, missing: list[str], complete,
) -> dict:
    """Open a PR that ADDS the missing declarations to an existing manifest.

    The difference from `bootstrap_manifest` is severity, and it is the whole
    reason this is a separate flow: a MISSING manifest means no gate can run at
    all, so the task ends and the human is asked to merge before resending. An
    INCOMPLETE manifest only degrades the preview — the task goes on, and this
    PR waits for a human whenever they get to it.

    Two gates, not one. The draft must (a) be accepted by the REAL L1 parser,
    like the bootstrap, and (b) actually CONTAIN what was missing — a model can
    return perfectly valid JSON that does not solve the problem, and without
    this second check the PR would open without fixing anything and the next
    task would open another one just like it."""
    existing = client.get_open_pr_for_branch(repo, BRANCH_AMEND)
    if existing:
        return {"ok": True, "pr_number": existing["number"],
                "url": existing.get("html_url"), "existing": True, "reason": ""}

    atual = client.get_file_text(repo, MANIFEST_PATH, base_branch)
    if not atual:
        return {"ok": False, "pr_number": None, "url": None, "existing": False,
                "reason": "the manifest disappeared between the probe and the amendment"}

    tree = client.get_tree_paths(repo, base_branch, limit=200_000)
    facts = derive_facts(tree)
    faltando_txt = "\n".join(
        f"- `{nome}`: {porque}" for nome, porque in _REQUIRED if nome in missing
    ) or "\n".join(f"- `{n}`" for n in missing)
    prompt = _AMEND_PROMPT.format(
        faltando=faltando_txt,
        atual=atual[:8_000],
        spec=_SPEC,
        facts=facts.as_bullets(),
        ci_block=_ci_snippets(client, repo, base_branch, tree),
    )
    draft = _strip_fences(complete(prompt))
    payload, reason = _parser_verdict(draft)
    if payload is None:
        logger.warning("manifest amendment refused for %s: %s", repo, reason)
        return {"ok": False, "pr_number": None, "url": None,
                "existing": False, "reason": reason}

    ainda_faltando = missing_declarations(json.dumps(payload))
    if any(nome in ainda_faltando for nome in missing):
        motivo = (
            f"the draft parses but still does not declare {sorted(set(missing) & set(ainda_faltando))}"
        )
        logger.warning("manifest amendment refused for %s: %s", repo, motivo)
        return {"ok": False, "pr_number": None, "url": None,
                "existing": False, "reason": motivo}

    base_sha = client.get_ref_sha(repo, base_branch)
    if not base_sha:
        return {"ok": False, "pr_number": None, "url": None, "existing": False,
                "reason": f"base branch {base_branch!r} has no resolvable SHA"}
    client.create_branch(repo, BRANCH_AMEND, base_sha)
    client.put_file(
        repo, MANIFEST_PATH,
        content=json.dumps(payload, indent=2) + "\n",
        message="Declare how this app boots in the DSE manifest (proposed by the DSE)",
        branch=BRANCH_AMEND,
    )
    pr = client.create_pr(
        repo, BRANCH_AMEND, base_branch,
        "Amend .dse/validation.json — declare how this app boots",
        _AMEND_PR_BODY.format(faltando=faltando_txt),
    )
    return {"ok": True, "pr_number": pr["number"], "url": pr.get("html_url"),
            "existing": False, "reason": ""}
