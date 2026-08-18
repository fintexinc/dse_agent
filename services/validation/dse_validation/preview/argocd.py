"""WSE-E4-T10 — trigger_preview: per-PR preview environment via Argo CD
ApplicationSet against the REAL k3d cluster (`k3d-dse-preview`, Argo CD v2.13.3).

Flow (implements the contract's `trigger_preview` Activity —
`ACTIVITY_TRIGGER_PREVIEW`, input `TriggerPreviewInput`, returns `PreviewRef`):

  1. UI-touching decision through a DETERMINISTIC paths-filter (FR-20, P1) —
     backend-only => `skipped_backend_only` (counts as success, NEVER blocks).
  2. Cap on concurrent previews per tenant (ADR-26, day 1): at the cap =>
     `degraded` with an explicit detail (does not block the PR; P6 clean
     failure).
  3. GitOps: writes `previews/preview-<work_item_id>/` (Namespace + pinned nginx
     Deployment + Service) into the manifests repo (gitops.py) and ensures the
     `dse-previews` ApplicationSet (git generator `previews/*`, low
     requeueAfterSeconds). Argo CD materializes the Application and syncs =>
     ephemeral namespace `preview-<work_item_id>` in the cluster.
  4. Waits for the Deployment to become Available (timeout). Failure/timeout =>
     `degraded` (failure mode 9 — the PR never stays blocked forever).
  5. TTL: label/annotation on the Namespace + `expires_at` in `wse_previews`.

TTL REAPER — documented decision (the addendum prefers kube-janitor, P7):
  kube-janitor would delete the NAMESPACE in the cluster, but with Argo CD on
  `automated.selfHeal` the source of truth is GIT — Argo CD would recreate the
  namespace on the next reconcile (the two controllers would fight). The correct
  reaper under GitOps is to remove the directory from the REPO: the
  ApplicationSet prunes the Application and the `resources-finalizer` finalizer
  cascades the namespace deletion. That is why `reap_expired_previews()` is a
  deterministic Python job (real, tested against the cluster) that operates on
  git — kube-janitor stays documented as the upgrade path for resources NOT
  managed by GitOps.
  The `janitor/ttl` annotation is already written on the Namespace for that
  future.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone

from dse_contracts.activities import PreviewRef, TriggerPreviewInput

from dse_validation import db
from dse_validation.config import PreviewConfig, RepoPreviewDeclaration
from dse_validation.preview import gitops
from dse_validation.preview.paths_filter import preview_decision

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

logger = logging.getLogger("dse_validation.preview.argocd")


# ---------------------------------------------------------------------------
# kubectl (subprocess, P7 — no extra client library; explicit kubecontext)
# ---------------------------------------------------------------------------
def _kubectl(cfg: PreviewConfig, args: list[str], *, input_text: str | None = None,
             timeout: int = 60) -> subprocess.CompletedProcess:
    # An empty context means "use the ambient credentials": in-cluster that is
    # the pod's service account, where no named context exists and passing
    # --context would fail outright.
    context = ["--context", cfg.kube_context] if cfg.kube_context else []
    proc = subprocess.run(
        ["kubectl", *context, *args],
        input=input_text, capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"kubectl {' '.join(args)} failed (exit={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


#: Quanto do log do pod entra no detail. As ÚLTIMAS linhas, porque é onde o erro
#: mora — `npm install` sozinho passa de mil linhas de ruído antes dele.
_POD_LOG_CHARS = 1200


def pod_failure_detail(cfg: PreviewConfig, namespace: str, reason: str) -> str:
    """O motivo de o preview não ter subido, com as palavras do POD.

    `kubectl wait` só sabe dizer que o relógio estourou. Isso descreve QUANDO
    desistimos, não o que quebrou — e nas PRs #2 e #3 o que quebrou estava
    inteiro no log do pod (`must have required property 'buildTarget'`), a um
    `kubectl logs` de distância que nenhum revisor de PR vai dar.

    O motivo original SEMPRE sobrevive: a captura é um bônus, e um log ilegível
    (pod já apagado, RBAC, cluster fora do ar) não pode transformar um erro ruim
    em nenhum erro.
    """
    # DUAS tentativas, e a segunda é a que importa. Em CrashLoopBackOff o
    # container está *waiting*, e `kubectl logs` sem `--previous` falha com
    # "container is waiting to start" — ou seja, a captura morria exatamente no
    # caso em que ela seria mais útil, porque só o pod que ESTÁ reiniciando tem
    # algo a dizer. Medido na PR #5: a linha chegou à PR com o timeout pelado,
    # sem log e sem o sufixo de "log vazio", que é a assinatura da exceção.
    base = ["logs", "-n", namespace, "-l", "app=preview", "--tail=40",
            "--all-containers=true"]
    log = ""
    respondeu = False
    for args in (base, [*base, "--previous"]):
        try:
            proc = _kubectl(cfg, args, timeout=25)
        except Exception as exc:  # noqa: BLE001 — bônus, nunca piora
            logger.warning("preview: could not read pod logs in %s (%s): %s",
                           namespace, args[-1], exc)
            continue
        respondeu = True
        log = (getattr(proc, "stdout", "") or "").strip()
        if log:
            break
    # As duas tentativas falharam: o motivo original é tudo o que temos, e é
    # melhor que nada. Distinto de "respondeu e veio vazio", que é informação.
    if not respondeu:
        return reason
    if not log:
        return f"{reason} (the pod wrote no log at all — it died before starting)"
    return f"{reason} — the pod said: {log[-_POD_LOG_CHARS:]}"


#: O anúncio que a receita imprime ao entregar o serve para o build — contrato
#: entre o script do pod e a nota da PR. Mudou lá, muda aqui.
_BUILD_HANDOVER_MARKER = "serving the production build"


def built_fallback_note(cfg: PreviewConfig, namespace: str) -> str | None:
    """O preview subiu — mas subiu servindo o BUILD? A resposta vem do log.

    A receita anuncia a entrega com `_BUILD_HANDOVER_MARKER` antes de buildar.
    Sem a nota, um `npm start` quebrado se disfarça de dev server são atrás de
    um preview verde: a PR mostraria só a URL, e o defeito que causou o
    CrashLoop da PR #6 sumiria da supervisão exatamente no momento em que o
    fallback o torna invisível.

    Bônus, nunca piora (mesma regra da captura de falha): log ilegível = None =
    a linha fica como sempre foi.
    """
    try:
        proc = _kubectl(cfg, ["logs", "-n", namespace, "-l", "app=preview",
                              "--tail=200", "--all-containers=true"], timeout=25)
    except Exception as exc:  # noqa: BLE001 — bônus, nunca piora o sucesso
        logger.warning("preview: could not check the serve mode in %s: %s",
                       namespace, exc)
        return None
    if _BUILD_HANDOVER_MARKER in ((getattr(proc, "stdout", "") or "")):
        return ("served from the repo's production build (`npm run build`) — "
                "the declared dev server failed to start; its error is in the pod log")
    return None


def namespace_exists(cfg: PreviewConfig, namespace: str) -> bool:
    """O namespace ainda está lá? Levanta se o cluster não responder.

    Deliberadamente SEM `except`: quem chama tem de decidir o que fazer com
    "não sei", e a resposta certa é diferente de "não existe". Engolir o erro
    aqui devolveria `False` para um `kubectl` fora do ar e liberaria vaga de
    preview vivo — trocaria um bloqueio visível por dois previews disputando o
    mesmo recurso, que é bem mais difícil de enxergar.
    """
    proc = _kubectl(cfg, ["get", "namespace", namespace, "-o", "name"], timeout=20)
    return bool((getattr(proc, "stdout", "") or "").strip())


def namespace_for(work_item_id: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in work_item_id.lower())
    return f"preview-{slug}"[:63].rstrip("-")


# ---------------------------------------------------------------------------
# Manifests of the minimal preview (pinned nginx serving the default page)
# ---------------------------------------------------------------------------
def resolve_fe_api_target(
    sibling_namespace: str | None, sibling_alive: bool, cfg: PreviewConfig
) -> str | None:
    """G-3 — o alvo do proxy /api do preview FE, em dois degraus:
    degrau 2 (precedência): o irmão de group_id com preview VIVO — o svc
    in-cluster derivado do namespace (proxy é server-side no ng serve; DNS do
    cluster resolve, sem depender de ingress); degrau 1: o alvo estável
    configurado (`fe_api_fallback`). Sem alvo → None → sem proxy: o 404 real
    aparece em vez de um alvo inventado."""
    if sibling_alive and sibling_namespace:
        return f"http://preview.{sibling_namespace}.svc.cluster.local"
    return cfg.fe_api_fallback or None


#: G-1 — a deploy key read-only do repo, montada de secret no namespace do
#: preview. O nome é fixo por namespace; o CONTEÚDO é a key do repo daquele
#: preview (copiada de `dse-preview-deploy-keys` do namespace do DSE).
DEPLOY_KEY_SECRET = "dse-preview-deploy-key"


def deploy_key_item_for(repo: str) -> str:
    """Nome do item dentro da secret agregada `dse-preview-deploy-keys` no
    namespace do DSE: um por repo, slug determinístico (k8s data keys aceitam
    [-._a-zA-Z0-9])."""
    return repo.replace("/", "__")


def _mint_installation_token() -> str | None:
    """Installation token da GitHub App JÁ instalada, emitido AGORA.

    Deliberadamente SEM cache (decisão de operador 2026-08-09): o token vale
    1h, o clone leva segundos, e um preview que viva mais que isso já clonou —
    cachear só criaria a janela em que um token velho é injetado num pod novo.
    Devolve None quando a App não está configurada (dev/local)."""
    try:
        from dse_validation.config import GitHubConfig
        from dse_validation.github.app_auth import fetch_installation_token
    except ImportError:  # pragma: no cover — dependência opcional em dev
        return None
    gh = GitHubConfig()
    if not gh.is_configured:
        return None
    try:
        return fetch_installation_token(
            gh.app_id, gh.private_key_pem, gh.installation_id, gh.api_base_url
        )
    except Exception as exc:  # noqa: BLE001 — preview nunca bloqueia (failure mode 9)
        logger.warning("preview: could not mint an installation token: %s", exc)
        return None


def resolve_preview_credential(
    cfg: PreviewConfig, repo: str
) -> tuple[str, str] | tuple[None, None]:
    """A credencial de clone deste preview, em ordem de precedência:

      1. `ssh`   — item do repo na secret agregada, SE o operador a semeou
                   (runbook deploy/vps/preview-deploy-keys.md);
      2. `token` — installation token da App já instalada (G-1', o caminho do
                   POC: nenhum escopo novo, nenhuma secret manual).

    Devolve `(mode, material_b64)` — material SEMPRE em base64, pronto para a
    secret — ou `(None, None)` quando nenhum caminho está disponível, e aí o
    chamador degrada com motivo nomeado."""
    item = deploy_key_item_for(repo)
    try:
        proc = _kubectl(cfg, [
            "get", "secret", cfg.deploy_keys_secret, "-n", cfg.dse_namespace,
            "-o", "jsonpath={.data." + item + "}",
        ])
        seeded = (getattr(proc, "stdout", "") or "").strip()
    except RuntimeError:
        seeded = ""
    if seeded:
        return "ssh", seeded
    token = _mint_installation_token()
    if token:
        return "token", base64.b64encode(token.encode()).decode()
    return None, None


def apply_preview_credential(
    cfg: PreviewConfig, namespace: str, mode: str, material_b64: str
) -> None:
    """Materializa a credencial na secret que o pod monta — via kubectl, FORA
    do manifest set: em modo gitops o set vira commit num repo git, e
    credencial não vai para git (garantia por construção)."""
    field = "key" if mode == "ssh" else "token"
    _kubectl(cfg, ["apply", "-f", "-"], input_text=f"""apiVersion: v1
