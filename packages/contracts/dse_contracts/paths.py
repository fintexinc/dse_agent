"""Deterministic path classification (P1) — shared.

`is_test_path` used to belong to sandbox-runtime (TesterToolset); it was
promoted to the contract because the L1 plan_compliance also needs it: the
Tester writes tests BY DESIGN under test paths, and the plan (Planner) never
lists them — test files cannot count as "out of plan" (found on a real run
2026-07-22: L1 would fail every task that added a new test).
"""
from __future__ import annotations

import re
from typing import Iterable

# Covers the common layouts: pytest (`tests/`, `test_*.py`, `*_test.py`,
# `conftest.py`), jest/vitest/node:test (`*.test.ts`, `*.spec.ts`,
# `__tests__/`, `test/`), go (`*_test.go`).
_TEST_PATH_RES = [
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
    re.compile(r"(^|/)conftest\.py$"),
    re.compile(r"\.(test|spec)\.[jt]sx?$"),
    re.compile(r"_test\.go$"),
]


def is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    return any(rx.search(p) for rx in _TEST_PATH_RES)


# --- DISPOSABLE runtime/CLI artifacts (post-Coder prune) --------------------
# Ever since `expected_files` became ADVISORY in L1 (the Planner guesses the
# files from the TEXT of the issue, BEFORE reading the code — operator
# decision, see the l1-expected-files-advisory memory), the post-Coder prune
# can NO LONGER delete "everything outside the plan": a NEW and legitimate
# source file the fix had to create would fall outside `expected_files` and
# vanish silently before the commit. `is_disposable_artifact` restricts the
# prune to obvious GARBAGE (log/scratch/backup and the spontaneous report the
# CLI writes about its own work, e.g. BUG_FIX_REPORT.md).
#
# INVARIANT (covered in packages/contracts/tests/test_paths.py): NEVER
# classifies a source file as disposable. The asymmetry is deliberate — when in
# doubt, KEEP: garbage that slips through stays in the diff (caught by the L1
# line budget / human review); a source file deleted by mistake breaks the fix
# without leaving a trace in the diff.

# Extensions that are runtime garbage by definition (never source code).
_DISPOSABLE_EXTS = frozenset({
    ".log", ".tmp", ".temp", ".bak", ".orig", ".rej",
    ".swp", ".swo", ".pyc", ".pyo", ".pid",
})

# Exact basenames of OS/editor/process garbage.
_DISPOSABLE_BASENAMES = frozenset({
    ".ds_store", "thumbs.db", "desktop.ini", "nohup.out",
})

# ONLY doc/text files can be pruned by the report NAME convention (a
# .py/.js/.ts never matches through this rule — that is what shields source).
_REPORT_DOC_EXTS = frozenset({".md", ".markdown", ".txt", ".rst"})

# Words that give away a spontaneous CLI report (BUG_FIX_REPORT.md,
# IMPLEMENTATION_SUMMARY.md, CHANGES_WALKTHROUGH.txt…). Curated on purpose: it
# does NOT include README/CHANGELOG/CONTRIBUTING/LICENSE/requirements —
# legitimate, maintained documents and manifests, which must survive.
_REPORT_NAME_KEYWORDS = ("REPORT", "SUMMARY", "WALKTHROUGH", "FINDINGS", "VERIFICATION")


def is_disposable_artifact(path: str) -> bool:
    """True if `path` is an obvious DISPOSABLE runtime/CLI artifact — log,
    scratch, backup, OS/editor garbage, or a spontaneous report the CLI writes
    about its own work (BUG_FIX_REPORT.md).

    Used by the post-Coder prune to delete ONLY garbage. See the note above on
    the anti-source invariant and the keep-when-in-doubt asymmetry — in
    particular, the report-name heuristic only applies to doc/text extensions,
    so no source file (whatever its name) is ever disposable.
    """
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    if base.lower() in _DISPOSABLE_BASENAMES:
        return True
    stem, dot, ext = base.rpartition(".")
    if not dot:  # no extension (Makefile, LICENSE, Dockerfile…) — never garbage
        return False
    ext = "." + ext.lower()
    if ext in _DISPOSABLE_EXTS:
        return True
    if ext in _REPORT_DOC_EXTS:
        up = stem.upper()
        return any(kw in up for kw in _REPORT_NAME_KEYWORDS)
    return False


