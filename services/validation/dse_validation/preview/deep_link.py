"""The preview link lands ON the change — an LLM picks the path, we validate it.

Measured on wi_aa299a51 (PR #153, 2026-08-19): the preview came up perfectly
and the posted link was the ROOT — which on that repo (a pure API with no route
at `/`) answers `500 SYS-002`. The right URL was the very route the task had
just created, present verbatim in the diff.

The split of responsibilities is the house's P1: the MODEL answers a fuzzy
question ("which single path shows this change?") that no per-framework
heuristic ladder could answer without becoming another hardcode to de-POC; the
PLATFORM owns everything enforceable — the path is RELATIVE, validated, and
appended to our own base URL at presentation time only. Any refusal, junk or
error resolves to "no path", which is today's behavior: the preview is never
blocked, never delayed, never wrong-by-invention.

`deep_path` is a separate field end to end (never folded into the URL): the
stored preview URL feeds Playwright's `baseURL` in demo evidence, and a path
there would silently re-root every navigation.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

#: Caps for the grounding block built from the PR's patches. Grounding, not a
#: mirror: the resolver needs to SEE route strings, not the whole diff.
PATCH_MAX_CHARS_PER_FILE = 3_000
PATCH_BLOCK_MAX_CHARS = 12_000
PATCH_MAX_FILES = 30

_PATH_MAX_CHARS = 200
_NOTE_MAX_CHARS = 120

#: O guia "How to test" (mesmo turno, mesmo portão): passos curtos e um login
#: de SEED — nunca inventado, só o que aparece literalmente no grounding.
_GUIDE_STEPS_MAX = 8
_GUIDE_STEP_MAX_CHARS = 140
_GUIDE_LOGIN_MAX_CHARS = 200

#: Seeds do repo como grounding: UM regex fechado sobre nomes (não uma escada
#: por framework) e caps duros — grounding, não espelho.
SEED_MAX_FILES = 2
SEED_MAX_CHARS_PER_FILE = 4_096
_SEED_NAME_RE = re.compile(r"[^/]*(seed|fixture)[^/]*\.(sql|ts|js|rb|py|json)$", re.I)

_PROMPT = """A pull request just got an ephemeral preview environment. A human
will click the link and try the change by hand. Decide:
1. which SINGLE relative path they should open to SEE this change working;
2. the click-path to exercise it (short numbered steps);
3. which SEEDED credentials to log in with, if the app needs login.

Task instruction:
{instruction}

Preview kind: {kind}
Files changed: {files}