kind: Secret
metadata:
  name: {DEPLOY_KEY_SECRET}
  namespace: {namespace}
type: Opaque
data:
  {field}: {material_b64}
""")


def materialize_deploy_key(cfg: PreviewConfig, namespace: str, repo: str) -> bool:
    """G-1 — copia a deploy key READ-ONLY do repo da secret AGREGADA (namespace
    do DSE, semeada pelo operador) para a secret que o pod do preview monta.

    Via kubectl, deliberadamente FORA do manifest set: em modo gitops o set
    vira commit num repo de manifestos, e chave privada não vai para git — a
    garantia é por construção, não por disciplina de quem edita depois.

    O DSE nunca gera nem registra chave (decisão (a), 2026-08-09): só copia o
    material que o operador semeou. Devolve False quando o repo não tem item
    na agregada — o chamador degrada com motivo NOMEADO."""
    item = deploy_key_item_for(repo)
    try:
        proc = _kubectl(cfg, [
            "get", "secret", cfg.deploy_keys_secret, "-n", cfg.dse_namespace,
            "-o", "jsonpath={.data." + item + "}",
        ])
    except RuntimeError:
        return False
    key_b64 = (getattr(proc, "stdout", "") or "").strip()
    if not key_b64:
        return False
    _kubectl(cfg, ["apply", "-f", "-"], input_text=f"""apiVersion: v1
kind: Secret
metadata:
  name: {DEPLOY_KEY_SECRET}
  namespace: {namespace}
type: Opaque
data:
  key: {key_b64}
""")
    return True


def _source_deployment(namespace: str, labels: str, cfg: PreviewConfig, *,
                       repo: str, branch: str, kind: str = "ui",
                       api_proxy_target: str | None = None,
                       auth_mode: str = "ssh",
                       repo_preview: RepoPreviewDeclaration | None = None) -> str:
    """Deployment that runs the PR branch straight from source.

    No image is built anywhere in this path, and that is the whole point: this
    cluster has no Docker daemon and no registry, so every variant that starts
    with `docker build` is unreachable here by construction. The container
    clones the branch, installs dependencies and starts the app.

    G-1 (2026-08-09, medido 4/4): o clone vai por SSH com a deploy key
    READ-ONLY do próprio repo, montada de secret no namespace — o limite do
    antigo docstring ("a token in a pod running arbitrary branch code") é
    respeitado por construção: a key não alcança nada além do repo que o pod
    já ia clonar.

    G-2: a receita segue o `kind` do paths-filter — `ui` = npm (como sempre);
    `deployable` = o comando de build do PRÓPRIO .dse/validation.json (a régua
    do L1, `commands.build[2]` via jq — nunca uma segunda receita), jar, e
    datasource apontando para o Postgres efêmero do namespace (Flyway do app
    migra no boot).

    G-3: `api_proxy_target` (ui) gera proxy.preview.json NO DEPLOY — nunca
    commitado no repo do cliente — e o ng serve o consome via --proxy-config.

    Readiness is deliberately patient: install/build on a cold container easily
    outlasts a default probe.
    """
    # G7: a porta é do repo quando ele a declara (o Spring deste cluster
    # recebia a porta do dev server de Node por acidente de default).
    port = (repo_preview.port if repo_preview and repo_preview.port else cfg.source_port)
    period = 5
    ready_timeout = (
        repo_preview.ready_timeout_s
        if repo_preview and repo_preview.ready_timeout_s
        else cfg.source_ready_timeout_s
    )
    failure_threshold = max(6, ready_timeout // period)
    if auth_mode == "token":
        # G-1' — installation token da App, lido do VOLUME (nunca do pod spec,
        # que `kubectl get pod -o yaml` mostra, e nunca em argv). O credential
        # helper mantém o token fora do .git/config do clone também.
        clone_url = f"https://github.com/{repo}.git"
        git_env = (
            "printf 'https://x-access-token:%s@github.com\\n' "
            "\"$(cat /preview-keys/token)\" > /tmp/.git-credentials; "
            "git config --global credential.helper "
            "'store --file=/tmp/.git-credentials'; "
        )
    else:
        clone_url = f"git@github.com:{repo}.git"
        git_env = (
            "export GIT_SSH_COMMAND='ssh -i /preview-keys/key "
            "-o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes'; "
        )
    # Single-quoted in the shell and injected from values the DSE itself
    # generated (repo slug, `dse/<work_item_id>` branch) — never from PR text.
    if kind == "deployable":
        # G7: o REPO declara a própria forma; o que ele não declara continua
        # exatamente como antes (o repo que já roda não pode notar diferença).
        decl = repo_preview or RepoPreviewDeclaration()
        image = decl.image or cfg.deployable_image
        # Multi-módulo: o artefato de um build Maven de vários módulos nasce em
        # `<modulo>/target/`, e o glob de módulo único falha com `set -eu`
        # ANTES do java -jar — sintoma "degraded por timeout", causa "ls".
        artifact_glob = decl.artifact_glob or "target/*.jar"
        script = (
            "set -eu; "
            "(apt-get update >/dev/null 2>&1 && "
            "apt-get install -y --no-install-recommends git jq openssh-client "
            ">/dev/null 2>&1) || "
            "apk add --no-cache git jq openssh-client >/dev/null 2>&1 || true; "
            # depois do install: no modo token o git_env chama `git config`
            + git_env +
            f"git clone --depth 1 --branch '{branch}' '{clone_url}' /srv/app; "
            "cd /srv/app; "
            "BUILD_CMD=$(jq -r '.commands.build[2]' .dse/validation.json); "
            "sh -c \"$BUILD_CMD\"; "
            f"JAR=$(ls {artifact_glob} | grep -v plain | head -1); "
            "exec java -jar \"$JAR\""
        )
        # Contrato de datasource MEDIDO no repo (2026-08-09, timebox de uma
        # passada): o app usa `spring.datasource.jdbc-url` (estilo Hikari), e
        # `SPRING_DATASOURCE_URL` NÃO liga nele — relaxed binding do Boot mapeia
        # para `spring.datasource.url`, chave que este app não lê. O repo expõe
        # os próprios placeholders (`${BMO_DB_URL}` etc.), que é o que
        # funciona. `SPRING_FLYWAY_ENABLED=true` porque o repo desabilita o
        # Flyway por padrão e o preview PRECISA da migração da PR aplicada.
        #
        # G7 ENTREGUE: quando o repo declara `preview.env`, é ele que vale — os
        # nomes abaixo pertencem a UM cliente e não têm por que viajar para
        # outro (o calculation-engine-service, por exemplo, não tem banco).
        # Sem declaração, o bloco antigo continua inteiro: o repo que depende
        # dele hoje não pode notar diferença.
        if decl.env:
            linhas = [f'            - name: SERVER_PORT\n              value: "{port}"']
            for nome, valor in decl.env.items():
                # `json.dumps` no valor: aspas/quebras vindas do manifesto do
                # cliente não podem escapar do YAML gerado.
                linhas.append(f"            - name: {nome}\n"
                              f"              value: {json.dumps(valor)}")
            env_yaml = "\n".join(linhas) + "\n"
        else:
            env_yaml = f"""            - name: SERVER_PORT
              value: "{port}"
            - name: SPRING_FLYWAY_ENABLED
              value: "true"
            - name: SPRING_DATASOURCE_URL
              value: "jdbc:postgresql://postgres:5432/{cfg.preview_db_name}"
            - name: SPRING_DATASOURCE_USERNAME
              value: "preview"
            - name: SPRING_DATASOURCE_PASSWORD
              value: "preview"
            - name: BMO_DB_URL
              value: "jdbc:postgresql://postgres:5432/{cfg.preview_db_name}"
            - name: BMO_DB_USER
              value: "preview"
            - name: BMO_DB_PASSWORD
              value: "preview"
