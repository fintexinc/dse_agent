# Deploy keys dos previews (G-1) — runbook do operador

O pod de preview clona o branch da PR **por SSH**, com uma **deploy key
read-only por repositório**. O DSE **só copia** essa chave da secret agregada
para o namespace do preview: ele nunca gera chave, nunca a registra no GitHub e
nunca retém material além do que o operador semeou (decisão (a), 2026-08-09).

Por que existe: os repos migraram para uma org privada em 2026-08-07 e o clone
do preview era anônimo (`https://github.com/...`, documentado como "public
repos only"). Resultado medido: **4 de 4 PRs** desde que `deploys_preview` foi
ligado terminaram em `preview_degraded`, cada uma gastando 300s até o timeout —
`fatal: could not read Username for 'https://github.com'`.

Quem roda: **o operador**, uma vez por repositório. Os comandos abaixo tocam
chave privada e a API do GitHub; o DSE não os executa.

---

## 1. Gerar um par por repositório

Uma chave por repo — o comprometimento de um preview não alcança o outro. Sem
passphrase (o pod não tem como digitá-la) e fora do repo do cliente:

```bash
mkdir -p ~/.dse-preview-keys && cd ~/.dse-preview-keys
ssh-keygen -t ed25519 -N '' -C 'dse-preview fe (read-only)' -f fe
ssh-keygen -t ed25519 -N '' -C 'dse-preview be (read-only)' -f be
```

## 2. Registrar a metade PÚBLICA como deploy key (read-only)

Sem `--allow-write` — o preview só lê. Se o repo já tiver uma key com o mesmo
título, remova antes (`gh repo deploy-key list -R <repo>`):

```bash
gh repo deploy-key add ~/.dse-preview-keys/fe.pub -R fintexinc/bmo-fee-calculator-fe-dse -t dse-preview
```

```bash
gh repo deploy-key add ~/.dse-preview-keys/be.pub -R fintexinc/bmo-fee-calculator-be-dse -t dse-preview
```

## 3. Semear a metade PRIVADA na secret agregada do namespace do DSE

O nome do item é o slug determinístico do repo (`owner/name` → `owner__name`),
que é o que `deploy_key_item_for()` procura:

```bash
sudo k3s kubectl create secret generic dse-preview-deploy-keys -n dse \
  --from-file=fintexinc__bmo-fee-calculator-fe-dse=$HOME/.dse-preview-keys/fe \
  --from-file=fintexinc__bmo-fee-calculator-be-dse=$HOME/.dse-preview-keys/be \
  --dry-run=client -o yaml | sudo k3s kubectl apply -f -
```

Para acrescentar um repo depois, repita o mesmo comando com todos os
`--from-file` (o `apply` substitui a secret inteira).

## 4. Conferir

```bash
sudo k3s kubectl get secret dse-preview-deploy-keys -n dse -o jsonpath='{.data}' | tr ',' '\n' | cut -d'"' -f2
```

Deve listar um item por repo. As chaves privadas locais podem ser apagadas
depois disso (`rm -rf ~/.dse-preview-keys`) — a secret é a fonte.

---

## O que o DSE faz a partir daí

Ao provisionar um preview, entre criar o namespace e aplicar o resto dos
manifestos, ele copia o item do repo para a secret `dse-preview-deploy-key`
daquele namespace (`materialize_deploy_key`, via kubectl — **fora** do conjunto
de manifestos, porque em modo gitops esse conjunto vira commit num repo git e
chave privada não vai para git). O pod monta em `/preview-keys/key` e usa
`GIT_SSH_COMMAND`.

**Repo sem item semeado**: o preview degrada com motivo nomeado
(`no deploy key seeded for <repo> … see deploy/vps/preview-deploy-keys.md`) em
vez do `could not read Username` opaco de 300s. Degradar nunca bloqueia a PR
(failure mode 9).

## Rotação

Repita os passos 1–3 e remova a key antiga no GitHub
(`gh repo deploy-key delete <id> -R <repo>`). Previews já rodando seguem com a
chave que montaram; os próximos pegam a nova.

---

## Honestidade operacional: o que estes testes NÃO cobrem

A camada determinística (`services/validation/tests/test_preview_recipes.py`)
cobre a forma dos manifestos, o roteamento por `kind`, o alvo do proxy e a
materialização da secret — tudo sem cluster. A camada de **integração real**
(`test_trigger_preview.py` contra o k3d `dse-preview` + garage + wse-gitserver)
fica **skipped** fora do ambiente de integração, e ela é justamente quem
provaria "namespace sobe, URL responde".

Portanto **a aceitação em produção é o que cobre esse vão**: uma PR de FE real
subindo preview com dados e uma PR de BE real respondendo health. Se a
aceitação falhar em algo que a camada determinística não pegou, isso é o
argumento para levantar o cluster de integração no CI — não para outra rodada
de escavação local.