# Lockfile -> manifest that declares it. Running the package manager (npm test,
# poetry run…) rewrites lockfile metadata WITHOUT any dependency change (found
# on a real run 2026-07-22: npm changed 16 lines of package-lock.json and
# diff_budget failed the task as an "out of plan change"). Lockfile churn is
# only a REAL change when the paired manifest changed too.
LOCKFILE_MANIFESTS: dict[str, str] = {
    "package-lock.json": "package.json",
    "npm-shrinkwrap.json": "package.json",
    "yarn.lock": "package.json",
    "pnpm-lock.yaml": "package.json",
    "bun.lockb": "package.json",
    "poetry.lock": "pyproject.toml",
    "uv.lock": "pyproject.toml",
    "Pipfile.lock": "Pipfile",
    "Cargo.lock": "Cargo.toml",
    "Gemfile.lock": "Gemfile",
    "composer.lock": "composer.json",
    "go.sum": "go.mod",
}


def lockfile_manifest_for(path: str) -> str | None:
    """If `path` is a known lockfile, returns the path of the paired manifest
    (same directory); otherwise None."""
    p = path.replace("\\", "/")
    manifest = LOCKFILE_MANIFESTS.get(p.rsplit("/", 1)[-1])
    if manifest is None:
        return None
    head, _, _ = p.rpartition("/")
    return f"{head}/{manifest}" if head else manifest


def is_lockfile_churn(path: str, files_changed: list[str] | set[str]) -> bool:
    """True when `path` is a lockfile and the paired manifest is NOT in the
    diff — mechanical package-manager churn, not a declarable change."""
    manifest = lockfile_manifest_for(path)
    if manifest is None:
        return False
    changed = {f.replace("\\", "/") for f in files_changed}
    return manifest not in changed

# ---------------------------------------------------------------------------
# Documentation-only changes
# ---------------------------------------------------------------------------
#: A change touching nothing but these cannot break a linter, a type checker, a
#: test suite or a build. That is not a judgement call, and it is the one skip
#: that is safe without enumerating every config file that governs every tool.
DOC_EXTENSIONS = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})


def is_documentation_only(changed_files) -> bool:
    """True when every changed file is plainly documentation.

    Conservative in one direction on purpose: an empty change, an unknown
    scope, or a file with no extension (Dockerfile, Makefile, an entrypoint
    script — able to affect anything) all return False, i.e. DO THE WORK.
    Skipping something that could have found a defect is the error that
    matters; doing work that could not is merely slow.

    This lives in the contracts package because two very different consumers
    need the SAME answer: the L1 gates decide whether to run at all, and the
    workflow decides whether the Tester has anything to test. Two copies of the
    extension list would drift, and a drifted list is a false green."""
    if not changed_files:
        return False
    for f in changed_files:
        name = str(f).replace("\\", "/").rsplit("/", 1)[-1]
        if "." not in name:
            return False
        if ("." + name.rsplit(".", 1)[1].lower()) not in DOC_EXTENSIONS:
            return False
    return True


# --- PROTECTED PATHS (`PlanArtifact.forbidden_paths`) -----------------------
# Uma implementação só, e ela mora aqui pela mesma razão que `is_test_path`:
# dois serviços perguntam a mesma coisa e precisam da mesma resposta.
#
# Existiam DOIS matchers, e eles DIVERGIAM: o gate L1 casava segmento em
# qualquer profundidade; o classificador de risco (sandbox-runtime) ancorava na
# raiz com `startswith`/`fnmatch`. Em monorepo,
# `packages/web/.github/workflows/ci.yml` era "low" para um e violação para o
# outro — ou seja, o plano NÃO ia ao gate humano (a política parqueia só
# "high") e o caminho protegido só aparecia quando o L1 reprovava um diff que
# ninguém tinha autorizado. Vale o mais estrito: o do gate.