"""
        probe_yaml = f"""          readinessProbe:
            tcpSocket: {{ port: {port} }}
            periodSeconds: {period}
            failureThreshold: {failure_threshold}
"""
        resources_yaml = """          resources:
            requests: { cpu: "100m", memory: "512Mi" }
            limits: { cpu: "2", memory: "3Gi" }
"""
    else:
        image = cfg.source_image
        proxy_step = ""
        # As TRÊS flags são obrigatórias, e cada uma foi medida no preview da
        # PR #19 (fintexinc/bmo-fee-calculator-fe-dse), onde o Angular compilou
        # ("Application bundle generation complete [28.2s]") e mesmo assim o pod
        # ficou 0/1 para sempre:
        #
        #   --host 0.0.0.0  o socket do pod era `tcp ::1:4200 LISTEN`. `ng serve`
        #                   escuta em localhost IPv6 por padrão: nem a sonda do
        #                   kubelet nem o Service alcançam. Curl de dentro do
        #                   cluster deu HTTP 000.
        #   --port N        `ng serve` IGNORA a env PORT, e o `npm start` do repo
        #                   é `ng serve --configuration development` sem `--port`.
        #                   Sem a flag ele sobe na 4200 e o Service aponta para
        #                   outra porta.
        #   --allowed-hosts o dev server é Vite (Angular 19) e recusa Host que
        #                   não conheça: HTTP 403 "Blocked request. This host is
        #                   not allowed." Liberamos O HOST DO INGRESS, que é
        #                   determinístico e nosso — não `--disable-host-check`,
        #                   que abriria o servidor para rebinding de DNS por um
        #                   ganho de zero (o hostname já é conhecido aqui).
        flags = f"--host 0.0.0.0 --port {port}"
        preview_host = cfg.external_hostname_for(namespace)
        if preview_host:
            flags += f" --allowed-hosts {preview_host}"
        if api_proxy_target:
            proxy_conf = json.dumps({"/api": {
                "target": api_proxy_target, "changeOrigin": True, "secure": False,
            }})
            proxy_step = f"printf '%s' {json.dumps(proxy_conf)} > proxy.preview.json; "
            flags += " --proxy-config proxy.preview.json"
        # TENTA o que o repositório tem, em vez de exigir um nome. Quarta vez
        # que a receita morre por assumir uma forma: `ng serve` em `::1` (PR
        # #19), `browserTarget` no angular.json (PRs #2 e #3), `npm error
        # Missing script: "start"` (PR #5) — e agora o script que EXISTE e
        # morre no arranque (PR #6: o angular.json gerado aponta o serve para
        # `@angular-devkit/build-angular:dev-server`, fora das devDependencies;
        # CrashLoop ×9 até o timeout de 900s).
        #
        # A ordem é de mais específico para mais genérico. Os degraus de dev
        # server NÃO usam `exec` de propósito: se o servidor escolhido morrer —
        # ou nem existir — a cadeia cai no único contrato que o L1 acabou de
        # provar verde no MESMO item, `npm run build`, e serve o artefato
        # estático (`serve -s` = SPA fallback; versão pinada, P7). A entrega se
        # ANUNCIA no log: é o que separa "o dev server do repo está quebrado"
        # de um preview saudável, e é o marker que `built_fallback_note` leva
        # até a linha da PR. Se nem o build existir, a mensagem é NOSSA: o erro
        # cru do npm manda o humano depurar a ferramenta, quando o problema é o
        # repositório não declarar como se serve.
        # Aspas duplas SIMPLES aqui: o script inteiro vai para o YAML via
        # `json.dumps`, que faz o escape. Escapar à mão aqui escaparia duas
        # vezes e o `sh` receberia barras literais.
        tem = ('node -e "process.exit(((require(\'./package.json\').scripts)||{})'
               '.%s?0:1)" 2>/dev/null')
        start = (
            f"PORT={port}; "
            f"if {tem % 'start'}; then npm start -- {flags} || true; "
            f"elif {tem % 'dev'}; then npm run dev -- {flags} || true; "
            f"elif [ -x node_modules/.bin/ng ]; then node_modules/.bin/ng serve {flags} || true; fi; "
            "echo 'DSE preview: the dev server is missing or exited; "
            "serving the production build instead'; "
            f"if ! {tem % 'build'}; then echo 'DSE preview: no way to serve this "
            "repository - the dev server (start/dev/local Angular CLI) is missing "
            "or broken, and there is no build script to fall back to'; exit 1; fi; "
            "npm run build; "
            "DIST=$(find dist build -name index.html 2>/dev/null | head -1); "
            "if [ -z \"$DIST\" ]; then echo 'DSE preview: npm run build succeeded "
            "but produced no index.html under dist/ or build/'; exit 1; fi; "
            f"exec npx --yes serve@14 -s \"$(dirname \"$DIST\")\" -l tcp://0.0.0.0:{port}"
        )
        script = (
            "set -eu; "
            "apk add --no-cache git openssh-client >/dev/null 2>&1 || true; "
            + git_env +
            f"git clone --depth 1 --branch '{branch}' '{clone_url}' /srv/app; "
            "cd /srv/app; "
            "npm install --no-audit --no-fund --loglevel=error; "
            + proxy_step + start
        )
        env_yaml = f"""            - name: PORT
              value: "{port}"
            - name: NODE_ENV
              value: "development"