How the change was meant to be tested (from the task's plan):
{test_plan}

Repository facts (manifest):
{manifest_facts}

{seeds}

{patches}

Answer with STRICT JSON only, no fences, exactly:
{{"path": "/the/single/relative/path", "note": "<= 12 words on what to look at",
  "steps": ["step 1", "step 2"], "login": "user/password hint or empty"}}

Rules:
- "path" MUST be a relative path starting with "/". Never a full URL, never a
  host. Prefer a GET-able route introduced or changed by this diff.
- Use null for "path" when the environment ROOT is already the right landing
  (e.g. a home-page change).
- The note is in English and says what the reader should check.
- "steps": at most {steps_max} steps, each <= {step_max} chars, written in the
  SAME LANGUAGE as the task instruction. Concrete clicks ("Open /plans",
  "Click New Simulation"), not test theory. Empty list when the note already
  says it all.
- "login": ONLY a credential that appears LITERALLY in the seed files or diff
  above, naming where it came from. If none appears, either "" or the honest
  route (e.g. "sign up for a new account"). NEVER invent an email or password."""


def pr_patch_block(client, repo: str, pr_number: int) -> str:
    """The PR's patches, capped, as the resolver's grounding. Best-effort: an
    API failure returns an empty block and the model works from the instruction
    and file list alone."""
    try:
        files = client.get_pr_files(repo, pr_number)
    except Exception as exc:  # noqa: BLE001 — grounding é best-effort
        logger.info("deep link: could not fetch PR files for %s#%s (%s)",
                    repo, pr_number, exc)
        return ""
    partes: list[str] = []
    total = 0
    for f in files[:PATCH_MAX_FILES]:
        patch = (f.get("patch") or "")[:PATCH_MAX_CHARS_PER_FILE]
        trecho = f"--- {f.get('filename')} ({f.get('status')}) ---\n{patch}"
        if total + len(trecho) > PATCH_BLOCK_MAX_CHARS:
            partes.append(f"--- {f.get('filename')} ({f.get('status')}) ---\n(patch omitted)")
            break
        partes.append(trecho)
        total += len(trecho)
    if not partes:
        return ""
    return "The PR's diff (truncated), where route strings live:\n\n" + "\n\n".join(partes)


def seed_files_block(client, repo: str, ref: str, *, files_changed: list[str]) -> str:
    """As seeds REAIS do repo, como grounding do login — melhor-esforço.

    Candidatos: arquivos do diff cujo nome case com o regex fechado, depois a
    árvore do branch (o mesmo `get_tree_paths` que o Planner já usa). Qualquer
    falha de API vira bloco vazio: o guia degrada, o preview nunca espera."""
    try:
        tree = client.get_tree_paths(repo, ref)
    except Exception as exc:  # noqa: BLE001 — grounding é best-effort
        logger.info("seed block: no tree for %s@%s (%s)", repo, ref, exc)
        tree = []
    candidatos = [p for p in (files_changed or []) if _SEED_NAME_RE.search(p)]
    candidatos += [p for p in tree if _SEED_NAME_RE.search(p) and p not in candidatos]
    partes: list[str] = []
    for path in candidatos:
        if len(partes) >= SEED_MAX_FILES:
            break
        try:
            texto = client.get_file_text(repo, path, ref)
        except Exception:  # noqa: BLE001 — idem
            continue
        if not texto:
            continue
        partes.append(f"--- {path} ---\n{texto[:SEED_MAX_CHARS_PER_FILE]}")
    if not partes:
        return ""
    return ("The repository's seed/fixture files (REAL test data and logins "
            "live here):\n\n" + "\n\n".join(partes))


def _validate_guide(parsed: dict) -> tuple[list[str], str]:
    """O portão do guia, irmão do `_validate_path`: caps, sem control chars,
    lixo vira campo vazio — nunca erro, nunca invenção que sobrevive."""
    steps: list[str] = []
    raw_steps = parsed.get("steps")
    if isinstance(raw_steps, list):
        for item in raw_steps:
            if not isinstance(item, str):
                continue
            passo = re.sub(r"[\x00-\x1f]", " ", item).strip()
            if not passo:
                continue
            steps.append(passo[:_GUIDE_STEP_MAX_CHARS])
            if len(steps) >= _GUIDE_STEPS_MAX:
                break
    login = ""
    raw_login = parsed.get("login")
    if isinstance(raw_login, str):
        login = re.sub(r"[\x00-\x1f]", " ", raw_login).strip()[:_GUIDE_LOGIN_MAX_CHARS]
    return steps, login


def _validate_path(raw) -> str | None:
    """The deterministic gate: the model never composes a URL. A path is a
    short, printable, root-relative string — anything else is None (today's
    link), never an error."""
    if not isinstance(raw, str):
        return None
    path = raw.strip()
    if not path or not path.startswith("/") or path.startswith("//"):
        return None
    if len(path) > _PATH_MAX_CHARS:
        return None
    if "://" in path or re.search(r"[\s\x00-\x1f]", path):
        return None
    return path


def resolve_deep_link(
    client, *, repo: str, pr_number: int, instruction: str,
    files_changed: list[str], kind: str, complete,
    test_plan: str = "", manifest_facts: str = "", seed_block: str = "",
) -> dict:
    """{path, note, steps, login, cost_usd} — path None means "use the root,
    like today"; steps/login vazios significam "sem guia", o comportamento de
    sempre. UM turno resolve o link E o How to test.

    `complete` is the model call (prompt -> text), injected so the gate logic
    tests without a gateway; the activity wrapper supplies the real one (the
    triage pattern: Stage.reviewer, temperature 0, cost ledgered by the
    gateway client)."""
    prompt = _PROMPT.format(
        instruction=(instruction or "")[:3_000],
        kind=kind or "unknown",
        files=", ".join(files_changed[:40]) or "(unknown)",
        test_plan=(test_plan or "").strip()[:1_500] or "(not stated)",
        manifest_facts=(manifest_facts or "").strip()[:1_500] or "(none)",
        seeds=seed_block or "",
        patches=pr_patch_block(client, repo, pr_number),
        steps_max=_GUIDE_STEPS_MAX,
        step_max=_GUIDE_STEP_MAX_CHARS,
    )
    vazio = {"path": None, "note": "", "steps": [], "login": ""}
    cost = 0.0
    try:
        resposta = complete(prompt)
        if isinstance(resposta, tuple):
            resposta, cost = resposta
    except Exception as exc:  # noqa: BLE001 — fail-open, o preview nunca espera por isto
        logger.warning("deep link resolution failed (%s: %s)", type(exc).__name__, str(exc)[:200])
        return {**vazio, "cost_usd": cost}

    from dse_validation.model_json import parse_model_json

    parsed, motivo = parse_model_json(resposta or "")
    if parsed is None:
        logger.warning("deep link output did not parse (%s): %.200s", motivo, resposta)
        return {**vazio, "cost_usd": cost}

    path = _validate_path(parsed.get("path"))
    note = str(parsed.get("note") or "").strip()[:_NOTE_MAX_CHARS] if path else ""
    steps, login = _validate_guide(parsed)
    return {"path": path, "note": note, "steps": steps, "login": login, "cost_usd": cost}


def manifest_facts_block(client, repo: str, ref: str) -> str:
    """Fatos DECLARADOS que ajudam o guia: o argv do `prepare` (a receita de
    migrate+seed nomeia arquivos de seed com frequência) e os NOMES de env dos
    `services` — nunca valores, que podem carregar o token da senha."""
    try:
        import json as _json

        from dse_validation.config import L1_MANIFEST_PATH

        texto = client.get_file_text(repo, L1_MANIFEST_PATH, ref)
        if not texto:
            return ""
        payload = _json.loads(texto)
        fatos: list[str] = []
        prepare = payload.get("prepare")
        if isinstance(prepare, list) and prepare:
            fatos.append("prepare (migrate+seed): " + " ".join(str(c) for c in prepare)[:300])
        services = payload.get("services")
        if isinstance(services, dict) and services:
            nomes = ", ".join(
                f"{nome} (env keys: {', '.join(sorted((decl or {}).get('env') or {}))})"
                for nome, decl in sorted(services.items()) if isinstance(decl, dict)
            )
            fatos.append("declared services: " + nomes[:400])
        return "\n".join(fatos)
    except Exception as exc:  # noqa: BLE001 — grounding é best-effort
        logger.info("manifest facts: unreadable for %s@%s (%s)", repo, ref, exc)
        return ""


def build_completer(tenant_id: str, work_item_id: str):
    """The real model call, the triage's exact shape (Stage.reviewer — a new
    Stage would touch gateway enforcement; see triage.py's docstring). Returns
    (text, cost_usd)."""
    from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
    from model_gateway_client.gateway_call import chat_completion
    from model_gateway_client.virtual_keys import mint_virtual_key

    headers = GatewayCallHeaders(
        tenant_id=tenant_id, work_item_id=work_item_id,
        stage=Stage.reviewer, task_class="default", data_class="internal",
    )
    model = (
        os.environ.get("DSE_PREVIEW_DEEP_LINK_MODEL")
        or os.environ.get("DSE_L2_MODEL")
        or os.environ.get("DSE_CODER_MODEL", "anthropic/claude")
    )

    def _complete(prompt: str):
        vk = mint_virtual_key(tenant_id, work_item_id, Stage.reviewer)
        result = chat_completion(
            headers=headers, virtual_key=vk, model=model,
            messages=[{"role": "user", "content": prompt}],
            # 700, não 300: a mesma chamada agora devolve também o guia
            # (steps ≤8×140 + login ≤200) além do path/note.
            timeout=60.0, max_tokens=700, temperature=0,
        )
        return (result.content or ""), float(getattr(result, "cost_usd", 0.0) or 0.0)

    return _complete
