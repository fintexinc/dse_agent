"""WSE-E1-T2 (part 2) — self-contained secret scanner (regex + Shannon
entropy), with no dependency on an external service. It runs INSIDE the sandbox
through the same `SandboxExecutor` as the other checks: the scanner is a pure
Python script (stdlib only) embedded as a string and executed with `python3 -c`
— it works both in `DockerExecSandbox` (the container only needs `python3`,
already guaranteed by the OpenHands runtime) and in the test `LocalFakeSandbox`.

Covers: AWS access key id, GitHub/Slack tokens, PEM private key header, and the
generic case "variable with a secret-ish name == high-entropy literal".
`detect-secrets` (if installed in the sandbox) could replace this later without
changing the signature of `secret_scan_check` — see README.
"""
from __future__ import annotations

import json

from dse_contracts import GateStatus, L1Finding

from dse_validation.config import default_scan_timeout_seconds
from dse_validation.sandbox_exec import SandboxExecutor

# stdlib-only script executed inside the sandbox. It emits one JSON line on
# stdout with the list of findings — parsed from the outside.
_SCANNER_SCRIPT = r'''
import json, math, os, re, sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}
PLACEHOLDER_VALUES = {
    "changeme", "example", "placeholder", "xxx", "todo", "your_key_here",
    "insert_key_here", "dummy", "test", "fake", "secret", "password",
}

PATTERNS = [
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("private_key_header", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----")),
]

ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key)\b"
    r"\s*[:=]\s*[\"']([^\"']{8,})[\"']"
)


def shannon_entropy(s):
    if not s:
        return 0.0
    freq = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in freq.values())


findings = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
    for fname in filenames:
        if fname.endswith((".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".lock")):
            continue
        path = os.path.join(dirpath, fname)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(lines, start=1):
            for kind, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(
                        {"kind": kind, "file": path, "line": lineno, "snippet": line.strip()[:200]}
                    )
            for m in ASSIGNMENT_RE.finditer(line):
                value = m.group(2)
                if value.lower() in PLACEHOLDER_VALUES:
                    continue
                if shannon_entropy(value) >= 3.0 and len(value) >= 12:
                    findings.append(
                        {
                            "kind": "high_entropy_assignment",
                            "file": path,
                            "line": lineno,
                            "snippet": line.strip()[:200],
                        }
                    )

print(json.dumps({"findings": findings}))
'''


def _scoped_to_the_diff(
    findings: list[dict], changed_files: set[str] | None
) -> tuple[list[dict], int]:
    """(achados DESTA mudança, quantos ficaram de fora).

    O gate julga o diff do work item, não a história do repositório — a mesma
    doutrina de `_only_in_changed_files` (lint) e do `inherited` (test), e o
    secret scan era o último a julgar a árvore inteira. Medido no
    glide-path-planner-93: 87 achados no `main` LIMPO (65 em `.test.ts` com
    senha de teste, o resto em `dist/` e docs), nenhum do item — e como este
    gate é da PLATAFORMA (o repo não pode desligá-lo, nem deve), não havia
    conserto possível: o item morria por construção.

    `changed_files=None` = sem diff conhecido → tudo conta: perder um achado
    real é pior que reportar um que não é nosso."""
    if changed_files is None:
        return findings, 0
    nossos = [
        f for f in findings
        if str(f.get("file", "")).lstrip("./") in changed_files
    ]
    return nossos, len(findings) - len(nossos)


def secret_scan_check(
    executor: SandboxExecutor, target_dir: str = ".", timeout: int | None = None,
    changed_files: set[str] | None = None,
) -> L1Finding:
    # Same reason as in `sast.py`: 60 s was frozen in this signature. At the
    # 91k lines/s the scanner measures inside the sandbox pod it covers ~5,5M
    # lines, so a big repository needs to be able to ask for more.
    if timeout is None:
        timeout = default_scan_timeout_seconds("secret_scan")
    result = executor.run(["python3", "-c", _SCANNER_SCRIPT, target_dir], timeout=timeout)
    if result.returncode == 127:
        return L1Finding(
            check="secret_scan",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"python3 not found in the sandbox: {result.stderr.strip()}",
            summary="python3 not found in the sandbox (exit 127)",
        )
    if result.returncode != 0:
        return L1Finding(
            check="secret_scan",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"secret scanner failed (exit={result.returncode}): {result.stderr[:2000]}",
            summary=f"secret scanner failed (exit={result.returncode})",
        )
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {"findings": []}
    except (json.JSONDecodeError, IndexError):
        return L1Finding(
            check="secret_scan",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"unexpected scanner output: {result.stdout[:2000]}",
            # The scanner emits ONE json line holding every snippet, so this
            # branch's FIRST line is the payload. Taking "the summary line"
            # would have published the whole scan.
            summary="the secret scanner produced output that could not be parsed",
        )

    findings, herdados = _scoped_to_the_diff(payload.get("findings", []), changed_files)
    # A dívida pré-existente NUNCA é engolida em silêncio: ela não reprova o
    # item que não a criou, mas o operador tem de saber que ela está lá.
    nota_herdada = (
        f" ({herdados} pre-existing, outside this change — reported, not charged)"
        if herdados else ""
    )
    if not findings:
        summary = "no secret/token detected" + nota_herdada
        return L1Finding(
            check="secret_scan",
            passed=True,
            detail=summary,
            summary=summary,
        )

    lines = [f"- [{f['kind']}] {f['file']}:{f['line']} — {f['snippet']}" for f in findings[:20]]
    summary = f"{len(findings)} possible secret(s) detected" + nota_herdada
    # `snippet` IS the matched source line. It stays in `detail` only.
    detail = summary + ":\n" + "\n".join(lines)
    return L1Finding(
        check="secret_scan", passed=False, detail=detail, summary=summary
    )