"""
        probe_yaml = f"""          readinessProbe:
            httpGet: {{ path: /, port: {port} }}
            periodSeconds: {period}
            failureThreshold: {failure_threshold}
"""
        # 6Gi/3 desde 2026-08-11. Com 768Mi o container era OOMKilled (exit
        # 137) ANTES de terminar o `npm install`+compilação: medido no preview
        # da PR #19, `Reason: OOMKilled` com `Restart Count: 1`. Este teto foi
        # escrito quando o nó tinha 2 cores e 8 GiB; hoje são 8 e 31, e o teto
        # é TETO — o request continua baixo, então isto não reserva nada.
        #
        # Note que o `--max-old-space-size` que o sandbox deriva NÃO alcança
        # aqui: o preview é outro Pod, com outro limite, e o V8 dele mira este.
        resources_yaml = """          resources:
            requests: { cpu: "200m", memory: "512Mi" }
            limits: { cpu: "3", memory: "6Gi" }
"""
    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: preview
  namespace: {namespace}
  labels:
{labels}spec:
  replicas: 1
  selector:
    matchLabels:
      app: preview
  template:
    metadata:
      labels:
        app: preview
    spec:
      volumes:
        - name: deploy-key
          secret:
            secretName: {DEPLOY_KEY_SECRET}
            defaultMode: 256
      containers:
        - name: web
          image: {image}
          command: ["sh", "-c"]
          args:
            - {json.dumps(script)}
          volumeMounts:
            - name: deploy-key
              mountPath: /preview-keys
              readOnly: true
          ports:
            - containerPort: {port}
          env:
{env_yaml}{resources_yaml}{probe_yaml}"""


def read_repo_preview(repo: str, branch: str) -> RepoPreviewDeclaration | None:
    """Lê o bloco `preview` do `.dse/validation.json` do repo, no branch da PR.

    Em Python e não pelo `jq` de dentro do pod porque a IMAGEM sai daqui — ela
    precisa estar escolhida antes de o pod existir. Usa o mesmo leitor da
    triage (`github_client.get_file_text`), e é best-effort pelo mesmo motivo:
    manifesto ausente, ilegível ou torto significa "sem declaração", nunca
    preview derrubado. Declaração inválida (chave desconhecida, tipo errado)
    também degrada para None — o L1 é quem reprova manifesto inválido, com
    mensagem própria; o preview não é lugar de dar essa notícia.
    """
    try:
        from dse_validation.config import L1_MANIFEST_PATH, parse_repo_preview
        from dse_validation.github.client import GitHubConfig, build_github_client

        client = build_github_client(GitHubConfig())
        reader = getattr(client, "get_file_text", None)
        if reader is None:
            return None
        texto = reader(repo, L1_MANIFEST_PATH, branch)
        if not texto:
            return None
        return parse_repo_preview(json.loads(texto), source=f"{repo}@{branch}")
    except Exception as exc:  # noqa: BLE001 — ver docstring
        logger.info("preview: no usable declaration in %s@%s (%s)", repo, branch, exc)
        return None


def build_manifests(namespace: str, work_item_id: str, tenant_id: str,
                    expires_at: datetime, ttl_seconds: int, cfg: PreviewConfig,
                    *, image: str | None = None, app_port: int | None = None,
                    repo: str = "", branch: str = "", kind: str = "ui",
                    api_proxy_target: str | None = None,
                    auth_mode: str = "ssh",
                    repo_preview: RepoPreviewDeclaration | None = None) -> dict[str, str]:
    """Plan 08 §D: `image` (D4 — the PR image; defaults to the cfg placeholder)
    and `app_port` (the app's port inside the container; Service/Ingress publish
    80 → targetPort). When `cfg.external_host_template` is set, also generates
    the INGRESS (D3) with the hostname derived from the template — the PR link
    becomes clickable from the outside (local Traefik / tunnel / VPS, same
    mechanism)."""
    image = image or cfg.preview_image
    port = app_port or cfg.app_port
    # A k8s label VALUE is capped at 63 chars (finding from the real run: the
    # work_item_id is `wi_`+64 hex = 67 chars → the namespace was REJECTED by
    # Argo, the sync failed and the namespace never came to life → degraded
    # preview with no URL). The FULL id goes into the annotation (no size
    # limit); the label carries the truncated version, which is enough for
    # selection/labelling.
    wi_label = work_item_id[:63]
    tenant_label = tenant_id[:63]
    labels = (
        f"    app.kubernetes.io/managed-by: dse-preview\n"
        f"    dse.fintex/work-item: \"{wi_label}\"\n"
        f"    dse.fintex/tenant: \"{tenant_label}\"\n"
    )
    ns = f"""apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
  labels:
{labels}  annotations:
    dse.fintex/work-item-id: "{work_item_id}"
    dse.fintex/expires-at: "{expires_at.isoformat()}"
    janitor/ttl: "{ttl_seconds}s"  # kube-janitor upgrade path (see docstring)
"""
    if cfg.mode == "source":
        deploy = _source_deployment(namespace, labels, cfg, repo=repo, branch=branch,
                                    kind=kind, api_proxy_target=api_proxy_target,
                                    auth_mode=auth_mode, repo_preview=repo_preview)
        # A porta que o Service/Ingress publicam tem que ser a MESMA que o
        # container escuta — se o repo declarou a dele, o encaminhamento segue
        # junto, senão o preview responde 502 com o pod saudável.
        port = (repo_preview.port if repo_preview and repo_preview.port
                else cfg.source_port)
    else:
        deploy = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: preview
  namespace: {namespace}
  labels:
{labels}spec:
  replicas: 1
  selector:
    matchLabels:
      app: preview
  template:
    metadata:
      labels:
        app: preview
    spec:
      containers:
        - name: web
          image: {image}
          ports:
            - containerPort: {port}
          readinessProbe:
            httpGet: {{ path: /, port: {port} }}
            initialDelaySeconds: 1
            periodSeconds: 2
"""
    svc = f"""apiVersion: v1
kind: Service
metadata:
  name: preview
  namespace: {namespace}
  labels:
{labels}spec:
  selector:
    app: preview
  ports:
    - port: 80
      targetPort: {port}
"""
    manifests = {"namespace.yaml": ns, "deployment.yaml": deploy, "service.yaml": svc}

    # G-2: Postgres efêmero no namespace para o preview `deployable` — o app
    # Spring aponta seu datasource para o Service `postgres` e o Flyway/JPA do
    # PRÓPRIO app migra no boot (a migração da PR viaja no jar). emptyDir: o
    # dado morre com o namespace no TTL, que é exatamente o que se quer.
    if kind == "deployable":
        manifests["postgres.yaml"] = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: postgres
  namespace: {namespace}
  labels:
{labels}spec:
  replicas: 1
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      volumes:
        - name: data
          emptyDir: {{}}
      containers:
        - name: postgres
          image: postgres:16-alpine
          env:
            - name: POSTGRES_DB
              value: "{cfg.preview_db_name}"
            - name: POSTGRES_USER
              value: "preview"
            - name: POSTGRES_PASSWORD
              value: "preview"
            - name: PGDATA
              value: "/var/lib/postgresql/data/pgdata"
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
          ports:
            - containerPort: 5432
          readinessProbe:
            exec: {{ command: ["pg_isready", "-U", "preview", "-d", "preview"] }}
            periodSeconds: 5
            failureThreshold: 12
          resources:
            requests: {{ cpu: "50m", memory: "128Mi" }}
            limits: {{ cpu: "500m", memory: "512Mi" }}
---
apiVersion: v1
kind: Service
metadata:
  name: postgres
  namespace: {namespace}
  labels:
{labels}spec:
  selector:
    app: postgres
  ports:
    - port: 5432
      targetPort: 5432
"""

    hostname = cfg.external_hostname_for(namespace)
    if hostname:
        # TLS não é higiene, é REQUISITO FUNCIONAL do preview. Medido na PR #19:
        # sobre `http://` o app subiu inteiro e ficou em branco, com
        # `auth0-spa-js must run on a secure origin` no console. Origem insegura
        # desliga `crypto.subtle`, service workers e todo SDK de auth — e o
        # defeito é silencioso, porque o `curl` e a sonda continuam vendo 200.
        #
        # As duas anotações são as MESMAS que os ingresses do próprio DSE já
        # usam (resolver ACME `le` do Traefik, HTTP-01). Sem `certresolver` o
        # bloco `tls` é inerte; sem `entrypoints=websecure` o router não atende
        # na 443. `tls` sem `secretName` de propósito: quem materializa o
        # certificado é o resolver, não um Secret que alguém teria de criar.
        manifests["ingress.yaml"] = f"""apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: preview
  namespace: {namespace}
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: websecure
    traefik.ingress.kubernetes.io/router.tls.certresolver: {cfg.tls_cert_resolver}
  labels:
{labels}spec:
  ingressClassName: {cfg.ingress_class}
  tls:
    - hosts:
        - {hostname}
  rules:
    - host: {hostname}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: preview
                port:
                  number: 80
"""
    return manifests


