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

import json
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

_PROMPT = """A pull request just got an ephemeral preview environment. Decide
which SINGLE relative path a human should open to SEE this change working —
e.g. the new API route the task created, or the page route of a frontend
change.

Task instruction:
{instruction}

Preview kind: {kind}
Files changed: {files}

{patches}

Answer with STRICT JSON only, no fences, exactly:
{{"path": "/the/single/relative/path", "note": "<= 12 words on what to look at"}}

Rules:
- "path" MUST be a relative path starting with "/". Never a full URL, never a
  host. Prefer a GET-able route introduced or changed by this diff.
- Use null for "path" when the environment ROOT is already the right landing
  (e.g. a home-page change).
- The note is in English and says what the reader should check."""


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
) -> dict:
    """{path, note, cost_usd} — path None means "use the root, like today".

    `complete` is the model call (prompt -> text), injected so the gate logic
    tests without a gateway; the activity wrapper supplies the real one (the
    triage pattern: Stage.reviewer, temperature 0, cost ledgered by the
    gateway client)."""
    prompt = _PROMPT.format(
        instruction=(instruction or "")[:3_000],
        kind=kind or "unknown",
        files=", ".join(files_changed[:40]) or "(unknown)",
        patches=pr_patch_block(client, repo, pr_number),
    )
    cost = 0.0
    try:
        resposta = complete(prompt)
        if isinstance(resposta, tuple):
            resposta, cost = resposta
    except Exception as exc:  # noqa: BLE001 — fail-open, o preview nunca espera por isto
        logger.warning("deep link resolution failed (%s: %s)", type(exc).__name__, str(exc)[:200])
        return {"path": None, "note": "", "cost_usd": cost}

    texto = (resposta or "").strip()
    if texto.startswith("```"):
        texto = texto.strip("`\n")
        texto = texto[4:] if texto.startswith("json") else texto
    try:
        parsed, _ = json.JSONDecoder().raw_decode(texto.strip())
    except json.JSONDecodeError:
        logger.warning("deep link output did not parse: %.200s", texto)
        return {"path": None, "note": "", "cost_usd": cost}

    path = _validate_path(parsed.get("path"))
    note = str(parsed.get("note") or "").strip()[:_NOTE_MAX_CHARS] if path else ""
    return {"path": path, "note": note, "cost_usd": cost}


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
            timeout=60.0, max_tokens=300, temperature=0,
        )
        return (result.content or ""), float(getattr(result, "cost_usd", 0.0) or 0.0)

    return _complete
