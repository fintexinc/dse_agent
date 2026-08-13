#!/usr/bin/env python3
"""WSF-E2-T3a — plaintext secret scanner over version-controlled files.

Proves the acceptance criterion "no secret in env-var/manifest": walks
version-controlled files (`.env`, YAML/manifests, docker-compose fragments, Helm
values) looking for known hardcoded-secret patterns (Slack/GitHub tokens, AWS
keys, plaintext database passwords, PEM private keys, etc.) and fails
(`exit(1)`) on any match OUTSIDE an explicit exclusion list (`.env.example` /
`*.example` files, marked test fixtures, and the local-dev placeholders that
CONVENTIONS.md documents as acceptable: `dse_dev_only`, `dse_dev_root`,
`dse_app_dev_only` — those three are local-development Postgres/Vault
credentials, not production ones, and are known/documented in the foundation).

Usage:
    python3 scripts/scan_for_plaintext_secrets.py [--root PATH] [--ci]

Output: list of findings (file:line:pattern) on stdout; exit code 1 if any
finding survives the exclusion filters, exit code 0 otherwise.

This script is deterministic (no LLM) — P1: no control-flow decision made by an
LLM. The "this is a leaked secret" call is 100% regex + an explicit allowlist
reviewable in human code review.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Known secret patterns. Each entry: (name, compiled regex).
# The regexes are deliberately specific enough not to fire on variable names
# alone (`SLACK_BOT_TOKEN=` with no value is not a match; the real token format
# must follow the `=`).
# ---------------------------------------------------------------------------
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("slack_bot_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("slack_signing_secret_assignment", re.compile(r"SLACK_(BOT_TOKEN|SIGNING_SECRET|APP_TOKEN)\s*[:=]\s*['\"]?[A-Za-z0-9_-]{16,}")),
    ("github_pat", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github_app_private_key_assignment", re.compile(r"GITHUB_(APP_PRIVATE_KEY|WEBHOOK_SECRET|TOKEN)\s*[:=]\s*['\"]?[A-Za-z0-9/+_=-]{16,}")),
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws_secret_access_key_assignment", re.compile(r"AWS_SECRET_ACCESS_KEY\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{30,}")),
    ("generic_api_key_assignment", re.compile(r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=.]{20,}")),
    ("private_key_pem", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("vault_token_assignment", re.compile(r"VAULT_TOKEN\s*[:=]\s*['\"]?(hvs|hvb|s)\.[A-Za-z0-9]{10,}")),
    ("postgres_password_in_url", re.compile(r"postgresql://[^:\s]+:[^@\s]{6,}@")),
    ("database_password_assignment", re.compile(r"(?i)(db|database|postgres)_password\s*[:=]\s*['\"]?[^\s'\"]{6,}")),
]

# Local-dev placeholder values documented in the foundation (CONVENTIONS.md /
# docker-compose.yml) — not production secrets, just fixed credentials for a
# disposable local environment. They trip several of the patterns above (e.g.
# `postgres_password_in_url` matches every dev DSN) — excluded explicitly so the
# scanner does not become permanent noise.
KNOWN_DEV_PLACEHOLDERS = {
    "dse_dev_only",
    "dse_dev_root",
    "dse_app_dev_only",
}

# Directories/files never scanned: dependencies, VCS, caches, and the example
# files themselves (which exist precisely to show the expected *format* without
# holding a real value).
EXCLUDED_DIR_NAMES = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "*.egg-info",
}
EXCLUDED_FILE_SUFFIXES = (".example", ".sample", ".md")
EXCLUDED_FILE_NAMES = {".env.example", ".env.sample"}

# Extensions/files relevant to the scan (env, manifests, YAML, compose).
SCANNED_GLOBS = ["**/.env", "**/*.env", "**/*.yml", "**/*.yaml"]


def _is_excluded_path(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if path.name in EXCLUDED_FILE_NAMES:
        return True
    if any(str(path.name).endswith(suf) for suf in EXCLUDED_FILE_SUFFIXES):
        return True
    for part in rel.parts:
        if part in EXCLUDED_DIR_NAMES or part.endswith(".egg-info"):
            return True
    return False


def _line_is_only_known_placeholder(line: str) -> bool:
    """True if the only 'credential' on the line is one of the known dev
    placeholders (e.g. the DSN `postgresql://dse:dse_dev_only@...`)."""
    stripped = line.strip()
    return any(placeholder in stripped for placeholder in KNOWN_DEV_PLACEHOLDERS)


# Indicators that the value is a REFERENCE to an env var/secret manager rather
# than a hardcoded secret (e.g. `api_key: os.environ/OPENAI_API_KEY` in LiteLLM
# format, or `${VAULT_TOKEN}` in a manifest) — these are exactly the pattern we
# want to encourage, not flag as a violation.
_ENV_REFERENCE_INDICATORS = (
    "os.environ",
    "os.getenv(",
    "process.env.",
    "${",
    # k8s pod-spec: o kubelet interpola `$(VAR)` a partir de env/secretKeyRef
    # em runtime (ex.: DATABASE_URL do model-gateway) — nada em texto plano.
    "$(",
    "secretKeyRef",
    "valueFrom",
)


def _match_is_env_reference(line: str) -> bool:
    return any(indicator in line for indicator in _ENV_REFERENCE_INDICATORS)


def _git_tracked_files(root: Path) -> set[Path] | None:
    """Returns the set of "version-controllable" files (relative to `root`):
    already committed (`git ls-files`) UNION not yet committed but not ignored
    (`git ls-files --others --exclude-standard`). Or None if `root` is not a git
    repository / git is unavailable.

    We use the union (not just `git ls-files`) because this monorepo is under
    active parallel development without intermediate commits (the integrator
    consolidates everything at the end) — running the scanner against the
    committed HEAD alone would leave it blind to secrets introduced in new files
    not yet added to the index. "Version-controlled" here means "eligible for
    version control" (not covered by .gitignore), which is precisely what
    .gitignore defines."""
    try:
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        untracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if tracked.returncode != 0:
        return None
    lines = tracked.stdout.splitlines() + (untracked.stdout.splitlines() if untracked.returncode == 0 else [])
    return {root / line for line in lines if line.strip()}


def scan_repo(root: Path) -> list[tuple[Path, int, str, str]]:
    """Returns a list of (file, line, pattern_name, snippet) for every match that
    survives the exclusion filters.

    When `root` is a git repository, the scan is restricted to TRACKED files
    (`git ls-files`) — that is the exact definition of "version-controlled" the
    acceptance criterion asks for, and it avoids false positives in locally
    installed dependencies (`.venv-*/`, `node_modules/`, etc.) that would never
    be committed. With no git available, it falls back to a directory walk using
    the heuristic exclusion list below."""
    findings: list[tuple[Path, int, str, str]] = []
    seen_files: set[Path] = set()

    tracked = _git_tracked_files(root)

    def candidate_files() -> list[Path]:
        if tracked is not None:
            return [p for p in tracked if p.suffix in (".yml", ".yaml") or p.name == ".env" or p.name.endswith(".env")]
        found: list[Path] = []
        for pattern_glob in SCANNED_GLOBS:
            found.extend(p for p in root.glob(pattern_glob) if p.is_file())
        return found

    for path in candidate_files():
        if path in seen_files or not path.is_file():
            continue
        if _is_excluded_path(path, root):
            continue
        if tracked is None and _is_excluded_by_dir_heuristic(path, root):
            continue
        seen_files.add(path)

        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            if _line_is_only_known_placeholder(line):
                continue
            if _match_is_env_reference(line):
                continue
            for pattern_name, regex in SECRET_PATTERNS:
                match = regex.search(line)
                if match:
                    findings.append((path.relative_to(root), lineno, pattern_name, match.group(0)[:60]))

    return findings


def _is_excluded_by_dir_heuristic(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    return any(part in EXCLUDED_DIR_NAMES or part.startswith(".venv") for part in rel.parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Root of the repository to scan (default: cwd)")
    parser.add_argument("--ci", action="store_true", help="Compact output format for CI")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    findings = scan_repo(root)

    if not findings:
        print(f"[scan_for_plaintext_secrets] OK — no plaintext secret found under {root}")
        return 0

    print(f"[scan_for_plaintext_secrets] FAILED — {len(findings)} possible plaintext secret(s):")
    for path, lineno, pattern_name, snippet in findings:
        print(f"  {path}:{lineno}  [{pattern_name}]  {snippet}")
    print()
    print("If any of these is a false positive (e.g. a test fixture with a fake value),")
    print("move the value into a variable clearly named as a fixture/mock, or")
    print("add the file to the explicit exclusion list in this script with a rationale.")
    print("If it is a real secret: move it to Vault (services/platform/dse_secrets) and")
    print("replace the value here with an env var reference read at runtime.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