APPLICATIONSET_TEMPLATE = """apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: {name}
  namespace: {argocd_ns}
spec:
  goTemplate: true
  generators:
    - git:
        repoURL: {repo_url}
        revision: main
        directories:
          - path: previews/*
        requeueAfterSeconds: 15
  template:
    metadata:
      name: "{{{{.path.basename}}}}"
      finalizers:
        - resources-finalizer.argocd.argoproj.io
    spec:
      project: default
      source:
        repoURL: {repo_url}
        targetRevision: main
        path: "{{{{.path.path}}}}"
      destination:
        server: https://kubernetes.default.svc
        namespace: "{{{{.path.basename}}}}"
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
"""


def ensure_applicationset(cfg: PreviewConfig | None = None) -> None:
    """Idempotent: applies the `dse-previews` ApplicationSet (git generator)."""
    cfg = cfg or PreviewConfig()
    manifest = APPLICATIONSET_TEMPLATE.format(
        name=cfg.applicationset_name,
        argocd_ns=cfg.argocd_namespace,
        repo_url=cfg.repo_url_in_cluster,
    )
    _kubectl(cfg, ["apply", "-f", "-"], input_text=manifest)


_PREVIEW_BODY_MARKER = "<!-- dse:preview -->"
# The `- **Preview**: <url>` line in the PR body, terminated by the marker
# (invisible in rendered markdown) that allows RE-writing the line instead of
# duplicating it.
_PREVIEW_LINE_RE = re.compile(r"^- \*\*Preview\*\*:.*" + re.escape(_PREVIEW_BODY_MARKER) + r"$", re.M)
# insertion point: right after the L1 evidence bullet of the PR template.
_EVIDENCE_BULLET_PREFIX = "- **Test evidence (L1)**:"


def _preview_body_with_line(body: str, sentence: str) -> str:
    """The PR body with the `- **Preview**: …` line inserted/updated (idempotent
    via the marker). Inserts after the L1 evidence bullet; if the template
    changes, appends at the end.

    Takes a SENTENCE, not a URL: the same line carries the link when the preview
    is up and the reason when it is not, so both go through one writer and one
    marker instead of the success path having a home and every failure having
    none."""
    line = f"{sentence} {_PREVIEW_BODY_MARKER}"
    if _PREVIEW_LINE_RE.search(body):
        return _PREVIEW_LINE_RE.sub(line, body)
    lines = body.splitlines()
    out: list[str] = []
    inserted = False
    for ln in lines:
        out.append(ln)
        if not inserted and ln.startswith(_EVIDENCE_BULLET_PREFIX):
            out.append(line)
            inserted = True
    if not inserted:
        out.append(line)
    return "\n".join(out)


#: Quanto do `detail` do runtime cabe na frase. O detail pode ser um dump de
#: log inteiro; o corpo da PR não é lugar para ele.
_PREVIEW_DETAIL_CHARS = 300
#: Quando não cabe, guarda-se o COMEÇO e o FIM, e o meio é o que se perde.
#: Cortar só pela cabeça foi medido falhando na PR #6 (2026-08-11): o namespace
#: tem 63 chars, então o boilerplate do `kubectl wait` sozinho ocupa ~254 dos
#: 300 — e as palavras do pod, que vêm depois e carregam a causa no FIM do log
#: (`Error: Could not find … dev-server …`), nunca chegavam à PR. A informação
#: já estava capturada e era descartada a um passo do humano.
_DETAIL_HEAD_CHARS = 150


def _fit_detail(cause: str) -> str:
    """`cause` na medida da frase: inteira quando cabe; senão começo (que
    identifica o desfecho) + fim (onde o erro mora), com o meio elidido."""
    if len(cause) <= _PREVIEW_DETAIL_CHARS:
        return cause
    tail = _PREVIEW_DETAIL_CHARS - _DETAIL_HEAD_CHARS - 5  # 5 = " […] "
    return f"{cause[:_DETAIL_HEAD_CHARS]} […] {cause[-tail:]}"


def preview_body_line(
    status: str,
    *,
    url: str | None = None,
    namespace: str | None = None,
    expires_at: object | None = None,
    detail: str | None = None,
) -> str | None:
    """A ÚNICA frase que descreve um preview para um humano. `None` = não falar.

    Exaustiva sobre os status de propósito. O modo de falha de 2026-08-11 não
    foi erro, foi SILÊNCIO: seis causas diferentes, seis PRs sem uma palavra
    sobre o preview, com a informação viva em três tabelas que ninguém abre. Por
    isso um status desconhecido produz uma frase feia com o status cru, em vez
    de `None` — feio é recuperável, mudo não é.

    Uma linha só, e isso é requisito e não estilo: o marker de idempotência
    (`_PREVIEW_LINE_RE`, `re.M`) casa UMA linha. Uma seção multi-linha faria o
    re-trigger do fix cycle empilhar frases contraditórias em vez de substituir.
    """
    def _sentence(text: str) -> str:
        return f"- **Preview**: {' '.join(text.split())}"

    if status in ("skipped_disabled", "skipped_backend_only"):
        # Decisão de operador (2026-08-11): pulo legítimo não gera seção. Uma
        # linha "não se aplica" em toda PR de backend é ruído, não informação.
        return None

    cause = (detail or "").strip()
    if status == "created":
        target = url or "(no URL)"
        extra = f" (namespace `{namespace}`)" if namespace else ""
        # `created` com detail não engole o detail: é o caso do preview que
        # subiu SERVINDO O BUILD porque o dev server do repo está quebrado —
        # só a URL na frase disfarçaria o defeito de dev server são.
        nota = f" — {_fit_detail(cause)}" if cause else ""
        return _sentence(f"{target}{extra}{nota}")

    if status == "reaped":
        return _sentence(
            "expired (TTL) — the environment was reaped and the link no longer resolves")

    if status == "degraded":
        if cause:
            return _sentence(f"did not come up — {_fit_detail(cause)}")
        return _sentence(
            "did not come up, and the runtime gave no reason (see the item's audit ledger)")

    # Desconhecido: fala mesmo assim, com o status cru.
    rest = f" — {_fit_detail(cause)}" if cause else ""
    return _sentence(f"state `{status}`{rest}")


def preview_autofix_line(cause: str, round_: int, cap: int) -> str:
    """A frase da PR ENQUANTO o auto-fix corre: o degradado + o que está sendo
    feito a respeito. Sem ela, quem abre a PR no meio do laço vê um degradado
    parado — e "parado" e "sendo consertado" são estados diferentes. O mesmo
    marker de sempre: o announce/desfecho do re-preview a substitui."""
    causa = " ".join((cause or "").split())
    meio = f"did not come up — {_fit_detail(causa)} — " if causa else "did not come up — "
    return (
        f"- **Preview**: {meio}automatic fix attempt {round_}/{cap} is running; "
        f"a new preview will follow"
    )


