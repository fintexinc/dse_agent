"""A receita do preview para de assumir a forma de UM repo (Fase A3).

Três bypasses do manifesto, todos medidos, todos no mesmo arquivo:

  1. `BUILD_CMD=$(jq -r '.commands.build[2]')` — índice fixo. O manifesto REAL
     do calc-engine usa `["sh","-c",…]` e funciona por sorte; um build declarado
     como `["./mvnw","-B","package"]` viraria `sh -c null` SILENCIOSO no deploy
     (o parse do L1 aceita as duas formas — a receita só aceitava uma).
  2. O branch `ui` ignora `preview.image` e `preview.env` — os campos existem
     no manifesto e só valem no branch deployable (assimetria sem razão).
  3. O fallback de env injeta `BMO_DB_*`/Spring de UM cliente em qualquer repo
     que não declare `preview.env`. Decisão de operador (2026-08-19): o
     fallback vira só SERVER_PORT, e os repos BMO passam a DECLARAR o bloco no
     próprio manifesto — migração pré-condição do deploy da rc.101 (eles têm
     que continuar funcionando).

(A triage de preview que também lia os manifestos de build morreu na rc.130
com o laço de autofix — 0/8 despachos viraram preview `created`.)
"""
from __future__ import annotations

from dse_validation.config import PreviewConfig, parse_repo_preview
from dse_validation.preview import argocd

_LABELS = {"dse.fintex/work-item": "wi_teste"}


def _cfg() -> PreviewConfig:
    cfg = PreviewConfig()
    cfg.mode = "source"
    return cfg


def _declara(**campos):
    return parse_repo_preview({"version": 1, "commands": {}, "preview": campos})


def _deployment(**kw) -> str:
    base = dict(repo="acme/svc", branch="dse/wi", kind="deployable")
    base.update(kw)
    return argocd._source_deployment("preview-wi", _LABELS, _cfg(), **base)


# ---------------------------------------------------------------------------
# 1. O comando de build vem PARSEADO, para qualquer forma de argv
# ---------------------------------------------------------------------------

def test_a_parsed_build_cmd_is_embedded_whatever_its_argv_shape():
    y = _deployment(build_cmd=["./mvnw", "-B", "-DskipTests", "package"])
    assert "./mvnw -B -DskipTests package" in y, (
        "o argv parseado não chegou ao script — a receita ainda depende do "
        "índice [2] do jq"
    )
    assert "jq -r" not in y, "com o comando parseado, o jq de índice fixo sai"


def test_a_sh_dash_c_build_cmd_still_works():
    """A forma do manifesto real do calc-engine continua válida."""
    y = _deployment(build_cmd=["sh", "-c", "J=$X; ./mvnw -B package"])
    assert "./mvnw -B package" in y


def test_without_a_parsed_cmd_the_fallback_fails_loud_never_sh_c_null():
    """Leitura por API é best-effort; quando ela falha, o pod ainda tem o
    clone — o jq continua como fallback, mas `null` vira erro NOMEADO."""
    y = _deployment(build_cmd=None)
    assert "jq -r" in y, "o fallback in-pod sumiu — API instável derrubaria o preview"
    # o script viaja pelo json.dumps do YAML, então as aspas chegam escapadas;
    # o pin é semântico: a variável existe e é executada via sh -c.
    assert "$BUILD_CMD" in y and "sh -c" in y
    assert "no build command" in y, (
        "manifesto sem build tem que falhar com mensagem nossa, não `sh -c null`"
    )


# ---------------------------------------------------------------------------
# 2. O branch ui honra o manifesto como o deployable já honra
# ---------------------------------------------------------------------------

def test_the_ui_branch_honours_the_declared_image():
    y = _deployment(kind="ui", repo_preview=_declara(image="node:20-alpine"))
    assert "node:20-alpine" in y
    assert "node:22-alpine" not in y


def test_the_ui_branch_honours_the_declared_env():
    y = _deployment(kind="ui", repo_preview=_declara(env={"API_BASE_URL": "http://api"}))
    assert "API_BASE_URL" in y
    assert "NODE_ENV" in y, "o env base do ui continua — a declaração ADICIONA"


def test_the_ui_branch_without_declaration_is_unchanged():
    y = _deployment(kind="ui", repo_preview=None)
    assert "node:22-alpine" in y or "PORT" in y  # receita de sempre


# ---------------------------------------------------------------------------
# 3. O fallback de env deixa de carregar o cliente dentro da plataforma
# ---------------------------------------------------------------------------

def test_the_fallback_env_is_server_port_only():
    """Decisão de operador: os nomes `BMO_DB_*`/Spring saem da plataforma. Os
    repos que dependem deles passam a DECLARÁ-LOS no próprio manifesto — a
    migração dos dois bmo-fee-calculator é pré-condição do deploy (Ship,
    rc.101), então este teste e aquele merge andam juntos."""
    y = _deployment(repo_preview=None)
    assert "SERVER_PORT" in y
    assert "BMO_DB_URL" not in y, "nome de variável de cliente na plataforma"
    assert "SPRING_DATASOURCE_URL" not in y
    assert "SPRING_FLYWAY_ENABLED" not in y


def test_a_declared_env_still_reaches_the_pod():
    y = _deployment(repo_preview=_declara(env={"BMO_DB_URL": "jdbc:postgresql://postgres:5432/fee"}))
    assert "BMO_DB_URL" in y, (
        "declarado no manifesto do REPO os nomes valem — é assim que o BMO "
        "continua funcionando"
    )
