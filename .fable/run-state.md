# Run state — botão de detalhes do plano, tudo em inglês, e o porquê dos previews ausentes

Started: 2026-08-11 08:20 UTC  |  Branch: main  |  Last updated: 2026-08-11 08:20 UTC

Every agent on this run reads this file before starting and writes to it before finishing.
If the run is interrupted, this file — not the conversation — is what carries the job forward.

## Definition of Done

> DoD source: **derivada** do pedido do usuário (três tarefas numeradas, sem critérios de
> aceitação explícitos). Declarada aqui e seguida sem check-in, conforme instrução
> ("aja de maneira autônoma", "não deixe pergunta").

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | A mensagem de aprovação de plano no Slack tem um botão que abre um MODAL com o plano completo. Aprovar/Rejeitar continuam funcionando igual (one-shot, ack in-place). | TODO | |
| 2 | Toda comunicação com o USUÁRIO (Slack, corpo de PR, comentários de tracking, mensagens de erro que o humano lê) está em inglês. Um teste automatizado falha se voltar português. | TODO | |
| 3 | Resposta fundamentada, com evidência do ledger e do código, sobre por que PRs foram criadas sem preview — e correção aplicada se a causa for defeito nosso. | TODO | |
| 4 | (implícito) `make lint` verde + suites das áreas tocadas verdes com XML no disco; rc cortada, CI verde, deploy verificado pela imagem em uso. | TODO | |

## Regras do usuário para esta rodada

1. Avaliar o fluxo antes de agir.
2. Não pressupor nada — rastrear até a fonte real.
3. Rodar agentes de verificação depois de implementar.

## Decision ledger

| Date | Decision | Why | Lives in |
|------|----------|-----|----------|
| 2026-08-11 | Deploy da rc é parte do DoD e NÃO é hard stop nesta rodada | O usuário pediu explicitamente para subir e testar ao acordar; deploy é rotina neste repo e o rollback é re-pin da rc anterior (feito várias vezes nesta sessão) | `deploy/vps/values-vps-poc.yaml` |
| 2026-08-11 | NÃO recarregar crédito da Anthropic | Gastar dinheiro é hard stop do skill e decisão do usuário | — |

## Verified facts

| Fact | How it was traced | Anchor |
|------|-------------------|--------|
| A frota roda rc.78; todos os pods de pé | `kubectl get deploy -o jsonpath` + `helm list` em 2026-08-11 03:31 | helm rev 92 |
| O crédito da Anthropic AINDA está esgotado | log do model-gateway em 07:58 + `repo_routing_decided {"repos": [], "reason": "router unavailable: HTTPStatusError"}` | `wi_8d729b92` |
| Fila do Temporal vazia (0 Running) | `temporal workflow count --query "ExecutionStatus='Running'"` | 2026-08-11 |

## Interface contracts

| With | Shape / contract | Agreed on | Status |
|------|------------------|-----------|--------|
| — | — | — | — |

## Drive-by fixes

| What was broken | Commit |
|-----------------|--------|
| | |

## Open items

| Item | Assumption taken | Recommendation |
|------|------------------|----------------|
| Rodada real end-to-end | Impossível sem crédito | Medir quando o usuário recarregar |