def _put_preview_in_pr_body(
    inp: TriggerPreviewInput, line: str | None, *, actor: str
) -> None:
    """Escreve a frase do preview na DESCRIÇÃO da PR. Idempotente: um re-trigger
    reescreve a mesma linha.

    `line=None` significa "não há o que dizer" (pulo legítimo) — e é a única
    razão aceitável para não escrever nada. Qualquer OUTRA saída sem escrita
    emite evento: o `except` mudo que existia aqui engolia 401, PR inexistente e
    cliente falso com um `logger.warning`, então quando a escrita falhava nem o
    ledger sabia — e o sintoma era indistinguível de "o preview nunca rodou".
    """
    if line is None:
        return
    if not inp.pr_number or not inp.repo:
        _preview_body_failed(inp, actor, "no pr_number/repo — there is no PR to write to")
        return
    try:
        from dse_validation.github.client import GitHubConfig, build_github_client

        client = build_github_client(GitHubConfig())
        pr = client.get_pull_request(inp.repo, int(inp.pr_number))
        if pr is None:
            _preview_body_failed(inp, actor, f"PR #{inp.pr_number} not found in {inp.repo}")
            return
        body = pr.get("body") or ""
        new_body = _preview_body_with_line(body, line)
        if new_body == body:
            return  # já dizia exatamente isto
        client.update_pull_request(inp.repo, int(inp.pr_number), body=new_body)
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_line_in_pr_body", tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"pr_number": inp.pr_number, "line": line[:300]},
            )
    except Exception as exc:  # noqa: BLE001 — nunca bloqueia o item, mas nunca some
        _preview_body_failed(inp, actor, f"{type(exc).__name__}: {exc}")


def _announce_preview_provisioning(
    inp: TriggerPreviewInput, url: str, *, actor: str
) -> None:
    """Diz na PR que o ambiente ESTÁ SUBINDO, assim que ele começa a subir.

    Medido na PR #5 (2026-08-11): a PR finalizou e ficou sem linha nenhuma
    durante todo o provisionamento — o `trigger_preview_core` estava dentro do
    `_wait_deployment_available`, que espera até 900s, e a frase só era escrita
    no `return`. Para quem abriu a PR nesse intervalo, quinze minutos de
    silêncio e "falhou de novo" são a mesma coisa; foi essa a leitura.

    A URL já é conhecida aqui — ela sai do namespace, não do pod — então
    escondê-la até o container ficar Ready não protege de nada. A linha é
    substituída pelo desfecho quando ele chega, via o mesmo marker.
    """
    _put_preview_in_pr_body(
        inp,
        f"- **Preview**: {url} (environment is starting — this can take a few minutes)",
        actor=actor,
    )


def _preview_body_failed(inp: TriggerPreviewInput, actor: str, why: str) -> None:
    """Todo caminho que NÃO escreveu deixa rastro. Sem isto, a ausência da linha
    na PR tem duas explicações indistinguíveis: o preview não rodou, ou rodou e
    a escrita falhou."""
    logger.warning("preview line not written (%s): %.200s", inp.work_item_id, why)
    if audit_emit is not None:
        audit_emit(
            actor=actor, action="preview_line_in_pr_body_failed", tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={"pr_number": inp.pr_number, "reason": why[:300]},
        )


def _apply_manifests(cfg: PreviewConfig, manifests: dict[str, str],
                     *, after_namespace=None) -> None:
    """Apply the preview manifests straight to the cluster.

    The alternative is the GitOps path, where Argo CD is what actually creates
    the objects — and Argo also prunes them when the directory disappears.
    Applying directly removes that dependency and everything it needs (an Argo
    install, a reachable manifests repo), at the cost of that free garbage
    collection: with this path the TTL reaper is the ONLY thing that deletes a
    preview namespace, so it stops being optional.

    Namespace goes first, on its own: every other object is namespaced and a
    single combined apply would race against the namespace existing.
    """
    _kubectl(cfg, ["apply", "-f", "-"], input_text=manifests["namespace.yaml"])
    # G-1: a secret da deploy key entra ENTRE o namespace e o resto — o pod
    # monta esse volume, e um pod aplicado antes da secret fica em
    # ContainerCreating até o kubelet reconciliar.
    if after_namespace is not None:
        after_namespace()
    rest = "\n---\n".join(
        body for name, body in sorted(manifests.items()) if name != "namespace.yaml"
    )
    _kubectl(cfg, ["apply", "-f", "-"], input_text=rest)


def _wait_deployment_available(cfg: PreviewConfig, namespace: str, timeout_s: int) -> None:
    # the namespace only comes into existence after Argo CD's sync — wait in
    # THREE stages: namespace created, deployment CREATED, deployment Available.
    # The middle stage is essential (finding from the D3/D4 proof): `kubectl
    # wait --for=condition=...` with a specific NAME fails IMMEDIATELY if the
    # resource does not exist yet — and Argo applies namespace→deployment with a
    # gap of seconds on the first sync (a timing flake, not a logic one).
    _kubectl(
        cfg,
        ["wait", "--for=create", f"namespace/{namespace}", f"--timeout={timeout_s}s"],
        timeout=timeout_s + 15,
    )
    _kubectl(
        cfg,
        ["wait", "-n", namespace, "--for=create", "deployment/preview",
         f"--timeout={timeout_s}s"],
        timeout=timeout_s + 15,
    )
    _kubectl(
        cfg,
        ["wait", "-n", namespace, "--for=condition=Available", "deployment/preview",
         f"--timeout={timeout_s}s"],
        timeout=timeout_s + 15,
    )


def _harvest_expired_previews(tenant_id: str, *, actor: str) -> int:
    """Marca `reaped` as linhas já expiradas do tenant. Devolve quantas.

    Não é evicção: expirada não conta para o teto, então isto não libera vaga.
    É COLETA DE LIXO, e existe porque `reap_expired_previews` — o único código
    que fazia isso — nunca teve call site (admitido no README e no BACKLOG do
    repositório). O resultado prático estava no banco de produção: quatro linhas
    de 29 a 31 de julho ainda `created` em 11 de agosto.
    """
    colhidas = 0
    for row in db.list_expired_previews():
        if row.get("tenant_id") != tenant_id:
            continue
        db.mark_preview_reaped(row["work_item_id"])
        colhidas += 1
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_harvested_expired", tenant_id=tenant_id,
                work_item_id=row["work_item_id"],
                details={"namespace": row.get("namespace")},
            )
    return colhidas


def _reconcile_previews_with_cluster(
    cfg: PreviewConfig, tenant_id: str, *, actor: str
) -> int:
    """Linhas que CONTAM mas cujo namespace não existe mais viram `reaped`.

    O teto tem de medir recurso, não memória. O CronJob que apaga namespaces
    nunca escreve nesta tabela (decisão declarada no manifesto dele), então sem
    esta reconciliação um namespace apagado — à mão, por GC, por reboot do nó —
    fica ocupando vaga até o TTL, que na VPS é de 6 horas.

    FAIL-CLOSED por construção: se o `kubectl` não responde, a linha continua
    contando. "Não sei" é tratado como "está vivo", porque liberar vaga por erro
    de leitura poria dois previews no mesmo recurso — um modo de falha bem mais
    difícil de enxergar que um bloqueio.
    """
    liberadas = 0
    for row in db.list_oldest_active_previews(tenant_id, limit=100):
        ns = row.get("namespace") or namespace_for(row["work_item_id"])
        try:
            existe = namespace_exists(cfg, ns)
        except Exception as exc:  # noqa: BLE001 — "não sei" ≠ "não existe"
            logger.warning("preview: could not check namespace %s (%s); "
                           "keeping the slot taken", ns, exc)
            continue
        if existe:
            continue
        db.mark_preview_reaped(row["work_item_id"])
        liberadas += 1
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_slot_reconciled", tenant_id=tenant_id,
                work_item_id=row["work_item_id"],
                details={"namespace": ns, "reason": "namespace no longer exists"},
            )
    return liberadas


# ---------------------------------------------------------------------------
# trigger_preview — core of the contract's Activity
# ---------------------------------------------------------------------------
def trigger_preview_core(
    inp: TriggerPreviewInput,
    *,
    cfg: PreviewConfig | None = None,
    ttl_seconds: int | None = None,
    actor: str = "system:validation",
) -> PreviewRef:
    """A ÚNICA saída do preview, e é por isso que ela existe.

    `_trigger_preview` tem cinco `return` e pode ganhar um sexto amanhã. Enquanto
    a escrita na PR morava DENTRO dele, depois do último `return`, só o caminho
    de sucesso falava com o humano — e as cinco falhas de 2026-08-11 saíram
    caladas, cada uma por um `return` diferente.

    Aqui a frase é escrita FORA, sobre o `PreviewRef` que voltou, qualquer que
    seja ele. Um `return` novo não tem como escapar, e uma exceção também não:
    ela vira frase antes de subir, porque para quem revisa a PR "estourou" e
    "nunca rodou" são o mesmo silêncio.
    """
    try:
        ref = _trigger_preview(inp, cfg=cfg, ttl_seconds=ttl_seconds, actor=actor)
    except Exception as exc:  # noqa: BLE001 — fala e depois deixa subir
        _put_preview_in_pr_body(
            inp,
            preview_body_line("degraded", detail=f"{type(exc).__name__}: {exc}"),
            actor=actor,
        )
        raise
    _put_preview_in_pr_body(
        inp,
        preview_body_line(
            ref.status, url=ref.url, namespace=ref.namespace, detail=ref.detail,
        ),
        actor=actor,
    )
    return ref