def _normalise(path: str) -> str:
    """Forma canônica de um caminho do repositório.

    `strip("/")` e não `lstrip("./")`: este último remove QUALQUER ponto ou
    barra do começo e transformaria `.github/...` em `github/...`, que é
    exatamente o caminho que mais importa aqui.
    """
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def first_forbidden_match(path: str, forbidden_paths: Iterable[str]) -> str | None:
    """O primeiro padrão de `forbidden_paths` que cobre `path`, ou None.

    Duas propriedades, ambas correção de bug e não recurso:

    - casa em QUALQUER PROFUNDIDADE. A regra antiga era um `startswith` cru,
      preso à raiz do repositório, então `packages/web/.github/workflows/ci.yml`
      não casava com `.github/workflows/`: num monorepo os defaults embarcados
      não cobriam nada.
    - casa SEGMENTO INTEIRO. O mesmo `startswith` deixava `migrations/` cobrir
      `migrations_backup/0001.sql`; alargar o alcance sem consertar isso teria
      trocado um falso negativo por um falso positivo.

    As duas caem do mesmo truque: envolver caminho e padrão em "/" antes de
    comparar. Um padrão que COMEÇA com "/" fica preso à raiz — convenção do
    próprio .gitignore, e a extensão inteira da sintaxe: sem globs, sem
    curingas. Devolve o PADRÃO que casou (não um booleano) porque quem chama
    precisa nomear a regra na mensagem que o humano lê.
    """
    haystack = "/" + _normalise(path) + "/"
    for forbidden in forbidden_paths:
        pattern = forbidden.replace("\\", "/")
        needle = "/" + pattern.strip("/") + "/"
        if needle == "//":  # entrada vazia: erro de digitação, nunca "tudo"
            continue
        hit = haystack.startswith(needle) if pattern.startswith("/") else needle in haystack
        if hit:
            return forbidden
    return None


def path_is_authorised(path: str, expected_files: Iterable[str]) -> bool:
    """`path` está entre os arquivos que o plano APROVADO declara?

    Só é consultado para caminho protegido, e o plano em questão é o que o
    humano leu no gate (o gate grava o `plan_hash`) — por isso a resposta é
    literal:

    - entrada SEM barra no fim autoriza aquele arquivo, e não os irmãos dele;
    - entrada COM barra no fim é um diretório, e autoriza o que está dentro.
      Sem essa segunda forma, um plano que declara `.github/workflows/`
      recriaria a armadilha inteira: o humano aprova a pasta e o gate reprova o
      arquivo.
    """
    alvo = _normalise(path)
    for entry in expected_files:
        declarado = _normalise(entry)
        if not declarado:
            continue
        if alvo == declarado:
            return True
        if entry.replace("\\", "/").rstrip().endswith("/") and alvo.startswith(declarado + "/"):
            return True
    return False


def protected_expected_files(
    expected_files: Iterable[str], forbidden_paths: Iterable[str]
) -> list[tuple[str, str]]:
    """Os pares (arquivo declarado, padrão que o protege) — a CONTRADIÇÃO.

    É a interseção que o plano carrega consigo: o Planner declarou que precisa
    escrever ali, e a plataforma declarou que ali não se escreve. Ela existe
    desde sempre e já era calculada em dois lugares, sempre para virar um
    rótulo ("high") e nunca uma pergunta. Quem decide é o humano no gate.
    """
    forbidden = list(forbidden_paths)
    pares: list[tuple[str, str]] = []
    for arquivo in expected_files:
        hit = first_forbidden_match(arquivo, forbidden)
        if hit:
            pares.append((arquivo, hit))
    return pares
