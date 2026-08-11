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
| 1 | Botão Details abrindo modal com o plano; Approve/Reject intactos | IMPLEMENTED-NOT-VERIFIED | `fa154cb`; 5 testes novos + 76 na suite do Slack. NÃO verificado no Slack real (sem crédito, nenhum item chega ao gate) |
| 2 | Comunicação com o usuário em inglês, com teste que impede regressão | VERIFIED | `c05dd9d`; `tests/test_user_facing_text_is_english.py` varre 9 módulos e passa; 10 textos traduzidos |
| 3 | Por que as PRs saíram sem preview, com evidência — e correção | VERIFIED (diagnóstico) / IMPLEMENTED-NOT-VERIFIED (fix) | `88fcfeb`; causa provada no ledger da PR #8; os 4 call sites alinhados e pinados por teste. Preview real não sobe sem crédito |
| 4 | lint + suites + PR revisada + deploy verificado | IN-PROGRESS | 1.608 testes verdes nos 7 grupos; PR #64 aberta com 3 revisores |

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
| A mensagem do gate NÃO contém o plano — só a frase do template | trace workflow→local_activities→adapter | `local_activities.py:793` |
| NADA no repositório renderizava `plan["steps"]` | grep no repo inteiro | — |
| Um botão novo cairia no fallthrough e APROVARIA o plano | `parse_slack_approval` devolve `approved` para qualquer id fora dos tokens de rejeição | `events.py:63-86` |
| Preview: `repo_bindings.deploys_preview = t` para os DOIS repos ativos | SELECT em produção | — |
| Preview da PR #8 recebeu `files_changed: []` com 4 `.java` na PR | audit_log de produção | `wi_a47c490a` |
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
| A mensagem do gate de plano dizia `(risk: —)` — prometia o risco e mostrava travessão, no texto que decide quem pode aprovar. Faltava `detail` no payload. | `fa154cb` |

## Open items

| Item | Assumption taken | Recommendation |
|------|------------------|----------------|
| Rodada real end-to-end | Impossível sem crédito | Medir quando o usuário recarregar |