def _trigger_preview(
    inp: TriggerPreviewInput,
    *,
    cfg: PreviewConfig | None = None,
    ttl_seconds: int | None = None,
    actor: str = "system:validation",
) -> PreviewRef:
    cfg = cfg or PreviewConfig()
    ttl = ttl_seconds or cfg.default_ttl_seconds

    # 0) Plan 08 §D — operator-set gate (repo_bindings.deploys_preview). A repo
    # not marked as "generates preview" skips CLEANLY (never blocks). Distinct
    # from backend-only: here the operator declared that the repo has no
    # preview.
    if not inp.preview_enabled:
        db.upsert_preview(
            work_item_id=inp.work_item_id, tenant_id=inp.tenant_id,
            pr_number=inp.pr_number, repo=inp.repo, status="skipped_disabled",
            detail="repo not marked deploys_preview (plan 08 §D)", ttl_seconds=ttl,
        )
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_skipped_disabled",
                tenant_id=inp.tenant_id, work_item_id=inp.work_item_id,
                details={"pr_number": inp.pr_number, "repo": inp.repo},
            )
        return PreviewRef(
            work_item_id=inp.work_item_id, pr_number=inp.pr_number,
            status="skipped_disabled", detail="repo without preview (deploys_preview=false)",
        )

    # 1) FR-20 + plan 08 §D — deterministic paths-filter (P1). A preview is
    # worth it if the change touches UI (front end) OR a deployable service
    # (back end). Only docs/tests → skipped_backend_only, which counts as
    # SUCCESS and NEVER blocks.
    kind, matched = preview_decision(inp.files_changed, inp.ui_path_globs, inp.deployable_globs)
    if kind == "none":
        db.upsert_preview(
            work_item_id=inp.work_item_id, tenant_id=inp.tenant_id,
            pr_number=inp.pr_number, repo=inp.repo, status="skipped_backend_only",
            detail="no file matches ui/deployable globs (FR-20 + §D)", ttl_seconds=ttl,
        )
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_skipped_backend_only",
                tenant_id=inp.tenant_id, work_item_id=inp.work_item_id,
                details={"pr_number": inp.pr_number, "files_changed": inp.files_changed,
                         "ui_path_globs": inp.ui_path_globs, "deployable_globs": inp.deployable_globs},
            )
        return PreviewRef(
            work_item_id=inp.work_item_id, pr_number=inp.pr_number,
            status="skipped_backend_only", detail="no previewable change (docs/tests)",
        )

    ui_files = matched

    # 2) ADR-26 — cap on concurrent previews per tenant (day 1).
    cap = db.get_preview_cap(inp.tenant_id)
    if cap is None:
        cap = cfg.default_max_concurrent
    existing = db.get_preview(inp.work_item_id)
    already_active = existing is not None and existing["status"] == "created" and existing["reaped_at"] is None

    # ANTES de contar: colher o lixo e reconciliar com o cluster. Ordem
    # deliberada — a evicção interrompe o preview de alguém e por isso é o
    # ÚLTIMO recurso, não o primeiro.
    #
    # (a) LIXO: linhas expiradas. Não contam para o teto, então colhê-las não
    #     libera vaga nenhuma — mas enquanto ninguém as colhia elas se
    #     acumulavam (havia quatro de julho, penduradas duas semanas) e
    #     envenenavam a fila de evicção, que as ordenava primeiro. Custa uma
    #     linha e é de graça.
    _harvest_expired_previews(inp.tenant_id, actor=actor)
    # (b) FANTASMAS: linhas que contam mas cujo namespace não existe mais —
    #     apagado à mão, por GC, ou por reboot do nó. Sem esta reconciliação o
    #     teto mede memória em vez de recurso, e a vaga só voltaria depois do
    #     TTL (6h na VPS). Foi o que degradou a PR #4 com o cluster vazio.
    _reconcile_previews_with_cluster(cfg, inp.tenant_id, actor=actor)

    active = db.count_active_previews(inp.tenant_id)
    if not already_active and active >= cap and cap > 0:
        # LRU eviction (operator decision 2026-07-23): cap full => the OLDEST
        # preview yields its slot to the new PR (recency wins; the cap remains
        # the hard ceiling on concurrency). cap == 0 still means "tenant without
        # previews" — nothing to evict. A removal failure does NOT take down the
        # flow here: it falls into the degraded path right below (failure
        # mode 9).
        for row in db.list_oldest_active_previews(inp.tenant_id, limit=active - cap + 1):
            old_ns = row["namespace"] or namespace_for(row["work_item_id"])
            # Per row, not around the loop. One `try` covering the whole loop
            # meant the first failure aborted every remaining eviction, so a
            # single stuck row froze the cap for the entire tenant.
            removed = True
            try:
                gitops.remove_preview_dir(cfg.repo_dir, old_ns)
            except Exception as exc:  # noqa: BLE001 — see below: the slot is freed regardless
                removed = False
                logger.warning(
                    "could not remove the preview directory for %s (%s: %s); freeing the slot anyway",
                    old_ns, type(exc).__name__, exc,
                )
            # Freed whether or not the removal worked, and this is the whole
            # fix. The row is ACCOUNTING; the namespace is the RESOURCE, and the
            # reaper collects the resource on its own — it works from cluster
            # state and the namespace's own expiry annotation, and never reads
            # this table. So a failed removal leaks at most one namespace until
            # its TTL, while a slot that is never released disables previews for
            # the tenant permanently. That is exactly how this broke: the
            # removal raised because the reaper had already deleted those
            # namespaces days earlier, and the eviction it aborted was the only
            # thing that ever freed a slot.
            db.mark_preview_reaped(row["work_item_id"])
            if audit_emit is not None:
                audit_emit(
                    actor=actor, action="preview_evicted_lru", tenant_id=inp.tenant_id,
                    work_item_id=row["work_item_id"],
                    details={"namespace": old_ns, "evicted_for": inp.work_item_id,
                             "pr_number": inp.pr_number, "cap": cap,
                             "namespace_removed": removed},
                )
        active = db.count_active_previews(inp.tenant_id)
    if not already_active and active >= cap:
        detail = f"tenant concurrent preview cap reached ({active}/{cap}, ADR-26)"
        db.upsert_preview(
            work_item_id=inp.work_item_id, tenant_id=inp.tenant_id,
            pr_number=inp.pr_number, repo=inp.repo, status="degraded",
            detail=detail, ttl_seconds=ttl,
        )
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_degraded", tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"pr_number": inp.pr_number, "reason": "concurrency_cap", "active": active, "cap": cap},
            )
        return PreviewRef(
            work_item_id=inp.work_item_id, pr_number=inp.pr_number,
            status="degraded", detail=detail,
        )

    # 3-4) GitOps + Argo CD against the real cluster. Any failure => degraded
    # (failure mode 9 — a preview never blocks the PR forever).
    namespace = namespace_for(inp.work_item_id)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

    # D4 — the REAL PR image when enabled (fail-safe: None => placeholder, with
    # the reason audited; the build never degrades the preview).
    from dse_validation.preview.pr_image import build_pr_image
    pr_image, image_reason, detected_port = build_pr_image(
        work_item_id=inp.work_item_id, repo=inp.repo, head_sha=inp.head_sha, cfg=cfg,
    )
    # The port detected during synthesis (Node app) beats the cfg default —
    # otherwise readiness/Service would point at the wrong port and the preview
    # would degrade.
    app_port = detected_port or cfg.app_port

    # The branch the finalizer pushed for this work item. In `source` mode this
    # is what the preview container clones, so it has to match the convention
    # `finalize_pr` uses — the PR head branch, not the SHA.
    branch = f"dse/{inp.work_item_id}"
    ready_timeout = (
        max(cfg.sync_timeout_s, cfg.source_ready_timeout_s)
        if cfg.mode == "source" else cfg.sync_timeout_s
    )

    # G-3 degrau 2: se este item tem um irmão de group_id com preview VIVO, o
    # proxy /api do FE aponta para o backend dele (svc in-cluster). Só para
    # kind=ui; deployable não faz proxy.
    api_proxy_target = None
    if kind == "ui":
        sibling_ns, sibling_alive = db.live_sibling_preview_namespace(inp.work_item_id)
        api_proxy_target = resolve_fe_api_target(sibling_ns, sibling_alive, cfg)

    # G-1' — a credencial de clone: secret semeada (precedência) ou
    # installation token da App, mintado AGORA. Resolvida ANTES dos manifestos
    # porque o modo decide o script do pod (SSH vs HTTPS+helper).
    auth_mode, auth_material = resolve_preview_credential(cfg, inp.repo)
    if auth_mode is None:
        auth_mode = "ssh"  # sem credencial: o erro nomeado vem do hook abaixo

    # G7: a forma do repo vem do repo. Lido AQUI, em Python, e não pelo `jq`
    # de dentro do pod, porque a imagem precisa ser escolhida antes de o pod
    # existir. Best-effort pelo mesmo motivo da triage: manifesto ilegível não
    # pode derrubar o preview — sem declaração, tudo segue como antes.
    repo_preview = read_repo_preview(inp.repo, branch)

    try:
        manifests = build_manifests(namespace, inp.work_item_id, inp.tenant_id, expires_at, ttl, cfg,
                                    image=pr_image, app_port=app_port,
                                    repo=inp.repo, branch=branch, kind=kind,
                                    api_proxy_target=api_proxy_target,
                                    auth_mode=auth_mode, repo_preview=repo_preview)
        if cfg.apply_mode == "kubectl":
            def _seed_credential() -> None:
                if not auth_material:
                    raise RuntimeError(
                        f"no clone credential for {inp.repo}: the GitHub App is not "
                        f"configured and no deploy key is seeded in "
                        f"{cfg.deploy_keys_secret} (see deploy/vps/preview-deploy-keys.md)"
                    )
                apply_preview_credential(cfg, namespace, auth_mode, auth_material)
            _apply_manifests(cfg, manifests, after_namespace=_seed_credential)
        else:
            gitops.write_preview_dir(cfg.repo_dir, namespace, manifests)
            ensure_applicationset(cfg)
        # A PR fala ANTES da espera, não depois. `_wait_deployment_available`
        # segura por até `ready_timeout` (900s em produção), e enquanto isso a
        # PR ficava sem uma palavra sobre o preview — indistinguível de falha
        # para quem a abrisse no intervalo. Foi o que aconteceu na PR #5.
        _announce_preview_provisioning(inp, cfg.preview_url_for(namespace), actor=actor)
        _wait_deployment_available(cfg, namespace, ready_timeout)
    except Exception as exc:  # degraded, never blocks (failure mode 9)
        # As palavras do POD, não as do relógio. `kubectl wait` só sabe dizer
        # que desistiu; nas PRs #2 e #3 o defeito estava inteiro no log do
        # container, a um `kubectl logs` que nenhum revisor de PR vai dar.
        detail = pod_failure_detail(
            cfg, namespace, f"preview degraded: {type(exc).__name__}: {exc}"
        )
        logger.warning("trigger_preview %s: %s", inp.work_item_id, detail)
        db.upsert_preview(
            work_item_id=inp.work_item_id, tenant_id=inp.tenant_id,
            pr_number=inp.pr_number, repo=inp.repo, status="degraded",
            namespace=namespace, detail=detail[:900], ttl_seconds=ttl, expires_at=expires_at,
        )
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_degraded", tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"pr_number": inp.pr_number, "reason": "provision_failure", "error": detail[:500]},
            )
        return PreviewRef(
            work_item_id=inp.work_item_id, pr_number=inp.pr_number,
            status="degraded", namespace=namespace, detail=detail[:900], kind=kind,
        )

    # plan 08 §D (D3): external URL (browser-reachable) when configured;
    # otherwise the cluster-internal DNS (the link shows up on the PR either
    # way — D1).
    url = cfg.preview_url_for(namespace)
    # Available ≠ dev server são: pode ser o degrau do build servindo no lugar
    # de um `npm start` quebrado. A nota viaja no PreviewRef.detail até a frase
    # da PR — sem ela o fallback esconderia o defeito que acabou de contornar.
    nota = built_fallback_note(cfg, namespace) if kind == "ui" else None
    detail_row = f"{kind} files: {', '.join(ui_files[:10])} | image={image_reason}"
    if nota:
        detail_row += f" | {nota}"
    db.upsert_preview(
        work_item_id=inp.work_item_id, tenant_id=inp.tenant_id,
        pr_number=inp.pr_number, repo=inp.repo, status="created",
        namespace=namespace, url=url, ttl_seconds=ttl, expires_at=expires_at,
        detail=detail_row,
    )
    if audit_emit is not None:
        audit_emit(
            actor=actor, action="preview_created", tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={"pr_number": inp.pr_number, "namespace": namespace, "url": url,
                     "kind": kind, "ttl_seconds": ttl, "expires_at": expires_at.isoformat(),
                     "image": pr_image or cfg.preview_image, "image_source": image_reason,
                     "served_from_build": bool(nota), "files": ui_files[:20]},
        )
    return PreviewRef(
        work_item_id=inp.work_item_id, pr_number=inp.pr_number,
        status="created", namespace=namespace, url=url, kind=kind,
        detail=nota or "",
    )


# ---------------------------------------------------------------------------
# TTL reaper (deterministic Python job — see the decision in the module docstring)
# ---------------------------------------------------------------------------
def reap_expired_previews(
    *, cfg: PreviewConfig | None = None, actor: str = "system:validation", now=None
) -> list[str]:
    """Removes the expired previews (`expires_at <= now`) from GIT — the
    ApplicationSet prunes the Application and the finalizer cascades the
    namespace deletion. Marks `reaped` in wse_previews + audit. Idempotent."""
    cfg = cfg or PreviewConfig()
    reaped: list[str] = []
    for row in db.list_expired_previews(now):
        namespace = row["namespace"] or namespace_for(row["work_item_id"])
        gitops.remove_preview_dir(cfg.repo_dir, namespace)
        db.mark_preview_reaped(row["work_item_id"])
        reaped.append(row["work_item_id"])
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="preview_reaped", tenant_id=row["tenant_id"],
                work_item_id=row["work_item_id"], details={"namespace": namespace},
            )
    return reaped


def wait_namespace_gone(namespace: str, *, cfg: PreviewConfig | None = None, timeout_s: int = 180) -> None:
    """Waits for Argo CD's cascade delete to finish (for tests/verification)."""
    cfg = cfg or PreviewConfig()
    _kubectl(
        cfg,
        ["wait", "--for=delete", f"namespace/{namespace}", f"--timeout={timeout_s}s"],
        timeout=timeout_s + 15,
    )


def get_preview_http_status(namespace: str, *, cfg: PreviewConfig | None = None, timeout_s: int = 30) -> int:
    """Proof of 'URL responding' from outside the cluster: an ephemeral
    `kubectl run curl` inside the namespace hitting the Service — returns the
    HTTP status."""
    cfg = cfg or PreviewConfig()
    proc = _kubectl(
        cfg,
        ["run", "curl-probe", "-n", namespace, "--rm", "-i", "--restart=Never",
         "--image=curlimages/curl:8.10.1", "--command", "--",
         "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         f"http://preview.{namespace}.svc.cluster.local/"],
        timeout=timeout_s + 60,
    )
    digits = "".join(ch for ch in proc.stdout if ch.isdigit())
    return int(digits[:3]) if digits else 0


def _applicationset_status(cfg: PreviewConfig | None = None) -> dict:
    cfg = cfg or PreviewConfig()
    proc = _kubectl(cfg, ["get", "applicationset", "-n", cfg.argocd_namespace,
                          cfg.applicationset_name, "-o", "json"])
    return json.loads(proc.stdout)
