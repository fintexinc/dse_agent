# Review de backlog em dois chapéus — 09/08/2026

> **NOTA DE 2026-08-10 — leia antes do resto.** Este documento é um RETRATO do
> dia 09/08 e fica como registro; não foi reescrito. No dia seguinte uma decisão
> de operador removeu do produto **o parque de spec (`spec_conflict`), o
> reauthor e toda a noção de posse de teste**: o DSE passou a poder alterar
> qualquer teste, com a supervisão no diff da PR, e a única saída de laço
> travado passou a ser `escalated`.
>
> Portanto, no texto abaixo: o verbete "Parque / portas" do glossário, o "Túnel
> do reauthor", os itens **A6**, **B1**, **B3**, **B4**, **B8**, **E3**, **E4**,
> **E5**, **E12** e o "bloco do parque" citado como critério da Fase 1
> descrevem um mecanismo **que não existe mais**. Os vereditos continuam
> válidos como julgamento da época — e o desfecho deles, aliás, foi o inverso
> do previsto: o custo do parque não se pagou, e o mecanismo inteiro saiu.

**Objeto:** os ~40 itens de `docs/BACKLOG-DSE.md` (projetos A–G), revisados em dois passes independentes — Tech (registro, sem opinião) e Produto (julgamento, em cima do registro) — e fechados com uma síntese.

**Fontes e estado do mundo no momento do review:**
- Código: branch `main`, HEAD `f14962b` (RBAC do preview), working tree limpo exceto `docs/FLUXO-CODIGO-ATE-VALIDACAO.md`. Toda linha citada foi conferida **nos arquivos de hoje** — várias linhas do backlog derivaram e aparecem corrigidas aqui.
- Ledger: Postgres de **produção** (VPS, `dse-dse-postgres-0`), consultado em 2026-08-09 (somente SELECT + `kubectl get`). Snapshot: 79 work items (38 `escalated`, 26 `failed`, 14 `review_ready`, 1 `blocked`, 0 `implementing`), `model_call_ledger` com US$ 233,87 all-time (92% na última semana).
- Grounding: `GROUNDING-REVIEW.md` (09/08) e a memória do projeto.

**Convenção de honestidade (vale para o documento inteiro):** cada afirmação carrega sua categoria — **[ledger]** = medido em produção, **[código]** = verificado em arquivo:linha hoje, **[histórico]** = medição registrada na memória/autópsias da semana, **[não medido]** = nunca observado, só previsto. Dado do ledger > inferência > opinião. Onde a evidência não existe, está escrito "não medido". O documento passou por uma rodada de verificação adversarial (3 revisores independentes: fatos, vereditos, completude) antes da publicação; os vereditos que caíram na verificação foram corrigidos, não maquiados — E2 é o exemplo (o gatilho que o adiava já tinha disparado).

**Glossário mínimo para quem não viveu a semana:**
- **Casos 1/2/3** — os três casos de teste do POC: 1 = FE Angular (badge/dashboard, PR #15), 2 = BE Java (PR #4), 3 = cross-repo Slack ("retire payout levels" → par PR #5 BE + PR #17 FE).
- **L1/L2** — L1: 8 gates determinísticos pós-Coder (lint, typecheck, test, build, sast, secret_scan, diff_budget, forbidden_paths); L2: revisão de código automatizada antes da PR.
- **Parque / portas** — estados onde o item espera decisão humana durável. Porta 1: spec do cliente quebrada pelo diff; Porta 5: spec própria do Tester sem veredito (repara in-place); Beco 1: spec própria com veredito + exaustão. Vereditos de parque hoje: `retry`, `reauthor`, (fallback) escalate.
- **Pinça** — deadlock medido onde satisfazer a spec quebra outro gate e vice-versa (ex.: spec afirma valor que o typecheck proíbe).
- **Túnel do reauthor** — precedente canônico de custo: 5 rcs (48–52) para um mecanismo cujo caso de uso `discard` resolveria por ~5% do custo.
- **H1–H6** — hipóteses do diagnóstico original (docs/FLUXO-CODIGO-ATE-VALIDACAO.md): H1 executor do Coder, H2 falha-rápida no L1, H3 captura de aprendizado, H4 exemplo por proximidade, H5 infra fora do laço de retry, H6 orçamentos separados.
- **rc.N** — release candidate `v0.1.0-rc.N`; **CAN** — `continue_as_new` do Temporal (o workflow renasce só com o input); **wi_…** — work item id; **#NN** — issue/PR nos repos do testbed (`bmo-fee-calculator-{fe,be}-dse`).
- **Fases 1–5** — a priorização do operador de 09/08, tabela no topo de `docs/BACKLOG-DSE.md` (1: canal; 1.5: preview; 2: apresentação; 3: multi-repo; 4: aprendizado; 5: robustez por oportunidade).

**Duas descobertas transversais deste review** (não estavam em nenhum item):
1. **[código, reproduzido hoje]** A suite `services/console-projector` está **vermelha na main**: `test_every_fase1_status_has_console_mapping` falha com `KeyError: 'spec_conflict'` (`mappers.py:104`) — o status de parque nunca entrou no `STATUS_MAP` do console. Afeta B1/B4/B5.
2. **[ledger]** A anomalia de contabilidade (E11) não é cosmética: 102 linhas com `cost_usd > 0.5` e `tokens_in < 1000` somam **US$ 203,46 — 87% do gasto all-time**. Os `cost_usd` (vindos do header do gateway / do resultado do SDK) são críveis; os `tokens_in` não são (tokens de cache não são lidos em nenhum write path — grep `cache_read` = 0 no repo). Toda régua de custo deste documento usa `cost_usd`, não tokens.

---

## PASSE 1 — Chapéu Tech (estado, sem opinião)

### Números do ledger que vários itens usam [ledger]

| Medição | Valor |
|---|---|
| Eventos de topo | `ci_pending`=6507, `signal_duplicate_ignored`=1506, `steering_authorized`=1491, `coder_turn_completed`=348 (=174 turnos; cada turno emite 2 eventos), `l1_pipeline_run`=114, `l1_failed_retrying`=60 |
| Mortes | `coder_retry_cap_exhausted`=9, `tester_retry_cap_exhausted`=10, `work_item_escalated_stranded`=31 (todos `system:stranded-sweep`, idle 6h) |
| Mortes × infra (E2/F1) | Dos 9 mortos por cap, **5 tiveram ≥1 rodada de L1 com gate ERROR** (sast/lint); o extremo: **wi_8c46e17e queimou o cap inteiro em 4 rodadas cujo único não-PASS era `sast: ERROR`** (24/07, era fintex-wallet) — cap consumido 100% por infra, re-verificado por query direta neste review |
| Checkpoints/sandbox | `sandbox_checkpointed`=211 (commits de fase rotineiros), `sandbox_rebuilt`=4 (a recriação existe e já rodou) |
| Aprendizado | `skill_episode` tem **1 linha all-time** (source=clarification, occurrence_n=1); zero ci_repair, zero review_feedback — em 79 itens e 174 turnos de Coder (re-verificado por query direta) |
| Custos históricos de referência **[histórico, memória do projeto]** | Runs Java 100% infra: wi_dc571c08 US$ 6,23 + wi_866b96ce US$ 4,50 = US$ 10,73 (≈ a régua de ~US$ 4-5 por camada descoberta-por-run do grounding §4, 2 camadas cobertas); MockBean: wi_5620d2c1 morto no cap a US$ 4,14; item FE típico no testbed Angular: ~25-40 min |
| Parques | `spec_conflict_detected`=13, `spec_conflict_resolved`=10 (verdicts: 5 retry, 5 reauthor), `spec_conflict_deferred_to_coder`=1 |
| Steering | `steering_rejected_unauthorized`=**22** (o backlog dizia 19): 15 bot GitHub (`dse-fintex[bot]`, kind=clarification_answer; +1 hoje), 5 `repo_select` (24–25/jul), 2 Slack novas (hoje 10:22–10:23, usr_2756c382…) |
| Aprovações | `plan_auto_approved`=80, `awaiting_plan_approval`=2, `plan_approved`=1. Cliques de Approve no Slack que chegaram como signal: **2 — ambos roteados ao irmão errado e recusados** (`dispatch_declined_unexpected_status`). A única aprovação efetiva (wi_6733a6fb, 10:31) chegou por fora do ingest-gateway (console/CLI; `console_login` 8 min antes) |
| Destravamento de parques | Os 10 `spec_conflict_resolved` têm actor `system:orchestrator` com o humano em `details.actor`: 7× a string literal `'usr_saraiva'` (signal manual via CLI) e 3× o principal real. **Zero parques destravados por botão.** |
| Rodadas até PR | 20 itens com PR; **18 de 20 convergiram em ≤2 rodadas de Coder**. Exceções: PR #17 (FE do caso 3, 5 rodadas) e PR #15 (caso 1, 3 rodadas / 7 L1 runs) |
| Caso 3 | BE PR #5: US$ 2,30, 2 rodadas. FE PR #17: US$ 15,78, 5 rodadas — as 3 primeiras (cegas, no sintoma) somam **US$ 11,12**. A "rodada dirigida" da rc.54 **não foi uma rodada do FE**: virou um work item novo no BE (wi_8a7036, PR #7, instrução "Expose retire as PUT /payout-levels/{id}/retire…"), **US$ 1,85, 2 rodadas, 12 min** — razão ~6× vs as rodadas cegas |
| L1 por repo | Java: 16–84s por run. Angular: FAIL avg 514–762s (max 1558s), gate `test` domina (305–852s). Razão ~7–14× |
| Preview | `preview_triggered`=24 (3 `human_request`), `preview_created`=8 — **todos kind=ui do fintex-wallet**. `preview_degraded`=12: 7× timeout do `kubectl wait` no deployment, 2× RBAC Forbidden, 2× read-only FS, 1× concurrency cap (3/3). **Zero preview `deployable` jamais ficou Available** |
| Pods agora | 4 pods Running em `dse-sandboxes`, todos de itens `review_ready` (o mais velho há 4h40m). Órfãos no sentido estrito: 0 |

---

### Projeto A — Canal & Identidade

**A1 — `bot_ts` herdado no fan-out**
- Estado **[código]**: o INSERT do irmão copia `source_ref` inteiro ([local_activities.py:1258-1272](services/orchestrator/src/dse_orchestrator/local_activities.py:1258), `SELECT %s, tenant_id, source, source_ref, …` em :1265, sem subtração; o comentário em :1223-1225 declara o compartilhamento como intencional). A ordem que arma o bug: o status comment é postado ANTES do fan-out ([workflows.py:1775-1783](services/orchestrator/src/dse_orchestrator/workflows.py:1775), fan-out em :1784-1796); o post appenda `bot_ts` no `source_ref` do primário ([adapter-slack app.py:576-590](services/adapter-slack/adapter_slack/app.py:576)). A correlação do clique é por containment `source_ref @> {channel, bot_ts:[ts]}` com `ORDER BY created_at DESC LIMIT 1` ([correlate.py:82-90](services/ingest-gateway/ingest_gateway/correlate.py:82)) — com bot_ts herdado, primário e irmão casam e o mais novo (irmão) vence. O fix desenhado (`source_ref - 'bot_ts'`) não existe.
- Ledger **[ledger]**: 3 grupos na base; o único grupo Slack com bot_ts (o caso 3, hoje) tem o primeiro elemento do array do irmão **herdado do primário** — 1 de 1 grupo Slack afetado. Efeito medido hoje: os 2 cliques de Approve correlacionaram com o irmão FE e foram recusados (`unexpected_status`); a aprovação real foi por console/CLI.
- Proposta e custo: 1 linha de SQL em `local_activities.py:1265` + teste de regressão (A4). Risco baixo — a idempotência do fan-out (`ON CONFLICT`, :1269) não muda; a correlação por `thread_ts` compartilhado permanece. Suites: `services/orchestrator` (test_multi_repo_routing.py), `services/adapter-slack` (test_bot_thread_correlation.py), `services/ingest-gateway` (test_correlate.py).
- Dependências/conflitos: A4 é o vermelho deste fix. A6 e qualquer veredito por botão em thread com irmãos dependem de A1. A memória registra C1 "após a rc do A1".

**A2 — steering_rejected_unauthorized (agora 22, não 19)**
- Estado **[código]**: o webhook do GitHub **não filtra comment do próprio bot** — `issue_comment created` vai direto para `_ingest_issue_comment` ([adapter-github app.py:406-407](services/adapter-github/adapter_github/app.py:406)); o filtro `_is_bot_comment` **já existe** (app.py:549-568) mas só é aplicado no reconciler (:588). Slack ignora eventos sem user (app.py:302-304); Jira filtra por identidade (`_is_dse_authored`, ingest.py:432-462). Sub-caso (b): no caminho `issues assigned/labeled` o requester gravado é `payload['sender']['login']` — quem etiquetou, não o autor (app.py:387-388, events.py:31-48); o autor respondendo resolve para outro principal e é rejeitado.
- Ledger **[ledger]**: 22 total — 15 bot GitHub (cresceu +1 hoje, wi_8a7036e7), 5 `repo_select` de julho, 2 Slack novas de hoje (um humano, usr_2756c382…, tentando responder clarification na janela do caso 3 e sendo recusado **em silêncio**).
- Proposta e custo: aplicar `_is_bot_comment` no caminho webhook — ~5-15 linhas + testes (o comentário em app.py:551-556 diz que filtro por autor é seguro no GitHub). O sub-caso (b) exige decisão de requester (sender vs issue.user) — pequeno em linhas, decisão de produto embutida. Suites: `services/adapter-github`, `services/ingest-gateway`, `packages/dse_identity`.
- Dependências/conflitos: sub-caso (b) sobrepõe com A5. O filtro de bot é pré-condição prática de A3 no GitHub (orientação postada pelo bot re-ingere sem ele).

**A3 — resposta in-channel de GitHub/Jira na recusa de pedido não-task** (o "fluxo F2" do canal — não confundir com o **item** F2 do backlog, que é a migração de seletores)
- Estado **[código]**: Slack orienta (handler `NonTaskAdmissionRefused`, [adapter-slack app.py:230-261](services/adapter-slack/adapter_slack/app.py:230), commit 77e428d). GitHub: handler em app.py:194-210 — audita e devolve 200 mudo. Jira: dois sites (ingest.py:101-114 e :371-384), idem. Infra de postagem existe nos dois adapters (comment_store em ambos).
- Ledger: frequência de humanos batendo nessa parede em GitHub/Jira — **não medido** (as recusas non-task auditadas não foram contadas por canal neste passe).
- Proposta e custo: ~20-40 linhas por adapter + testes. Riscos nomeados: no Jira, comment avulso fora do `comment_state` pode realimentar o poller (o guard `_is_dse_authored` só reconhece o writer registrado); no GitHub, o comment do bot volta pelo webhook enquanto A2 não fechar. Suites: `services/adapter-github`, `services/adapter-jira`, `services/adapter-slack`.
- Dependências/conflitos: interage com A2 (filtro de bot primeiro); sobrepõe com A6 (mesmos adapters, mesma ampliação de conversa).

**A4 — Botão de Approve nunca re-testado**
- Estado **[código]**: o caminho do clique existe ponta a ponta e **tem testes** (adapter-slack test_inbound_flow.py:189/228/269; test_bot_thread_correlation.py:161-218) — mas o teste de irmãos insere o irmão ANTES do post do prompt, então nunca exercita a herança do bot_ts (a ordem de produção é a inversa). Com bot_ts herdado: se o irmão não está em `awaiting_plan_approval`, o dispatcher **declina e consome** o evento ([dispatcher.py:186-192](services/ingest-gateway/ingest_gateway/dispatcher.py:186)) — o Approve evapora; se o irmão também estiver no gate, aprova o plano do item errado.
- Ledger **[ledger]**: placar all-time do botão: 2 cliques chegaram como signal, **0 surtiram efeito** (ambos hoje, declinados). 1 aprovação efetiva por via não-Slack.
- Proposta e custo: teste que reproduza a ordem de produção (~10-30 linhas), zero linha de produção própria — é o vermelho do A1.
- Dependências/conflitos: par indissociável do A1; A6 depende deste caminho.

**A5 — Identidade cross-canal**
- Estado **[código]**: `resolve_principal` cria 1 principal por (platform, platform_user_id), sem link cross-canal ([resolve.py:22-58](packages/dse_identity/dse_identity/resolve.py:22); docstring: "NO SSO/SCIM — ADR-22, Phase 2"; comportamento fixado em test_resolve.py:14). O SSO/console cria um **quarto** principal (sso.py:44-85). Nenhum fluxo de merge/link existe; `identity_links` só recebe self-registration. Todas as autorizações comparam string de principal (correlate.py:112-116, steering.py:171-200).
- Ledger **[ledger]**: os 5 rejects `repo_select` de julho são deste mecanismo. Curiosidade que dimensiona: `steering_authorized`=1491, sendo 1479 clarification_answer de um único principal Slack — e `signal_duplicate_ignored`=1506 na mesma ordem de grandeza (o gateway deduplica re-ingestões do poller).
- Proposta e custo: o mecanismo de match não está especificado no backlog; superfície real = resolve.py + migration + decisão sobre dados históricos (principals antigos já gravados em requester/allowlist/approvers/audit). **Risco alto de segurança**: um link errado transfere autorização de aprovação entre canais. Suites: `packages/dse_identity`, `services/ingest-gateway`, `services/platform`, adapters.
- Dependências/conflitos: resolve o sub-caso (b) do A2; A6 depende parcialmente. Alternativa de POC existente no schema: `tenant_steering_allowlist` aceita N principais por tenant (PK (tenant_id, principal_id), migrations/0002) — cobre por config os kinds gateados por `is_authorized_to_steer` (clarification_answer/steering/review_comment/repo_select). **Ressalva verificada [código]: NÃO cobre approval** — o caminho de plan approval não checa principal em ponto nenhum: `approval` não está nos kinds gateados (correlate.py:54-58, docstring "has its OWN gate"), o dispatcher roteia por status sem checar actor, e o handler do workflow aceita qualquer actor (workflows.py:3098-3106 — `decided_by` é só registro; `designated_approvers` serve para resolver quem notificar, não para autorizar). **Achado de segurança colateral deste review: hoje qualquer membro do canal Slack que clique em Approve aprova o plano.**

**A6 — Veredito pelo canal**
- Estado **[código]**: o que já existe — botões de plan approval no Slack (events.py:63-86), transição de coluna no Jira (ingest.py:553-592); GitHub não tem plan approval in-channel. O comment do veredito viaja no fix_context desde a rc.54 (commit c8ebcdc; `_last_verdict_comment` em workflows.py:1329, entregue em 4 sites). O que **não** existe: nenhum produtor do signal `spec_conflict_resolution` fora de testes (grep: só o handler, workflows.py:550-558); o dispatcher não roteia esse signal; um comment num item parqueado vira `clarification_answer`, que o parque **não consome**; um clique de approval em item parqueado é **declinado e consumido**.
- Ledger **[ledger]**: 10 de 10 parques destravados por signal manual (7 com actor literal `'usr_saraiva'` digitado). O wi_53c820 (caso 1) consumiu 5 resolves e ainda assim foi varrido pelo stranded-sweep no meio (parque + 6h de silêncio = escalated).
- Proposta e custo: (a) rota no dispatcher `status=spec_conflict → SIGNAL_SPEC_CONFLICT_RESOLUTION` (~20-40 linhas); (b) botões/semântica por canal (~50-150 linhas por canal + testes); (c) gate de autorização do veredito. Risco: o dispatcher é o caminho de todos os sinais; `clarification_answer` roteado sem status-check (dispatcher.py:167-175) é uma interação a preservar. Suites: `services/ingest-gateway`, `services/orchestrator`, adapters, `tests`.
- Dependências/conflitos: depende de A1/A4; parcialmente de A5 (ou da alternativa de allowlist); sobrepõe com A3 e com B4 (B4 é a pergunta, A6 é a resposta — mesmo trabalho).

### Projeto B — Apresentação & UX

**B1 — Etapas simplificadas por canal**
- Estado **[código]**: o corpo das mensagens é único para os 3 canais e já é humanizado para 12 status (`_STATUS_BODIES`, [local_activities.py:790-823](services/orchestrator/src/dse_orchestrator/local_activities.py:790)). O vazamento cru tem dois mecanismos: (1) status sem entrada cai no fallback `"DSE status: {status}"` que **descarta o {detail}** (:864-865) — hoje alcançável por `spec_conflict`; (2) as entradas failed/escalated interpolam `{detail}` com strings técnicas (`tester_failed_after_retry_cap`, `coder_made_no_change: …`). Acoplamento de risco: o Slack chaveia os botões pela string exata do status (app.py:546-548).
- Ledger: reclamação direta de usuário — não medido (single-user).
- Proposta e custo: colapsar para 5 etapas = ~40-80 linhas em `post_tracking_comment` + tradução dos reasons + possivelmente console. Mudança na activity é replay-safe.
- Dependências/conflitos: sobrepõe com B3/B4 (mesma tabela), B6 (mesmo body), B7 ("❌ Não consegui" pressupõe que o caminho de exaustão poste algo — um deles não posta).

**B2 — Status em thread com update in-place**
- Estado **[código]**: **já existe nos três canais**, todos via o mesmo `MutableCommentWriter` ([mutable_comment.py:34-57](packages/contracts/dse_contracts/mutable_comment.py:34)): GitHub `upsert_status_comment` (app.py:502-526), Slack `chat_update` como reply na thread (app.py:511-598), Jira (app.py:220-238). Ref persistido em `comment_state` por (work_item_id, surface).
- Divergência com o backlog: o item pede verificação como se pudesse faltar — não falta.
- Proposta e custo: zero.
- Dependências/conflitos: é a base que B1/B3/B4/B6/B7 pressupõem.

**B3 — Erros em linguagem humana (dossiê do parque)**
- Estado **[código]**: o dossiê existe e é rico — `_park_spec_conflict` extrai até 12 pares Expected/Received, audita specs/assertions/diff_files e monta um comment completo (workflows.py:1267-1311). O que chega ao canal: **nada disso** — o comment viaja como `detail` de `_post_status_comment("spec_conflict", …)` e o fallback o descarta; o canal recebe literalmente `"DSE status: spec_conflict"`.
- Ledger **[ledger]**: 13 parques detectados — 13 dossiês montados e descartados na borda do canal.
- Proposta e custo: 1 entrada `"spec_conflict"` em `_STATUS_BODIES` com `{detail}` — **~3-10 linhas**; zero mudança de replay. Suites: `services/orchestrator` (test_spec_conflict.py, test_tester_spec_exhaustion.py).
- Dependências/conflitos: sobrepõe com B1; pré-requisito prático de B4.

**B4 — Parque = pergunta, não status**
- Estado **[código]**: pior que o backlog descreve. Além do texto: (a) **não existe rota de resposta** a partir do canal (dispatcher sem rota para `spec_conflict`; signal sem emissor de produção; approval em item parqueado é declinado); (b) a suite `services/console-projector` está **vermelha hoje** nesse exato ponto (`KeyError: 'spec_conflict'`, reproduzido neste review) — o console nem atualiza a linha do item parqueado. `needs_clarification` já é pergunta bem-formada (workflows.py:1849-1875); `awaiting_plan_approval` posta sem detail — o body sai sem o plano e sem o risco (workflows.py:3030-3039).
- Ledger **[ledger]**: os 10 destravamentos foram todos por CLI (ver A6).
- Proposta e custo: body-pergunta (com B3), rota no dispatcher (~15-30 linhas), botões Slack (padrão `approval_blocks` existe), 1 linha no `STATUS_MAP` do console (desavermelha a suite). Risco: rota nova de sinal é superfície de segurança.
- Dependências/conflitos: mesmo trabalho que A6 (lado pergunta / lado resposta); toca a área de B5.

**B5 — "Last event" do painel mistura ciclo de vida com sinais rejeitados**
- Estado **[código]**: confirmado — `_project_audit` grava `last_event` de **toda** linha do audit com work_item_id, sem filtro ([projector.py:197-231](services/console-projector/console_projector/projector.py:197)); `dispatch_declined_*`, `signal_duplicate_ignored`, `steering_rejected_unauthorized` viram "note" com a action crua (mappers.py:107-115).
- Ledger **[ledger]**: matéria-prima do ruído: 1506 `signal_duplicate_ignored`, 22 rejeições de steering, 3 declines. Nos 3 itens mais recentes, o evento útil (`awaiting_human_review`) chega colado em 4 eventos de ruído do mesmo segundo.
- Proposta e custo: classificar actions de rejeição no mapper (~10 linhas) + filtro no loop do last_event (~5-15 linhas); timeline mantém tudo. Risco baixo (projeção idempotente, re-projetável). Suite dedicada existe (test_projector.py).
- Dependências/conflitos: independente; a suite vermelha de B4 mascara verde/vermelho aqui até ser corrigida.

**B6 — Irmãos na mesma thread sem se identificar**
- Estado **[código]**: cada irmão tem sua própria mensagem (comment_state por work_item_id) e **nenhum body carrega repo/id** — a única menção aos repos é o post pré-fan-out "Routing to N repositories". Divergência relevante: o comentário em workflows.py:1774-1778 afirma que a superfície "never shows two messages for one request" — o mecanismo real produz uma mensagem POR IRMÃO; o comentário não bate com o código.
- Ledger: screenshot do caso 3 citado no backlog; frequência = todo fan-out Slack (3 grupos até hoje) **[ledger]**.
- Proposta e custo: variante mínima — prefixar o body com o repo (~5-15 linhas em `post_tracking_comment`; o repo já está carregado na linha 856). Variante "uma mensagem por grupo" = redesenho de comment_state + correlação — superfície muito maior.
- Dependências/conflitos: sobrepõe com B1; a variante grande competiria com a correlação por bot_ts existente.

**B7 — Ninguém é avisado da morte**
- Estado **[código]**: medido caminho a caminho — (1) `coder_retry_cap_exhausted` (workflows.py:2735-2757): seta `failed` no banco e **retorna sem postar** — único terminal do workflow sem post; a superfície congela no último body ("implementing"). (2) `tester_retry_cap_exhausted` → posta "failed". (3) kill de infra do Tester → posta "escalated". (4) cancel de operador → posta "failed". (5) morte externa (terminate): nada roda; o stranded-sweep que eventualmente escala **também não posta no canal** (stranded.py:348-359).
- Ledger **[ledger]**: 9 `coder_retry_cap_exhausted` + 31 escalações do sweep — ~40 mortes/escalações sem aviso no canal.
- Proposta e custo: caminho (1): `_post_status_comment("failed", …)` antes do return — ~2-5 linhas + patch marker de replay (disciplina do RUNBOOK §3). Caminho (5): notificação a partir do sweep (~20-40 linhas, chamada HTTP ao adapter). Suites: test_iteration_caps_debounce.py, test_stranded_sweep.py.
- Dependências/conflitos: divergência com o backlog: no caminho (1) o **banco** vira `failed` — o que congela é a superfície. Sobrepõe com B8 (morte externa) e B1 (texto).

**B8 — `terminate` não atualiza `work_items.status`**
- Estado **[código]**: listener direto de terminate não existe (grep: nenhum `.terminate(` fora de testes; nenhum describe/sync de status). **Mas** o stranded-sweep existe, está wired (CronJob) e converge implementing→escalated com ~6h de latência (idle 21600s). Lacunas reais: latência 6h; sem notificação de canal; **cegueira permanente** para terminate em status de espera humana (needs_clarification, awaiting_plan_approval, spec_conflict, review_ready… ficam fora do sweep para sempre, stranded.py:85-100); e o sweep **não implementa o probe de Temporal que o próprio docstring declara obrigatório** antes de escalar (stranded.py:34-43 vs stranded_sweep.py:58-80).
- Ledger **[ledger]**: hoje 0 itens presos em implementing — o sweep normaliza (31 escalações, inclusive 4 varridas de `spec_conflict` — o sweep **atropelou parques humanos legítimos**, caso wi_53c820). Nota de atualização: o atropelo foi corrigido **hoje** (commit 277b937, spec_conflict entrou em STRANDED_HUMAN_WAIT_STATUSES); as 4 varreduras são pré-fix. Efeito colateral: agora **todo** status de espera humana é um buraco permanente para workflows mortos — o probe do Temporal deixou de ser melhoria e virou a única linha de defesa dessa classe.
- Proposta e custo: (a) probe do Temporal no sweep (~30-60 linhas) — resolve a ambiguidade e a escalação indevida de parque; (b) wrapper de terminate no queue board (~30 linhas) — cobre só terminates nossos. Risco: escrita terminal fora do writer canônico (padrão de state_version já existe em stranded.py:281-287).
- Dependências/conflitos: sobrepõe com B7 (notificação); a escalação-de-parque interage com A6/B4 (parque destravável pelo canal reduziria o atropelo).

### Projeto C — Multi-repo

**C1 — Contrato de interface entre irmãos**
- Estado **[código]**: o fan-out acontece no **intake**, antes de qualquer Planner (workflows.py:1755-1796); o Planner do primário só roda na fase de implementação (:2096, :2920). Os irmãos recebem cópia **verbatim** do payload do primário (local_activities.py:1273-1286). Não existe artefato de interface cross-repo (RunPlannerTurnInput sem campo; grep = 0). **A forma escrita no backlog ("Planner do primário escreve o contrato ANTES do fan-out") pressupõe um ponto do workflow que não existe** — exigiria um turno de Planner novo pré-fan-out, em região de replay do intake.
- Ledger **[ledger]**: o vão custou US$ 11,12 (3 rodadas cegas do FE) + retrabalho; a versão dirigida do mesmo contrato (item BE novo com a frase exata) custou US$ 1,85 e 12 min — **razão ~6×**. Nota de medição: a "rodada FE dirigida da rc.54" citada no backlog não existe como rodada do FE — o direcionamento materializou como work item novo no BE.
- Proposta e custo: como escrito, ~4-5 arquivos centrais (workflows intake + fan-out + contracts + prompt do Planner) — centenas de linhas, replay-sensível. Alternativa estrutural com a mesma dor: **segurar o despacho do irmão até o plano do primário existir** e entregar o PlanArtifact do primário como grounding. Superfície verificada da alternativa **[código]**: o irmão é de fato despachado pelo dispatcher (start_workflow do task_request inserido no fan-out, dispatcher.py:202-209), o drain já faz JOIN em work_items e o plano do primário está em `work_items.plan` — mas não é dispatcher-only: o hold exige semântica nova no outbox (hoje só SIGNAL_FAILED deixa linha unprocessed, :282-285; uma linha retida é re-varrida a cada drain e ocupa slot do batch), e a **entrega** do grounding exige mudar o load path do workflow do irmão + input/prompt do Planner (orchestrator + sandbox-runtime). Ainda assim, fração do custo do estágio pré-fan-out e fora da região de replay do intake. Suites: `services/orchestrator`, `packages/contracts`, `services/sandbox-runtime`, `services/ingest-gateway`.
- Dependências/conflitos: compete com C3 (ordem BE→FE resolve a mesma dor por outro caminho — e as duas soluções são mutuamente redundantes); sobrepõe com C5 (ambos estruturam o artefato do Planner). A memória registra C1 após a rc do A1.

**C2 — Link entre as PRs do grupo**
- Estado **[código]**: `grep ChangeGroup` = 0 (confere). Divergência: "group_id existe só para renderização" — **não existe nenhum leitor de renderização**; o único leitor real é o degrau 2 do preview (proxy FE→BE irmão, db.py:592-621 + argocd.py:857-860). A intenção de renderização vive só num docstring.
- Ledger **[ledger]**: 3 grupos; o caso 3 produziu PR #5 + PR #17 sem nenhum link entre si — e a instrução operacional vigente é "não mergear o par como está".
- Proposta e custo: corpo de PR com link ao grupo — ~2-3 arquivos, dezenas de linhas; restrição estrutural: sem ordem entre irmãos, a primeira PR abre antes da segunda existir (link simétrico exige segundo passe).
- Dependências/conflitos: pressupõe group_id (existe); sobrepõe com C3 e C1.

**C3 — Ordem/atomicidade opcional (BE→FE)**
- Estado **[código]**: nenhum mecanismo de ordem existe (dispatcher drena por `ORDER BY ie.id`, sem noção de grupo; nenhuma coluna de dependência). Único acoplamento entre irmãos: o proxy oportunista do preview.
- Ledger: necessidade — não medido (o backlog mesmo diz "decidir após C1").
- Proposta e custo: migração + ponto de espera (dispatcher ou workflow do dependente); risco médio-alto (região de replay ou loop de despacho global).
- Dependências/conflitos: compete com C1 (mesma dor); habilitaria C2 determinístico e melhoraria o degrau 2 do preview (hoje o irmão vivo é acaso).

**C4 — Múltiplos repos por canal / binding real**
- Estado **[código]**: **as duas alegações do backlog estão erradas hoje.** (a) `repo_bindings.base_branch` é lido em 3 lugares: repo_resolver.py:70 e :97 (Rungs 2-4) e adapter-slack app.py:130; o fan-out propaga o base_branch resolvido aos irmãos. (b) O orchestrator usa repo_bindings também via `TENANT_REPO_CATALOGUE_SQL` no router (local_activities.py:1091), além do gate `deploys_preview` (:530-535). O que de fato não existe: N repos por binding_value (PK impede, migrations/0023:26) e escrita de produção em repo_bindings (só seed manual).
- Ledger: dor de roteamento por binding único — não medido (o router por union já cobre multi-repo por tenant; caso 3 roteou por ele).
- Proposta e custo: migração de PK + resolver + picker (~4 arquivos + migração). Risco: o resolver decide ANTES do router e sem linha de auditoria — mudar precedência muda silenciosamente para onde tarefas vão.
- Dependências/conflitos: o union `repo_bindings ∪ repo_profiles` (invariante do CLAUDE.md) já responde "quais repos o tenant tem".

**C5 — Porta 4: `expected_behavior_changes` no plano**
- Estado **[código]**: nada existe (grep = 0). O fluxo de plan approval existe e é o veículo natural (cross_repo já força risk high → aprovação humana, policy.py:56-57). Registro factual importante: a porta 4 **inverte uma regra hoje explícita em código** — "spec de cliente não é editada nem por ordem humana" (workflows.py:1338-1341) — e relaxa o revert determinístico das edições de teste do Coder (workspace_hygiene.py:109; invariante "tests belong to the Tester").
- Ledger **[ledger]**: casos onde a porta 4 teria agido: **0 até hoje** — no caso 3, a spec do cliente estava CERTA e segurou um bug real três vezes (o oposto do cenário da porta 4).
- Proposta e custo: ~6 arquivos (contrato, prompt, gate, detector, revert, adapter); risco alto — relaxa guard de segurança e toca replay.
- Dependências/conflitos: sobrepõe com C1 (artefato do Planner); conflita com as portas 1/exaustão (specs autorizadas teriam de ser excluídas da detecção).

### Projeto D — Aprendizado universal

**D1 — H4: exemplo por proximidade com o diff**
- Estado **[código]**: no Pod, `sorted(existing)[:3]` está hoje em [activities.py:2194](services/sandbox-runtime/sandbox_runtime/activities.py:2194) (a linha 2160 do backlog derivou) — alfabético; no caminho Docker/local a seleção é ordem de `os.walk` (nem alfabética, :2074-2078). O diff do Coder está no mesmo contexto mas é lido **depois** da seleção do exemplo (:2205 vs :2187-2198). Parser diff→arquivos já existe no mesmo arquivo (`_changed_files_from_diff`, :3840-3857).
- Ledger: quantas specs ruins nasceram de exemplo ruim — não medido.
- Proposta e custo: ~30-60 linhas em 3 funções de um único arquivo (reordenar leitura do diff + ranking por proximidade + espelho no caminho local). Risco: muda o prompt de autoria; os dois caminhos devem mostrar o mesmo (comentário :2203-2204). Suites: `services/sandbox-runtime` (test_tester_context_read.py e vizinhos).
- Dependências/conflitos: mesma função onde D3 mapeia skills do Tester — edições concorrentes.

**D2 — Onboarding automático (sonda como estágio)**
- Estado **[código]**: nenhum estágio automatizado existe. O que há: `repo_profiles` com seed manual em migration; `propose_agents_md` (repo_doc.py) — deriva fatos da árvore e abre PR de AGENTS.md, mas o único invocador é o script manual `scripts/propose_agents_md.py`; a "sonda" existe só como episódio manual citado em docstrings de teste.
- Ledger **[ledger, histórico]**: a sonda manual pagou 2× (grounding §4: ~US$ 0 vs ~US$ 4-5/camada por descoberta-por-run; 4 camadas de infra + 10 convenções de spec descobertas de uma vez).
- Proposta e custo: como estágio: novo gatilho + workflow/modo + persistência de aprendizado (CHECK de `skill_episode.source` exige migration) — centenas de linhas em ≥3 serviços; restrição do CLAUDE.md: item iniciado por `temporal workflow start` não cria linha em work_items.
- Dependências/conflitos: o produto do estágio pressupõe a captura de D3; escrita automática de AGENTS.md compete com a decisão registrada em repo_doc.py:1-29 (PR revisado por humano, três razões nomeadas).

**D3 — Captura de aprendizado (H3 residual)**
- Estado **[código]**: divergências relevantes com o backlog. (1) `fix_context` **é usado** — vai ao próximo turno do Coder (workflows.py:936-938) e ao journal `run_episode`, que o Planner **lê de volta** (activities.py:1197-1214, :1683-1685); o docstring "Nothing reads it yet" está stale. (2) O "diff vermelho→verde" **não é capturado em lugar nenhum** — o dado não existe, não é só desuso. (3) O pipeline de promoção de skills existe **completo e sem nenhum invocador de produção**: `materialize_candidates`/`evaluate_candidate`/`promote` (skill_promotion.py, threshold 3, `ApproverRequired`) e as activities registradas (activities.py:3936-4014) — grep: só testes. As 3 fontes que já escrevem episódios: clarification, ci_repair, review_feedback.
- Ledger **[ledger, medido na verificação]**: o "acúmulo" afirmado pelo código (run_episodes.py:1-14) **não existe em produção** — `skill_episode` tem exatamente **1 linha all-time** (clarification, occurrence_n=1), zero ci_repair, zero review_feedback, em 79 itens; o pipeline de promoção exige `SUM(occurrence_n) >= 3` do mesmo pattern_key para materializar um candidato.
- Proposta e custo: fonte nova "rodada reprovada→verde" = ponto de captura no fix loop + captura do diff (inexistente) + migration + gatilho para o pipeline — ~100-200 linhas. Alternativa menor dentro do mesmo item: dar gatilho ao pipeline que já existe, sobre as fontes que já escrevem.
- Dependências/conflitos: promoção sem humano é impossível por construção (ApproverRequired; invariante "o DSE nunca aprova o próprio trabalho"). Sobrepõe com D2. O repositório central de skills (console) está fora deste repo.

### Projeto E — Robustez do harness

**E1 — Preflight acusa drift de versão**
- Estado **[código]**: o preflight (deploy/vps/preflight-upgrade.sh, 105 linhas) checa checkout/working tree/HEAD/inventário de recursos + diff de imagens "informativo, não bloqueia" (:95-103). **Nenhuma comparação de versão entre componentes.** O values tem âncora `_ghcrTag: rc.53` com exceções deliberadas (orchestrator/agent-runner rc.56, console rc.7 de outro repo) — um check ingênuo dispararia hoje. O incidente do backlog (adapters na rc.31 por 22 releases) está registrado em comentário no próprio values (:51-52).
- Ledger: 1 incidente medido (deploy da rc.53) **[histórico]**; recorrência desde então: não medido.
- Proposta e custo: ~20-40 linhas de bash; exige política de exceções (pins intencionais). Risco real: falso positivo bloqueando deploy legítimo. **Nenhuma suite cobre deploy/vps.**
- Dependências/conflitos: nenhum.

**E2 — H5: infra não incrementa retry nem entra no fix_context**
- Estado **[código]**: **metade entregue.** Lado Tester: a distinção existe e é comportamental — ending de infra escala sem comprar retry (patch `tester-infra-outcome-escalates-v1`, workflows.py:2379-2423). Lado L1: a classificação existe (`GateStatus.ERROR`, quality_checks.py:115-139) mas o laço trata ERROR e FAIL igual (incremento :2735, fix_context :2788); único uso de ERROR é excluí-lo da exclusividade da pinça (:2636-2639).
- Ledger **[ledger, query rodada neste review]**: o cenário que o item teme **já aconteceu** — wi_8c46e17e queimou o cap inteiro em 4 rodadas cujo único não-PASS era `sast: ERROR` (24/07); no total, 5 dos 9 mortos por cap tiveram ≥1 rodada com gate ERROR (sast/lint). Contexto histórico: 13/31 rodadas infra + desvio de alvo 2× antes das 4 camadas fecharem; os PRs recentes convergem (18/20 ≤2 rodadas), mas o caso consumado existe.
- Proposta e custo: ~30-80 linhas no ramo mais delicado do workflow (pinça + cap terminal), patch marker de replay. Suites: test_tester_infra_outcomes.py, test_fix_loop_feedback.py.
- Dependências/conflitos: sobrepõe com E6 (mesmos sites de incremento; o doc de ataque ordena H5 antes de H6).

**E3 — Detector v3: renames**
- Estado **[código + medido em repo descartável]**: confirmado por medição de git real — `git diff --name-only` (o comando do scoped_git.py:262-264) num rename com edição lista **só o caminho novo**; o sujeito da spec sai da lista e o prefixo não casa. Divergência menor: quem nomeia renames é `--name-status` (R100), não `git status -M` como escrito.
- Ledger: nunca mordeu em produção — **não medido, só previsto** (célula cinza anotada no mapa).
- Proposta e custo: acrescentar o caminho antigo à lista ~5-15 linhas em scoped_git.py **+ a cópia vendorizada no agent-runner** (mesmo mecanismo do incidente rc.39); mudar o formato da lista espalharia por 2 serviços + contrato. Suites: test_run_coder_turn_scoped_git.py, test_spec_conflict*.py.
- Dependências/conflitos: alimenta os parques; conflita com skip doc-only e no-op guard se o formato mudar.

**E4 — Deriva de nome no reauthor (tratamento geral)**
- Estado **[código]**: o filtro stem-equivalente existe e é **deliberadamente estreito** — só normaliza o marcador `-dse` (comentário explícito: "qualquer outra diferença continua caminho novo, recusado"; activities.py:3003-3040); a escrita aterrissa sempre no caminho ordenado (:3035-3039). O delta vira evento `tester_reauthor_missed` com details completos. rc.51 confirmada (commit e154d34 na tag).
- Ledger **[histórico]**: `tester_reauthor_missed` medido (miss total wi_6f00bf0a, parcial wi_53c820f1) — ambos ANTES do filtro; miss fora do caso `-dse` desde então: não medido.
- Proposta e custo: generalizar ~10-40 linhas; risco: aterrissar conteúdo do modelo em caminho errado — a garantia in-place atual é o que contém isso.
- Dependências/conflitos: sobrepõe com E5 (discard reduz os casos que chegam ao reauthor).

**E5 — Veredito `discard`**
- Estado **[código]**: não existe (grep = 0). O handler aceita retry e reauthor (este só com reason=`tester_spec_exhaustion`); **qualquer outra string escala** (workflows.py:1348-1351) — "escalate" não é um veredito aceito, é o fallback. O plumbing do reauthor (ordem no input, sobrevive a CAN, consumo one-shot) é o molde.
- Ledger **[ledger]**: 5 vereditos reauthor usados (todos no caso 1); o túnel das rc.48-52 é o precedente de custo. Casos onde discard teria sido a resposta: as specs de badge do caso 1 (análise do túnel, grounding §4).
- Proposta e custo: ~60-150 linhas (ramo no parque + 2 call sites + plumbing + ação material no Pod + auditoria). Suites: test_tester_spec_exhaustion.py, test_tester_reauthor_order.py.
- Dependências/conflitos: mesmo canal de ordem do reauthor (E4); mesmo ramo que E2 toca; a entrega do veredito é o problema de A6.

**E6 — H6: orçamentos de retentativa separados**
- Estado **[código]**: as três afirmações de H6 conferem hoje — 6 sites de incremento (2274/2414/2442/2735/3711/3992), **nenhum reset em código de execução**, e o campo atravessa continue_as_new (models.py:54; _continue_as_new :1617-1623). Contraste: os contadores de instância (_noop_coder_turns etc.) zeram por run. A consequência medida está em comentário no próprio código (:2434-2437).
- Ledger: mortes recentes atribuíveis à mistura de fases — não medido neste passe (query específica não rodada).
- Proposta e custo: ~40-100 linhas (campos novos + 6 incrementos + 5 checks + mensagens + query), patch de replay.
- Dependências/conflitos: sobrepõe com E2 (mesmos sites; H5 antes de H6 por decisão documentada).

**E7 — H2: falha-rápida no L1**
- Estado **[código]**: os gates rodam sequenciais, sem early-exit, veredito no fim (pipeline.py:68-208); durações por gate já vão ao ledger; sandbox tem 1 vCPU (comentário :89 — paralelizar não é opção).
- Ledger **[ledger]**: Angular FAIL avg 514-762s (gate `test` 305-852s domina); Java 16-84s. O custo por rodada em **dinheiro** é dominado pelo Coder (US$ 0,6-4,7), não pelo L1 — o L1 custa tempo de parede.
- Proposta e custo: ~15-40 linhas; risco: consumidores assumem a lista completa de findings — early-exit **empobrece o fix_context** (o Coder passa a ver 1 falha por rodada em vez de todas), e a regra "an operator must never have to guess whether a gate ran" exige status explícito de gate não-executado.
- Dependências/conflitos: mesma região de E8/E9; muda quando o baseline (E9) roda.

**E8 — Parser casa "2 errors" do javac como contagem de teste**
- Estado **[código]**: confirmado — `_COUNT_RE` hoje em quality_checks.py:364; "2 errors" do javac vira `TestCounts(executed=2, failed=2)`, `evidence=True` com zero testes executados, summary "2 errors" publicado como contagem (:566, :607). O gate ainda reprova pelo returncode — o veredito fica certo, a **evidência** fica falsa (família da falha silenciosa). Não há teste cobrindo o caso javac.
- Ledger **[histórico]**: observado na autópsia do caso 2; frequência desde então: não medido.
- Proposta e custo: ~10-30 linhas + teste vermelho; risco: apertar demais derruba dialeto legítimo em "no test count found" (que reprova por falta de evidência).
- Dependências/conflitos: mesma função que E9; E7 muda quando o parser roda.

**E9 — Baseline por suite, não por teste**
- Estado **[código]**: confirmado, com a limitação **escrita no próprio código** ("se o item piorar uma suite já vermelha… a piora não aparece aqui", quality_checks.py:573-575). Identidade por arquivo (jest) / classe (surefire); fail-closed quando o baseline não computa.
- Ledger: piora mascarada em suite já-vermelha — nunca observada; **não medido, só previsto**.
- Proposta e custo: ~40-80 linhas (parser por teste nos 2 dialetos + bump de cache + comparação), com risco direto sobre a semântica de NOT_OUR_FAILURE.
- Dependências/conflitos: mesma região de E8; o diff/L2 é o vigia atual do caso mascarado.

**E10 — Stranded-sweep não cobre kill manual**
- Estado **[código]**: são DOIS varredores. O stranded-sweep só toca banco/audit (nunca Pod). Quem cobre Pods é o **reaper** (CronJob): Pod Succeeded/Failed coletado em 900s; Pod Running só quando `expires-at` vence — TTL default **72h** (k8s_driver.py:59). Terminate mata o workflow sem rodar nenhum ramo de teardown (todos são código do workflow, workflows.py:1082-1112 + 6 call sites) → o Pod fica Running até o TTL. Divergência: "pods órfãos" = "pods vivos por até 72h", não órfãos para sempre.
- Ledger **[ledger]**: hoje 0 órfãos no sentido estrito; **os 4 pods Running são de itens `review_ready`** — sandbox vivo enquanto o item espera revisão humana (o mais velho, 4h40m). O custo real observado é esse, não o kill manual (VPS já teve disk-pressure, era rc.53).
- Proposta e custo: cruzar reaper × estado do workflow esbarra na NetworkPolicy do CronJob (egress só ao API server, reaper.py:19-21); alternativas: TTL/teardown por transição de status (ex.: entrar em review_ready) — mas o fix cycle pós-CI pode re-precisar do sandbox; superfície cruza 3 serviços.
- Dependências/conflitos: pré-requisito compartilhado com B8 (probe de Temporal); risco central documentado: "deleting a live one destroys an agent turn mid-flight".

**E11 — Contabilidade de tokens não fecha**
- Estado **[código]**: dois write paths (gateway lê `usage.prompt/completion_tokens` + custo do header; coder soma `input_tokens`/`output_tokens` dos ResultMessage do SDK). **Nenhum path lê tokens de cache** (grep `cache_read`/`cache_creation` = 0 no repo) — e `input_tokens` do protocolo exclui cache, o que explica tokens_in nas centenas com custo alto. O `model` da linha do coder é env var, não o modelo real. Turnos falhados JÁ são metrados (:644-699) — "retries invisíveis" do Temporal não se sustenta para o coder.
- Ledger **[ledger]**: a anomalia é dominante — 102 linhas (todas stage=coder) somando US$ 203,46 = **87% do gasto all-time**; coder tem tok_in médio 563 (28× menor que o planner) e custo médio 92× maior. **Tester sub-metrado confirmado**: 162 turnos vs 146 linhas; 64 turnos reportam custo 0.0 (re-execuções não geram linha). Planner: 84 vs 83.
- Proposta e custo: ler os campos de cache nos 2 paths (~20-60 linhas sem migração; colunas novas = migração + projeções do console); metrar re-execuções do tester. Risco de enforcement baixo (budget usa SUM(cost_usd)).
- Dependências/conflitos: pressupõe gateway/SDK exporem os campos (não verificado aqui). Toda a economia citada neste review usa `cost_usd`.

**E12 — Invariante `reauthor_context ⊇ authoring_context`**
- Estado **[código + suite rodada]**: **ENTREGUE.** O teste existe e nomeia o invariante literalmente (`test_the_order_prompt_is_a_superset_of_the_authoring_prompt`, test_tester_authoring_reads_skill_references.py:140-187), monta os dois prompts pelo código de produção e assere cada âncora. Suite `services/sandbox-runtime` rodada neste passe: **verde** (395 passed). O lado orchestrator é vigiado por test_tester_spec_exhaustion.py:441-472.
- Proposta e custo: zero.

### Projeto G — Preview

**G1' — Clone via installation token** · **G2 — Receita por kind** · **G3 — Proxy do FE**
- Estado **[código]**: os três estão implementados e commitados como o backlog descreve (G1': `resolve_preview_credential` com precedência ssh→token, mint no trigger sem cache, higiene token-fora-de-argv/pod-spec pinada em teste; G2: paths-filter + receita deployable com build do validation.json + Postgres efêmero + kind no ledger; G3: proxy gerado no deploy, degrau 2 = irmão por group_id via svc in-cluster, degrau 1 = fallback). Fatos de contorno: no ramo gitops nenhuma credencial é semeada (G1' só vale em apply=kubectl); o alvo do proxy é resolvido **uma vez, no trigger** — irmão que ganhe preview depois não atualiza um FE já rodando; o degrau 1 não tem alvo configurado hoje (`DSE_PREVIEW_FE_API_BASE` vazio).
- Config efetiva da VPS **[código, verificado neste review]**: `preview: enabled=true, mode=source, apply=kubectl, previewRbac.create=true, ttlSeconds=3600, maxConcurrent=3` (deploy/vps/values-vps-poc.yaml:227-239) — as receitas e o RBAC estão ativos em produção.
- Ledger **[ledger]**: aceitação ainda **não alcançada** — `preview_created`=8, todos kind=ui do repo antigo; **zero deployable jamais ficou Available**; degradações: 7× timeout de `kubectl wait`, 2× RBAC Forbidden (anteriores aos commits d924083/f14962b), 2× read-only FS, 1× cap 3/3.
- Dependências: G3-degrau-2 depende de G2 (BE vivo); o 404 do `PUT /{id}/retire` no preview do FE, se aparecer, é acerto (expõe o vão do C1).

**G4 — TTL/GC de namespaces**
- Estado **[código]**: divergência total com o backlog ("especificado, não implementado") — **existem três mecanismos**: (1) reaper git-based `reap_expired_previews` + activity `wse_reap_previews` (esta **sem nenhum call site**); (2) **CronJob helm `preview-reaper`** a cada 10 min deletando namespaces com `expires-at` vencido — e o gate dele (enabled+kubectl+previewRbac) está **satisfeito na config da VPS**, com TTL 1h; (3) eviction LRU no cap por tenant (cap 3/3 já disparou em produção [ledger]).
- Lacunas reais: a activity órfã (higiene); modo gitops sem scheduler; e o **TTL de 1h contra janelas de revisão humana de horas** (os 4 itens review_ready de hoje esperam 3-4h+) — o preview morre antes do revisor chegar; re-trigger por `human_request` existe (3 medidos).
- Proposta e custo: como formulado, nada a construir; ajuste de TTL = 1 linha de values.
- Dependências/conflitos: a Secret da credencial (G1') morre com o namespace — coleta OK.

**G5 — G-1'': preview recebe o workspace**
- Estado **[código]**: nada implementado (grep initContainer/tarball = 0 no preview); o pod sempre clona. Decisão de operador registrada: G-1' para o POC; G-1'' se sair de POC. Superfície futura: ~150 linhas afetadas no `_source_deployment` + mecanismo de transporte de workspace inexistente + reescrita de ~9 testes-pino.
- Dependências/conflitos: substituiria o G1' recém-entregue — não tocar sem o gatilho da decisão.

**G6 — Cluster de integração no CI**
- Estado **[código]**: o único e2e real (`test_preview_e2e_real_cluster_create_serve_and_ttl_reap`) exige k3d + Argo CD e fica **skipped local E no CI** (não há cluster no runner; grep k3d em workflows = 0). Ele é o único teste que exercita o reaper git-based contra cluster real.
- Ledger **[ledger]**: a aceitação em produção está falhando exatamente em coisas que um e2e de cluster pegaria (RBAC Forbidden, read-only FS, timeout de deployment) — o gatilho condicional do backlog ("se a aceitação falhar no que a camada determinística não pegou, este item sobe") **disparou parcialmente**.
- Proposta e custo: job de CI com k3d + Argo CD + git server — dezenas de linhas de workflow + 2-4 min de teste + provisionamento; risco de flake de infra no runner. Alternativa: rodar o mesmo e2e contra k3d local como passo de DoD de rcs que tocam preview (zero mudança no teste — o skip desliga sozinho).

### Projeto F — Arquitetura maior

**F1 — H1: executor escopado do Coder**
- Estado **[código]**: o Coder comprovadamente não executa nada — `DEFAULT_ALLOWED_TOOLS = [Read, Write, Edit, Glob, Grep]` (substrate.py:280), sem CoderToolset, `run_tests` exclusivo do Tester, e **dois testes pinam a ausência** (test_agent_turn.py:57, test_substrate_conformance.py:151-163).
- Ledger **[ledger]**: a query de encerramento **foi rodada neste review**: 18 de 20 PRs convergiram em ≤2 rodadas de Coder sem executor. As 2 exceções: PR #17 (contrato cross-repo — executor não ajudaria) e PR #15 (pinça JSDOM — idem).
- Proposta e custo: zero código; o item era a query.

**F2 — Migração `data-test` nas specs do cliente FE**
- Estado **[medido em clone raso neste review]**: repo `bmo-fee-calculator-fe-dse` @ 87b63bd (idêntico nos remotes andresaraivafintex e fintexinc). Nos `.spec.ts` de tests/suits: **zero** nth-child/max-w-/getComputedStyle — as 3 posicionais foram migradas mesmo. O resíduo vive nos **page objects** (tests/lib): 6 nth-child em Home.page.ts:56-72 (posicionais de coluna/célula), ~10 `.nth()` em Program.component.ts (maioria indexação legítima sobre getByTestId), ~9 seletores CSS crus em 4 page objects; + 1 nth-child e 1 assert de classe Tailwind em 2 specs unitárias de src/app. Adoção data-test: 629 nos templates.
- Proposta e custo: trabalho todo no repo do **cliente**; nenhuma suite deste monorepo vigia; risco: seletor novo errado derruba suites verdes do cliente.
- Dependências/conflitos: decisão pendente de namespace canônico (andresaraivafintex vs fintexinc, commit 043d619).

**F3 — Fatos falsos em arquivos de grounding**
- Estado **[código]**: o fato falso citado **ainda está lá** — `.fable/run-state.md:62` ("Fails on a different test each time…"), sem proveniência; e a **mesma afirmação replicada vive em `CLAUDE.md:40` como regra de conduta** (seção Pegadinhas). run-state.md:41 afirma "This file is NOT committed" — mas está commitado (91baa3e). OVERNIGHT.md: afirmações sem proveniência identificadas (l.33, l.96, l.110, l.118-119) ao lado de seções bem-proveniadas; quebra estrutural no log (l.225-232 inserida no meio do append-only). A falsidade do fato em si não foi re-verificada — **não medido** (exigiria medir a flakiness da suite de control-plane).
- Proposta e custo: ~10-20 linhas de anotação em 2 arquivos + a decisão sobre CLAUDE.md:40; zero código; nenhuma suite roda sobre .fable/.

---

## PASSE 2 — Chapéu Produto (julgamento)

Formato: as 4 perguntas obrigatórias, respondidas com o registro do Passe 1. Vereditos: `FAZER como proposto` / `FAZER diferente` / `ADIAR até <gatilho>` / `MATAR`.

### A1 — bot_ts herdado no fan-out
1. **Dor do usuário:** "Cliquei em Aprovar e nada aconteceu — o pedido ficou parado." (E pior invisível: o clique pode aprovar o plano do item errado.)
2. **Se nunca fizermos:** todo pedido Slack que vira mais de um repo tem o botão de aprovação quebrado — hoje 100% dos grupos Slack medidos [ledger: 2 cliques, 2 perdidos, hoje]. Quem sente: o solicitante, no momento mais crítico (destravar o próprio pedido).
3. **Caminho mais barato:** já é o mais barato possível (1 linha). Alternativa considerada: correlacionar por thread_ts em vez de bot_ts — descartada: mudaria a semântica de correlação de todos os kinds para consertar um INSERT.
4. **Veredito: FAZER como proposto** — com A4 como o vermelho commitado antes, na mesma rc.

### A2 — steering_rejected_unauthorized
1. **Dor do usuário:** metade do item não tem dor de usuário (bot re-ingerindo a si mesmo é lixo de telemetria); a outra metade tem: "respondi a pergunta do DSE no GitHub e fui ignorado, sem nem um aviso".
2. **Se nunca fizermos:** o ruído de bot cresce ~1/dia [ledger: 15, +1 hoje] e polui as métricas de steering; e cada comment que o A3 postar no GitHub será re-ingerido pelo próprio bot — o problema dobra quando A3 sair. O sub-caso humano (5 de julho + 2 Slack de hoje) segue mudo.
3. **Caminho mais barato:** o filtro `_is_bot_comment` **já existe** — só falta aplicá-lo no webhook (~5-15 linhas). Para o sub-caso humano, o caminho barato é A5-config (allowlist), não código novo aqui.
4. **Veredito: FAZER diferente** — só o filtro de bot agora, como pré-requisito do A3; o sub-caso "requester = quem etiquetou" vai para o A5 e a decisão de produto (sender vs autor) fica explícita lá.

### A3 — resposta in-channel de GitHub/Jira
1. **Dor do usuário:** "Comentei no ticket e o robô me ignorou" — o silêncio é a pior resposta possível para quem não conhece o sistema.
2. **Se nunca fizermos:** no GitHub (canal ativo do POC), quem errar o formato do pedido não recebe orientação — frequência real não medida, mas o custo por ocorrência é um usuário que desiste. No Jira: nenhum uso real medido.
3. **Caminho mais barato:** replicar o handler do Slack (~20-40 linhas/adapter). Não há caminho mais barato que o proposto para o GitHub; para o Jira, o mais barato é **não fazer ainda**.
4. **Veredito: FAZER diferente** — GitHub sim, **depois** do filtro do A2 (senão o bot responde a si mesmo); Jira **ADIAR até o primeiro fluxo Jira real de um usuário** (gatilho mensurável: 1 recusa non-task auditada vinda do Jira).

### A4 — Botão de Approve nunca re-testado
1. **Dor do usuário:** a mesma do A1 — é o teste que prova que a dor morreu.
2. **Se nunca fizermos:** o fix do A1 entra sem vermelho que reproduza a ordem de produção (post do prompt ANTES do INSERT do irmão) — e a próxima mudança de ordem no fan-out reintroduz o bug em silêncio. Os testes atuais passam exatamente porque invertem a ordem.
3. **Caminho mais barato:** é barato por natureza (~10-30 linhas de teste). Alternativa "testar em produção de novo": não substitui — foi assim que ficou 0 de 2.
4. **Veredito: FAZER como proposto** — é a Definição de Pronto do A1, não um item separado. Aceitação viva: o próximo item cross-repo aprovado por botão, sem SSH [critério do projeto A].

### A5 — Identidade cross-canal
1. **Dor do usuário:** "O DSE me conhece no Slack mas me trata como estranho no GitHub" — real, mas hoje o "usuário" é um: o operador.
2. **Se nunca fizermos:** num POC single-user, quase nada — as rejeições humanas medidas (5+2) têm workaround de configuração. Num cenário multi-usuário, vira bloqueador de A6 (autorizar veredito por qualquer canal).
3. **Caminho mais barato:** para os kinds gateados (clarification/steering/repo_select), **existe e custa zero código**: registrar os principais do operador (slack/github/jira) no `tenant_steering_allowlist`. Para **approval** a config é irrelevante — o gate por principal não existe hoje (achado do Passe 1); autorizar approval por identidade é código novo, e pertence ao bloco A6 quando os vereditos ganharem canal.
4. **Veredito: FAZER diferente** — agora: (i) a configuração de allowlist com todos os principais do operador (runbook); (ii) registrar o achado "approval sem gate de principal" como item de segurança nomeado — aceitável em POC single-user, inaceitável no primeiro segundo humano. O merge real de identidade: **ADIAR**, com uma ressalva de honestidade no gatilho: as 2 rejeições Slack de hoje (usr_2756c382…) podem ser um segundo humano OU um principal não-linkado do próprio operador — **é indecidível pelo ledger, e essa indecidibilidade é exatamente o que o merge daria**. Ação barata primeiro: perguntar ao operador quem é usr_2756c382. Se for segundo humano, o gatilho já disparou e A5 volta à mesa; se for o operador, a config (i) o cobre e o gatilho passa a ser "1 rejeição de principal que o operador declare não ser dele".

### A6 — Veredito pelo canal
1. **Dor do usuário:** "O robô disse que precisa de mim, mas responder exige SSH e um comando Temporal" — o usuário-alvo simplesmente não consegue responder. É a maior quebra da jornada hoje.
2. **Se nunca fizermos:** todo parque continua sendo destravado pelo operador via CLI [ledger: 10 de 10, 7 com actor digitado à mão] — o sistema não funciona para ninguém além de quem o construiu. O critério de aceitação da Fase 1 ("caso 3 inteiro sem nenhum SSH") é literalmente este item.
3. **Caminho mais barato:** fatiar por canal e por veredito: rota no dispatcher + botões **Slack** primeiro (retry/reauthor/discard no parque; o padrão de botões e o gate de autorização já existem no plan approval). GitHub/Jira depois. Não inventar UI nova — reusar `approval_blocks`.
4. **Veredito: FAZER diferente** — como **bloco único com B3+B4+E5**: a pergunta (dossiê renderizado), a resposta (rota+botões) e o veredito barato (discard) nascem juntos, Slack primeiro. Depende de A1/A4. GitHub na segunda leva (após A2-filtro), Jira com o gatilho do A3.

### B1 — Etapas simplificadas por canal
1. **Dor do usuário:** "spec_conflict? tester_spec_exhaustion? kkk" — jargão do sistema na cara de quem pediu uma mudança.
2. **Se nunca fizermos:** o dano real medido está nos DOIS vazamentos (fallback de spec_conflict e details técnicos) — o resto dos 12 status já é razoavelmente humano [código]. O colapso em 5 etapas sem usuário real testando é redesenho no escuro.
3. **Caminho mais barato:** fechar os dois vazamentos (~15-30 linhas, metade já vem com B3) e manter a granularidade atual. O colapso em 5 etapas fica para quando houver um usuário não-operador reclamando de granularidade — hoje não há [não medido].
4. **Veredito: FAZER diferente** — só a tradução dos details técnicos + entrada spec_conflict (carona no bloco do parque). O vocabulário de 5 etapas: **ADIAR** (gatilho contável: 1 feedback registrado de usuário não-operador sobre as mensagens de status — em canal ou issue, não impressão de corredor).

### B2 — Update in-place
1. **Dor do usuário:** nenhuma — já não existe: os 3 canais editam a mesma mensagem [código].
2. **Se nunca fizermos:** nada acontece; está feito.
3. **Caminho mais barato:** n/a.
4. **Veredito: MATAR** — item já entregue; remover do backlog para não gastar atenção. (O backlog dizia "metade do mecanismo já existe" — é o mecanismo inteiro.)

### B3 — Erros em linguagem humana
1. **Dor do usuário:** "O robô disse 'DSE status: spec_conflict' e nada mais" — quando na verdade ele **sabe exatamente** o que quer perguntar (Expected/Received, spec, linha) e joga isso fora na borda do canal.
2. **Se nunca fizermos:** 13 dossiês montados, 13 descartados [ledger]; cada parque exige o operador ir ao ledger via SSH para saber o que está sendo perguntado.
3. **Caminho mais barato:** já é o item mais barato do backlog inteiro: ~3-10 linhas (1 entrada na tabela de bodies — o texto já vem pronto do workflow).
4. **Veredito: FAZER como proposto** — imediatamente, dentro do bloco do parque (com B4/A6/E5). Melhor razão custo/valor de todo o backlog.

### B4 — Parque = pergunta
1. **Dor do usuário:** "O robô precisa de mim mas não me perguntou nada" — e mesmo que perguntasse, não há como responder (Passe 1: a rota de resposta não existe; o clique é declinado).
2. **Se nunca fizermos:** parque continua = SSH. O efeito de segunda ordem mudou de forma **hoje de manhã**: o atropelo histórico do sweep sobre parques (4 escalações de spec_conflict [ledger]) foi corrigido no commit 277b937 (spec_conflict entrou na lista de espera humana do sweep) — mas a correção **troca o atropelo por permanência**: um parque cujo workflow morreu agora nunca é varrido nem avisado. A pergunta sem canal de resposta não apodrece mais em 6h; congela para sempre.
3. **Caminho mais barato:** o desenho mínimo já está no Passe 1 (~50 linhas total: body + rota + 1 linha no STATUS_MAP que desavermelha a suite do console). Botões podem vir na segunda iteração; texto com instrução de resposta já resolve 80%.
4. **Veredito: FAZER como proposto** — dentro do bloco do parque. A linha do STATUS_MAP entra primeiro (suite vermelha na main é dívida de hoje, não de backlog).

### B5 — Last event com ruído
1. **Dor do usuário:** "O painel diz Running mas o último evento é 'dispatch declined'" — o painel mente por composição.
2. **Se nunca fizermos:** o ruído só cresce (1506 duplicatas ignoradas já são a maior fonte de eventos por item [ledger]); o painel perde a função de "onde está meu pedido".
3. **Caminho mais barato:** o proposto já é pequeno (~15-25 linhas, projeção re-projetável). Alternativa "filtrar no frontend": pior — o console é a verdade renderizada do ledger, filtrar na borda esconderia o dado de quem depura.
4. **Veredito: FAZER como proposto** — depois da linha do STATUS_MAP (B4), senão a suite vermelha mascara o verde deste item.

### B6 — Irmãos sem se identificar
1. **Dor do usuário:** "Tem duas conversas intercaladas na minha thread e parecem uma só, se contradizendo."
2. **Se nunca fizermos:** todo pedido multi-repo no Slack produz esse teatro (3 grupos até hoje; o caso 3 confundiu até o operador). Baixa frequência, alta confusão por ocorrência.
3. **Caminho mais barato:** prefixo `[repo]` no body (~5-15 linhas). A variante "uma mensagem por grupo" custa 10× e briga com a correlação por bot_ts — descartá-la explicitamente.
4. **Veredito: FAZER diferente** — só a variante mínima (prefixo), na mesma rc de B3/B4. **Matar a variante "uma mensagem por grupo"**. De carona: corrigir o comentário mentiroso em workflows.py:1774-1778 (docstring que descreve um comportamento que não existe é a família F3).

### B7 — Ninguém é avisado da morte
1. **Dor do usuário:** "Pedi há 3 dias, o card diz 'implementando' até hoje" — a pior experiência possível: silêncio após confiar uma tarefa.
2. **Se nunca fizermos:** ~40 mortes/escalações mudas até hoje [ledger: 9 caps + 31 sweeps]; cada uma é um usuário que aprende a não confiar no sistema. Frequência: toda morte, para sempre.
3. **Caminho mais barato:** o caminho (1) custa 2-5 linhas (o post que falta no único terminal sem post). O aviso do sweep (~20-40 linhas) cobre o resto. Não há alternativa mais barata que "postar a mensagem que já existe".
4. **Veredito: FAZER como proposto** — caminho (1) imediato; o aviso do sweep na mesma leva de B8.

### B8 — terminate não atualiza status
1. **Dor do usuário:** derivada da B7 (o silêncio); a parte "banco errado" é dor de operador.
2. **Se nunca fizermos:** o sweep já converge o caso comum em 6h [ledger: 0 presos hoje]; os buracos reais são (a) parque humano terminado = invisível para sempre e (b) o sweep **escala parque legítimo por não ter o probe que o próprio código exige** — 4 spec_conflicts atropelados [ledger].
3. **Caminho mais barato:** não construir listener de terminate (caro, cobre só terminates nossos); implementar o **probe de Temporal no sweep** (~30-60 linhas) — é dívida que o próprio docstring declara, e desde o 277b937 é a **única** defesa contra workflow morto em status de espera humana (o (b) do atropelo já morreu por outra via).
4. **Veredito: FAZER diferente** — probe no sweep + notificação de canal no sweep (com B7), com prioridade **reforçada** pelo 277b937. O wrapper de terminate: **ADIAR** (gatilho mensurável: 1 terminate nosso que o sweep+probe não converta em escalated+aviso dentro de 1 ciclo do CronJob).

### C1 — Contrato de interface entre irmãos
1. **Dor do usuário:** "Aprovei as duas mudanças, fiz o merge e o botão dá 404" — o sistema entrega duas metades que não se encaixam, e o usuário só descobre no clique.
2. **Se nunca fizermos:** todo pedido cross-repo paga o imposto medido: US$ 11 de rodadas cegas + costura manual + risco de merge quebrado [ledger, caso 3]. Com C1: a versão dirigida do mesmo trabalho custou US$ 1,85 [ledger] — o contrato é o que transforma uma na outra.
3. **Caminho mais barato:** três formas, em ordem de custo: (a) **costura manual documentada como runbook** (o que já funcionou hoje — US$ 1,85 — mas exige o operador perceber o vão); (b) **segurar o despacho do irmão até o plano do primário existir** e entregar o PlanArtifact como grounding (hold no dispatcher + entrega no prompt do Planner do irmão — sem novo estágio de workflow); (c) o proposto (estágio de Planner pré-fan-out — centenas de linhas em região de replay). A forma (b) entrega o mesmo grounding com muito menos superfície, ao custo de serializar o **planejamento** (não a implementação). Honestidade sobre o 6×: a razão foi medida com contrato escrito **por humano** num item novo — a hipótese "PlanArtifact do primário ≈ contrato" é plausível e **[não medido]**; a primeira rc do C1 é o teste dela, com critério de falha explícito.
4. **Veredito: FAZER diferente** — implementar a forma (b), com quatro condições de desenho que a verificação adversarial exigiu e são todas justas: (i) **timeout com fallback**: irmão que espera plano de primário morto despacha cego + avisa (primários morrem — 9 caps no ledger; sem isso a feature falha em silêncio, compondo com o B7); (ii) definir o gate: plano **produzido** ou plano **aprovado** (cross_repo força risk high → aprovação humana → a espera herda a latência do humano e o botão do A1); (iii) **direção**: "primário" é quem o roteador nomeou — pode ser o FE; decidir se o contrato é sempre do lado BE/dono-da-API ou aceitar contrato FE-led; (iv) tratar a primeira medição como teste da hipótese PlanArtifact≈contrato. Entra depois do A1, como já decidido. A forma (a) vale como runbook **até** a rc do C1.

### C2 — Link entre as PRs do grupo
1. **Dor do usuário:** "Aprovei uma PR sem saber que ela só funciona com a outra" — o revisor decide sem a informação mais importante.
2. **Se nunca fizermos:** o risco medido é exatamente o aviso vigente do caso 3: "não mergear o par como está" vive na cabeça do operador, não na PR [ledger: PR #5 e #17 sem nenhuma referência mútua].
3. **Caminho mais barato:** uma linha no corpo da PR ("Par de <repo>#<n> — não mergear isolado" quando o grupo tem irmão), aceitando assimetria (a primeira PR ganha o link via edição no segundo passe, que o upsert de PR já sabe fazer). Sem tabela nova, sem simetria garantida.
4. **Veredito: FAZER diferente** — a variante de uma linha com segundo passe, logo após C1 (o contrato dá o texto do link de graça).

### C3 — Ordem BE→FE
1. **Dor do usuário:** a mesma do C1 (metades que não se encaixam) — por outro mecanismo.
2. **Se nunca fizermos:** nada além do que C1 já cobre, **se** C1 sair na forma (b) — que já embute a serialização do planejamento.
3. **Caminho mais barato:** deixar o C1-(b) absorver a ordem de planejamento; ordem de **implementação/merge** completa nunca teve dor medida [não medido].
4. **Veredito: ADIAR até C1-(b) ser medido** — se o primeiro par com contrato ainda produzir incompatibilidade, C3 reabre com o dado. Como item independente, morre por redundância com a forma escolhida do C1.

### C4 — Múltiplos repos por canal
1. **Dor do usuário:** nenhuma formulável hoje — o roteamento multi-repo já funciona pelo router [código: caso 3 roteou por ele].
2. **Se nunca fizermos:** nada mensurável acontece; as duas evidências do item estão **falsificadas pelo código de hoje** (base_branch é lido em 3 lugares; o orchestrator usa repo_bindings além do preview).
3. **Caminho mais barato:** n/a — não há problema demonstrado a resolver.
4. **Veredito: MATAR** — o item nasceu de uma medição que envelheceu. Reabrir apenas com dor real de um tenant que precise de N repos **por binding** (o gatilho é um pedido de roteamento que o router de hoje erre por causa disso).

### C5 — Porta 4: mudança de comportamento declarada
1. **Dor do usuário:** "O teste antigo descreve o comportamento que eu pedi para mudar — e o sistema trava nele" — legítima em tese.
2. **Se nunca fizermos:** até hoje, **zero ocorrências** [ledger: no único caso candidato, a spec do cliente estava certa e segurou um bug real três vezes — a porta 4 teria autorizado quebrá-la]. O parque humano existente já cobre o cenário com um humano no loop.
3. **Caminho mais barato:** não construir; quando o cenário real aparecer, o veredito humano no parque (A6) pode ganhar uma opção "autorizar edição desta spec" — 20% do custo, mesmo efeito, sem relaxar guard antecipadamente.
4. **Veredito: ADIAR até o gatilho mensurável** — o primeiro parque porta-1 em que o humano conclua que a spec do cliente está genuinamente obsoleta. **Para o gatilho ser contável de verdade** (hoje essa conclusão morre no Slack ou na cabeça do operador — o parque só aceita retry/reauthor e o resto vira escalate genérico): o bloco A6/B4 deve incluir, a custo ~zero, um reason explícito `client_spec_obsolete` no veredito/escalate do parque, auditado. Se o contador chegar a 2, desenhar a porta 4 **como extensão do veredito do parque**, não como campo do plano. (O desenho atual relaxa um guard de segurança determinístico com base em zero casos medidos — é o perfil exato do túnel do reauthor.)

### D1 — Exemplo por proximidade com o diff
1. **Dor do usuário:** indireta — "o robô escreveu um teste que não parece com os do meu projeto" vira rodadas extras e specs ruins.
2. **Se nunca fizermos:** o Tester continua imitando o exemplo alfabeticamente primeiro — quantas specs ruins isso causou: não medido; o mecanismo de dano é plausível mas nunca foi isolado como causa.
3. **Caminho mais barato:** o proposto já é pequeno (~30-60 linhas, 1 arquivo). Alternativa ainda mais barata (skill por repo ensinando o padrão): é exatamente o que o projeto D quer eliminar.
4. **Veredito: FAZER como proposto** — na Fase 4 como planejado; é a melhoria de melhor custo do projeto D. Registrar antes/depois (qualidade da 1ª spec) para o D3 ter o que capturar.

### D2 — Onboarding automático (sonda)
1. **Dor do usuário:** "Plugar um repo novo exige um especialista descobrindo as manhas na mão" — dor real do operador, futura de qualquer cliente.
2. **Se nunca fizermos:** cada repo novo repete a arqueologia — os dois runs de referência do testbed Java custaram US$ 6,23 + US$ 4,50 = **US$ 10,73, 100% infra, sem PR**, cobrindo só 2 das 4 camadas na régua de ~US$ 4-5/camada do grounding [histórico, tabela do Passe 1]; com a sonda manual, ~US$ 0 — mas ela vive na cabeça do operador.
3. **Caminho mais barato:** **roteirizar a sonda como script de operador** (o molde `scripts/propose_agents_md.py` existe): Pod réplica + toolchain probe + relatório "o que mudou para sair do vermelho". 80% do valor (conhecimento antes do erro) por ~10% do custo do estágio automatizado.
4. **Veredito: FAZER diferente** — script primeiro; o estágio automatizado **ADIAR até 2-3 onboardings reais medirem o roteiro** (só então se sabe o que automatizar). A restrição do temporal-start (item sem linha em work_items) reforça: estágio de workflow é a forma cara.

### D3 — Captura de aprendizado
1. **Dor do usuário:** "Vocês ficam criando skill na mão" — a frase do operador que originou o projeto D; a dor é o sistema não aprender com os próprios erros.
2. **Se nunca fizermos:** cada modo de falha novo custa 1 item inteiro até alguém escrever a skill (US$ 4,14 medido no caso MockBean vs 2 min de skill [histórico]).
3. **Caminho mais barato:** a primeira versão deste review propôs "ligar o pipeline de promoção que já existe" — a verificação adversarial derrubou: com **1 episódio all-time** nas 3 fontes [ledger] e threshold 3, ligar o gatilho materializa **zero** candidatos; seria um no-op que mede esparsidade de captura, não suficiência das fontes. O caminho barato real é o diagnóstico: por que os writers de ci_repair/review_feedback **nunca escrevem** (call sites que não disparam ≠ fontes que existem)?
4. **Veredito: FAZER diferente, em duas fases honestas** — fase 1: diagnosticar os writers mudos (são 3 caminhos de escrita já implementados produzindo 1 linha em 79 itens — ou os fluxos que os alimentam não ocorrem, ou há defeito da família falha-silenciosa) e ligar o gatilho do pipeline **junto** com o conserto. Fase 2 (a fonte vermelho→verde, onde a dor do operador de fato vive — caso MockBean): **ADIAR com critério contável** — se após a fase 1 as fontes existentes produzirem <3 episódios úteis em 4 semanas de operação, a fase 2 sobe, porque será a única fonte com sinal. A captura do diff (hoje inexistente) continua sendo a parte cara — por isso ela espera o critério, não o instinto.

### E1 — Preflight acusa drift
1. **Dor do usuário:** nenhuma direta — dor de operador (deploy com componente 22 releases atrás, silencioso).
2. **Se nunca fizermos:** o incidente já aconteceu 1× e custou o caminho de volta do canal inteiro [histórico]; a âncora única no values reduz a chance de repetição, mas os pins-exceção continuam manuais.
3. **Caminho mais barato:** relatório de drift **informativo** no preflight (~15 linhas: tabela componente→tag, destacando quem está atrás da âncora) — sem bloqueio, sem política de exceções. O operador vê e decide.
4. **Veredito: FAZER diferente** — só o relatório. Bloqueio: **ADIAR até o drift morder de novo** (1 recorrência).

### E2 — H5: infra no laço do L1
1. **Dor do usuário:** "meu pedido morreu por um problema do ambiente, não do código". O lado Tester **já foi entregue** [código]; o lado L1, não.
2. **Se nunca fizermos:** *nota de método — a primeira versão deste review adiou o E2 com o gatilho "primeiro item cujo cap seja consumido por rodadas ERROR"; a verificação adversarial rodou a query e o gatilho **já tinha disparado**.* O caso consumado: wi_8c46e17e, cap inteiro queimado em 4 rodadas cujo único vermelho era `sast: ERROR`; 5 dos 9 mortos por cap tocaram gates ERROR [ledger]. Um usuário já perdeu um pedido inteiro por isso.
3. **Caminho mais barato:** escopo mínimo separável — rodada cujos únicos não-PASS são ERROR **não incrementa o retry e não entra no fix_context como falha de código** (re-roda o L1 ou escala como infra, espelhando a política que o Tester já tem). A classificação (`GateStatus.ERROR`) já existe; é só o laço que a ignora.
4. **Veredito: FAZER diferente** — o escopo mínimo acima, sob patch marker, **depois** do bloco da Fase 1 (o ramo é o mais delicado do workflow — pinça + cap terminal — e as 4 camadas fechadas tiram a urgência de dias, não a necessidade). E6 só se reavalia depois deste (decisão documentada: H5 antes de H6).

### E3 — Renames cegam o parque
1. **Dor do usuário:** nenhuma até hoje — célula cinza, prevista e vigiada [mapa de alcançabilidade].
2. **Se nunca fizermos:** o dia em que um Coder renomear o sujeito de uma spec parqueável, o parque não dispara — frequência: 0 em 79 itens [ledger].
3. **Caminho mais barato:** o fix é pequeno (~5-15 linhas) mas toca a **cópia vendorizada** (o mecanismo do incidente rc.39) — o custo de teste/verificação supera o de código.
4. **Veredito: ADIAR — como risco aceito, não como espera por sinal.** Honestidade que a verificação exigiu: o gatilho "primeiro parque cego por rename" é **inobservável por construção** — um parque que não dispara não emite nada; a detecção real seria autópsia de um merge já ruim. A decisão é: aceitamos esse risco enquanto a célula cinza do mapa (xfail) documenta a lacuna. Carona autorizada: se qualquer rc tocar `scoped_git`/vendored, o fix de 5-15 linhas entra junto, com teste vermelho.

### E4 — Deriva geral no reauthor
1. **Dor do usuário:** nenhuma — o caso medido (`-dse`) está coberto; deriva fora dele nunca ocorreu [ledger: misses medidos são pré-rc.51].
2. **Se nunca fizermos:** um miss futuro fora do caso `-dse` vira evento auditado (`tester_reauthor_missed` com details completos). Ressalva da verificação: hoje esse evento **morre no audit_log** (1 ocorrência all-time; nenhum consumidor específico; no painel vira um last_event transiente) — "visível" significa visível por SSH+psql, o padrão que este documento condena em B3.
3. **Caminho mais barato:** não generalizar; tornar o detector real com 1 linha — classificar `tester_reauthor_missed` como evento de atenção no mapper do console, dentro do trabalho do B5 que já vai acontecer.
4. **Veredito: MATAR** — a política estreita é deliberada e correta (o comentário no código a defende); generalizar amplia risco sem caso medido. Condição de honestidade do kill: a linha de classificação no B5, para o gatilho de reabertura ("primeiro miss real fora do caso `-dse`") alcançar um humano de verdade.

### E5 — Veredito discard
1. **Dor do usuário:** "Esse teste que o robô escreveu está errado — joga fora e segue" — hoje a única resposta é reauthor (caro) ou escalate (desiste).
2. **Se nunca fizermos:** specs próprias ruins continuam custando reauthors (5 usados no caso 1 [ledger]) onde um descarte resolveria; é a alternativa que teria evitado o túnel das rc.48-52 [análise registrada].
3. **Caminho mais barato:** já é uma tarde (~60-150 linhas sobre plumbing existente). Alternativa "reauthor com instrução 'esvazie a spec'": gambiarra que abusa do mecanismo caro.
4. **Veredito: FAZER como proposto** — **mas dentro do bloco do parque (A6/B4)**, para o veredito nascer exposto no botão e nunca precisar de SSH. Fazer E5 sozinho (Fase 5) e A6 depois criaria dois momentos de UI para a mesma pergunta.

### E6 — Orçamentos de retentativa separados
1. **Dor do usuário:** "o robô desistiu cedo demais" — quando fases distintas consomem o mesmo orçamento.
2. **Se nunca fizermos:** risco de morte prematura em itens longos; medido 1× em comentário de código [histórico], não re-medido desde os parques novos.
3. **Caminho mais barato:** a query de detecção (mortes no cap com rodadas de fases distintas) antes de qualquer código — **registrada no script de gatilhos da síntese**, não solta.
4. **Veredito: ADIAR até depois do E2** (que virou FAZER — mesmos sites de código, e H5 antes de H6 é decisão documentada) **e até a query acusar**. Com o E2 entregue, parte da pressão sobre o orçamento único desaparece — re-medir antes de mexer.

### E7 — Falha-rápida no L1
1. **Dor do usuário:** "demora" — o L1 Angular leva 8-15 min por rodada [ledger].
2. **Se nunca fizermos:** rodadas FAIL do Angular continuam custando ~10 min de parede; em dinheiro, quase nada (o custo é o Coder).
3. **Caminho mais barato:** nenhum bom — e o proposto tem um custo oculto que o backlog não nomeia: early-exit **empobrece o fix_context**, e o fix_context completo é o que sustenta a convergência em ≤2 rodadas [ledger: 18/20]. Trocar minutos de L1 por rodadas extras de Coder é trocar barato por caro.
4. **Veredito: ADIAR até a latência virar dor registrada** (gatilho contável: 1 reclamação de usuário não-operador sobre tempo de resposta, registrada em canal/issue). Correção da alternativa, apontada na verificação: "reordenar gates" sem early-exit é no-op de wall-time; o caminho do meio que preserva evidência é **skip condicional do gate `test` (o dominante, 305-852s) quando typecheck/build já falharam na mesma rodada** — vermelhos derivados de código que não compila acrescentam pouco ao fix_context. Quando o gatilho disparar, o A/B é esse skip vs status quo.

### E8 — Parser casa "2 errors" do javac
1. **Dor do usuário:** nenhuma direta — mas é da família falha-silenciosa no **gate de evidência**, o mecanismo mais sagrado do sistema (evidence=True com zero testes).
2. **Se nunca fizermos:** o rótulo mente em toda falha de compilação Java; o dia em que alguém escrever um invariante sobre `executed>0`, ele valida lixo.
3. **Caminho mais barato:** o proposto já é mínimo (~10-30 linhas + teste vermelho).
4. **Veredito: FAZER como proposto** — de carona na próxima rc que toque `services/validation`. Não vale rc própria; vale não esquecer (a família dele já custou dois dias, #60).

### E9 — Baseline por teste
1. **Dor do usuário:** nenhuma medida — "piora em suite já-vermelha passa batida até o L2" nunca aconteceu [não medido, só previsto].
2. **Se nunca fizermos:** o L2/diff continua sendo o vigia do caso; a limitação está documentada por escrito.
3. **Caminho mais barato:** nada a fazer é o barato certo aqui.
4. **Veredito: ADIAR até o caso mascarado ocorrer** — mensurável: 1 PR onde a piora em suite herdada só apareceu no review humano (ou depois). Honestidade sobre a assimetria do gatilho: o caso **maligno** (regressão mergeada sem ninguém ver) não gera evento — como no E3, há uma parcela de risco aceito aqui, documentada por escrito no próprio código do baseline.

### E10 — Pods após kill manual / espera humana
1. **Dor do usuário:** nenhuma direta; dor de operador: recursos do VPS (disk-pressure já aconteceu [histórico]).
2. **Se nunca fizermos:** o dado de hoje redefine o item duas vezes — o custo real não é o kill manual (backstop de 72h existe [código]), e a projeção "14 review_ready = 14 pods" também não se sustenta: **só 4 dos 14 têm pod** [ledger] — a maioria já perdeu o sandbox por algum caminho, o que corta a projeção de custo E levanta a pergunta mais interessante: se o fix cycle pós-review desses 10 precisar do sandbox, ou a recriação já funciona, ou já está quebrando em silêncio.
3. **Caminho mais barato:** correção da verificação sobre a premissa: a recriação existe (`sandbox_rebuilt`=4 [ledger]) mas **na config da VPS o checkpoint não sobrevive ao teardown** — o volume é emptyDir com `checkpointPvc.enabled=false` no chart, e o próprio código declara "re-cloning is the honest recovery… loses the turn's uncommitted work" (activities.py:400-412). Teardown em review_ready recuperaria só o que está commitado/pushado — o que, **em review_ready, é exatamente o que importa** (a PR existe). O custo real do teardown é a recriação (clone + install, minutos) no caminho normal de review feedback.
4. **Veredito: FAZER diferente, com diagnóstico antes do desenho** — passo 1: explicar por que 10 dos 14 review_ready não têm pod e se o fix cycle deles sobrevive (pode revelar um bug ativo da família falha-silenciosa); passo 2: só então decidir teardown-na-transição vs status quo, com o custo de recriação na mesa. O caso kill-manual: **MATAR** (o reaper de 72h é backstop suficiente para POC).

### E11 — Contabilidade de tokens
1. **Dor do usuário:** nenhuma hoje; dor de **decisão**: toda a economia do projeto (régua de vereditos deste review, inclusive) depende desse ledger, e 87% do gasto está em linhas anômalas [ledger].
2. **Se nunca fizermos:** o custo real por item segue subestimado (tester sub-metrado: 64 turnos a custo zero [ledger]) e qualquer análise de tokens é lixo; quando houver cobrança por cliente, a fatura não fecha.
3. **Caminho mais barato:** ler os campos de cache nos 2 write paths (~20-60 linhas, sem migração) + metrar re-execuções do tester. O proposto já é o barato.
4. **Veredito: FAZER como proposto** — cedo, como carona (não precisa de rc própria). É infraestrutura de decisão, não feature.

### E12 — Invariante do reauthor
1-3. n/a — entregue e vigiado [código + suite verde rodada neste review].
4. **Veredito: MATAR** — fechar como entregue na rc.52. O item era "confirmar"; está confirmado.

### G1'/G2/G3 — Preview (em voo)
- Fora de veredito de backlog (estão em execução), com um alerta de produto **[ledger]**: a aceitação ainda não aconteceu — zero preview `deployable` jamais ficou Available, e as degradações recentes (timeout do deployment, read-only FS) são exatamente a última milha. O critério do backlog (PR FE com dados, PR BE com health ok, kind no ledger) continua sendo o certo. Segundo alerta: **TTL 1h × revisão humana de 4h+** — o preview morre antes do revisor clicar (ver Lacunas).

### G4 — TTL/GC de namespaces
1. **Dor do usuário:** nenhuma da forma temida (disk-pressure) — o item está factualmente **entregue e ativo em produção** (CronJob de 10 min + TTL 1h + cap com eviction, tudo ligado na config da VPS [código verificado neste review]). Mas a verificação apontou a dor **inversa** com timing concreto: o reaper deleta o namespace **sem checagem de uso e sem evento no ledger** (preview-reaper.yaml:77-94; `expires_at` fixado na criação, nunca estendido) — com TTL 1h contra revisões de 3-4h+, o primeiro preview `deployable` que ficar Available **morre antes ou durante a revisão**, contaminando a própria métrica de aceitação do G em voo.
2. **Se nunca fizermos (o sucessor):** a aceitação do preview será julgada com links mortos.
3. **Caminho mais barato:** o próprio documento precifica — bump do TTL é 1 linha de values (decisão de operador: p.ex. 24-48h, o cap de 3 + eviction LRU já limita o custo); +1 `audit_emit` no reaper para o reap virar evento detectável.
4. **Veredito: MATAR como formulado** ("especificado, não implementado" está errado) — **e abrir o sucessor G4′ imediatamente, junto com a aceitação em voo**: TTL compatível com a janela de revisão + reap auditado. Não vai para "um dia": sem ele, a aceitação do G1'-G3 mede a coisa errada.

### G5 — G-1'' (workspace sem credencial)
1-3. n/a — decisão de operador já registrada (09/08, memória do projeto): risco token-no-pod aceito explicitamente para POC single-user; o item só existe como evolução condicionada a sair de POC, e o custo de espera em POC é nulo.
4. **Veredito: ADIAR até sair de POC** — nada mudou desde a decisão. Não tocar no G1' recém-entregue sem esse gatilho.

### G6 — Cluster de integração no CI
1. **Dor do usuário:** indireta — cada rc de preview quebrada em produção é mais um ciclo sem a "cara de produto" das PRs.
2. **Se nunca fizermos:** o padrão medido continua: 12 degradações em produção, várias de classe que um e2e de cluster pegaria (RBAC, read-only FS) [ledger] — o gatilho condicional do backlog disparou parcialmente.
3. **Caminho mais barato:** **não** é CI — é rodar o e2e existente contra k3d **local** como passo de DoD de toda rc que toque preview (o teste já existe; o skip desliga sozinho com o cluster de pé). CI com cluster = flake de infra + minutos por push, para um teste que só precisa rodar por rc de preview.
4. **Veredito: FAZER diferente** — e2e k3d local obrigatório no DoD de rcs de preview (uma linha de runbook + eventualmente um alvo make). CI: **ADIAR até o processo local falhar em pegar uma regressão** que chegue à VPS.

### F1 — H1: executor do Coder
1-2. A hipótese está respondida: 18/20 PRs convergem em ≤2 rodadas **sem** executor; as 2 exceções (PR #17 = vão de contrato cross-repo; PR #15 = pinça spec×JSDOM) não seriam resolvidas por executor — e com 1 vCPU no sandbox, rodar a suite in-turn custaria o mesmo wall-time do L1 [ledger + código, verificados]. Nota de viés que a verificação apontou: "18/20" condiciona em quem **chegou** a PR; os 9 mortos por cap ficam fora do denominador. A atribuição deles fecha o caso em vez de abri-lo: 5 dos 9 morreram tocando gates ERROR de infra (o caso do E2) e os Java eram as 4 camadas de ambiente documentadas — nenhum é um loop de build que um executor do Coder converteria.
3. n/a — o item **era** a query, e ela foi rodada (e estendida aos mortos) neste review.
4. **Veredito: MATAR** — enterrar H1 formalmente com este dado. Gatilho de reabertura registrado no script de gatilhos da síntese: padrão novo de loops build-fail em itens que morrem no cap **sem** gates ERROR.

### F2 — Migração data-test no cliente FE
1. **Dor do usuário:** indireta — seletor posicional é a matéria-prima das pinças (a família max-w-[200px] custou um item inteiro [histórico]).
2. **Se nunca fizermos:** o resíduo medido (6 nth-child no Home.page + 1 spec unitária) fica como mina — dispara quando um item DSE mexer naquelas colunas.
3. **Caminho mais barato:** escopo cirúrgico — só os posicionais puros (Home.page.ts + ia-codes-table.spec) e não os `.nth()` de indexação legítima. E a forma barata de execução é **submeter como item DSE no próprio testbed** (dogfood: mede o sistema enquanto limpa a mina).
4. **Veredito: FAZER diferente** — escopo reduzido, executado como work item DSE. Baixa prioridade; oportunista.

### F3 — Fatos falsos em grounding
1. **Dor do usuário:** nenhuma — dor de **agente**: grounding com fato falso envenena toda sessão futura (é a família falha-silenciosa aplicada a contexto).
2. **Se nunca fizermos:** o fato segue em 3 lugares (run-state.md:62, CLAUDE.md:40 como **regra de conduta**, e a memória do projeto) — e a regra "pode ser flake, rerun" é exatamente o tipo de instrução que faz um agente descartar um vermelho legítimo.
3. **Caminho mais barato:** 1 medição (a flakiness da suite control-plane sob contenção — estabelece a verdade) + correção dos 3 lugares com proveniência. Uma hora de trabalho.
4. **Veredito: FAZER como proposto, com o escopo ampliado ao CLAUDE.md:40** — o backlog só via o run-state.md; a réplica na instrução de projeto é a parte perigosa.

---

## PASSE 3 — Síntese

### Os 5 movimentos de maior alavanca por custo, na ordem

1. **A1+A4 — o fix de 1 linha do botão Approve** (`source_ref - 'bot_ts'` + o vermelho com a ordem de produção). Medido falhando **hoje**: 2 cliques, 2 perdidos, aprovação por console. Destrava tudo que depende de clique em thread com irmãos — inclusive o bloco 2 e o C1. Custo: horas.
2. **O bloco do parque: B3 + B4 + A6-Slack + E5** — "o parque pergunta, e a resposta é um botão". O dossiê já existe (13 montados, 13 descartados); a rota não existe; os 10 destravamentos da história foram SSH. Este bloco é o critério de aceitação da Fase 1 ("caso 3 sem nenhum SSH") escrito como código: ~200-300 linhas somadas, quase tudo sobre plumbing existente (approval_blocks, dispatcher, plumbing do reauthor). Inclui a linha do STATUS_MAP que desavermelha a suite do console — essa entra **primeiro** (é dívida de hoje). Três caronas de ~1 linha cada que pertencem ao bloco: a A5-config (allowlist com os principais do operador — pré-requisito do gate de autorização dos vereditos), o reason `client_spec_obsolete` no escalate do parque (o contador do gatilho do C5) e a classificação de `tester_reauthor_missed` como evento de atenção no mapper (a condição de honestidade do kill do E4).
3. **B7 + B5 + B6-mínimo — o sistema para de sumir e o painel para de mentir**: post no único terminal mudo (2-5 linhas), aviso do sweep, filtro do last_event, prefixo de repo nos irmãos. ~40 mortes mudas no ledger; custo total ~60-100 linhas.
4. **E11 — fechar a régua de custo** (~20-60 linhas): 87% do gasto em linhas anômalas e o tester sub-metrado. Não é feature — é a infraestrutura de decisão de todas as outras fases. Carona, cedo.
5. **C1-(b) — contrato via plano do primário, irmão espera o plano**: a maior alavanca de dinheiro por item multi-repo (6× medido entre rodada dirigida e cega), na forma barata (dispatcher segura o despacho; PlanArtifact como grounding) em vez do estágio novo em região de replay. Depois do movimento 1, como a memória já registrava.

Sequência recomendada: 1 → 2 → 3 são a Fase 1 real (2 e 3 podem ser a mesma rc ou duas); 4 entra de carona na primeira rc que tocar validation/model-gateway; 5 é a Fase 3 antecipada que o operador já decidiu — só muda a **forma** (b em vez de estágio pré-fan-out). Logo atrás dos cinco: **E2** (que a verificação promoveu de ADIAR a FAZER — rodada só-ERROR não compra retry) entra como rc própria após o bloco, e **G4′** (TTL × janela de revisão + reap auditado) entra junto com a aceitação do preview em voo, senão a aceitação mede links mortos. O preview (G) segue em voo com o adendo de DoD (G6-lite).

**Executor dos gatilhos (sem isto, todo ADIAR deste documento é decorativo):** os gatilhos de reabertura de E2(residual)/E6/E9/F1/C5 são queries — e a verificação demonstrou o modo de falha ao flagrar este próprio review afirmando "nenhuma ocorrência" de um gatilho sem rodar a query (E2; rodada, ela acusou). Resposta barata: um `reopening-triggers.sql` único (~30 linhas), rodado por CronJob ou anexado ao post de morte que o B7 vai construir. Os ADIARs sem query possível (E3, metade do E9) estão marcados no texto como **risco aceito** — decisão, não espera.

### O que o Produto matou ou reformulou

| Item | Era | Virou | Por quê |
|---|---|---|---|
| B2 | Verificar update in-place Slack/Jira | **MORTO** | Já existe nos 3 canais, mesmo writer [código] |
| E12 | Confirmar invariante do reauthor | **MORTO** (entregue) | Teste existe, nomeia o invariante, suite verde [rodada neste review] |
| F1 | Query de encerramento do H1 | **MORTO** (executada) | 18/20 PRs ≤2 rodadas sem executor; exceções não são caso de executor [ledger] |
| C4 | Binding real / base_branch morto | **MORTO** | As duas evidências falsificadas pelo código de hoje; sem dor medida |
| E4 | Tratamento geral de deriva | **MORTO** | Política estreita é deliberada; miss vira evento auditado; 0 casos pós-rc.51 |
| G4 | "TTL/GC especificado, não implementado" | **MORTO** (entregue) + sucessor **G4′** imediato | 3 mecanismos ativos na config da VPS; o problema real é o inverso — TTL 1h mata o preview antes da revisão e o reap não é auditado |
| E2 | H5 completo (Tester+L1) | **FAZER diferente** — só o lado L1, escopo mínimo | A 1ª versão deste review o adiou; a verificação rodou a query do gatilho e ele **já tinha disparado** (wi_8c46e17e: cap inteiro em rodadas sast=ERROR) |
| C5 | Porta 4 no plano | **ADIADO** com gatilho + contador criado | 0 casos medidos; o único candidato provou o oposto; relaxa guard de segurança; o reason `client_spec_obsolete` no bloco do parque torna o gatilho contável |
| C3 | Ordem BE→FE | **ADIADO**/fundido | C1-(b) absorve a ordem de planejamento; reabre só se o contrato não bastar |
| E6/E7/E9 | Robustez por previsão | **ADIADOS** com gatilhos nomeados | Nenhum mordeu desde os fixes da semana; queries registradas no script de gatilhos |
| E3 | Renames cegam o parque | **ADIADO como risco aceito** | O gatilho é inobservável por construção (parque que não dispara não emite nada); carona autorizada se scoped_git for tocado |
| A5 | Identidade cross-canal | Config agora, merge depois | Allowlist multi-principal cobre o POC por 0 código; link é decisão de segurança |
| A2 | Um item, duas causas | Dividido | Filtro de bot agora (pré-req do A3); requester/identidade → A5 |
| B1 | 5 etapas por canal | Só traduzir os vazamentos | Os 12 status já são humanos; colapsar sem usuário real é redesenho no escuro |
| B8 | Listener de terminate | Probe do Temporal no sweep | Mata a cegueira de parque E o atropelo de parque legítimo (4 medidos) de uma vez |
| C1 | Planner pré-fan-out | Irmão espera o plano do primário | Mesmo grounding, fração da superfície, fora da região de replay do intake |
| D2 | Estágio automatizado de sonda | Script de operador primeiro | 80% do valor (conhecimento antes do erro); automatizar antes de 2-3 usos reais é o túnel de novo |
| D3 | Capturar vermelho→verde | Diagnosticar os writers mudos, depois decidir | A ideia "ligar o pipeline existente" caiu na verificação: skill_episode tem 1 linha all-time — ligar o gatilho materializaria zero candidatos |
| E10 | Kill manual deixa pod órfão | Sandbox não vive a espera humana | 72h de backstop cobre o kill; os 4 pods de hoje são de review_ready [ledger] |
| G6 | Cluster k3d no CI | e2e k3d local no DoD de rc de preview | O teste já existe; CI adiciona flake e custo para um teste por-rc, não por-push |
| F2 | Varrer todo o resto | Só posicionais puros, como item DSE | Metade do "resto" é indexação legítima; dogfood mede o sistema de graça |
| F3 | Varrer OVERNIGHT.md | + CLAUDE.md:40 | A mesma afirmação sem proveniência é **regra de conduta** na instrução do projeto |
| A3 | Orientação in-channel GitHub+Jira | GitHub após A2-filtro; Jira adiada | Sem o filtro, o bot re-ingere a própria orientação; Jira sem uso real medido (gatilho: 1 recusa auditada) |
| A6 | Item avulso de vereditos | Bloco único B3+B4+A6+E5 | Pergunta sem resposta e resposta sem pergunta são o mesmo trabalho; separados criariam duas UIs para a mesma decisão |
| B6 | Irmãos se identificam | Só o prefixo de repo; variante "uma mensagem por grupo" **MORTA** | A variante grande custa 10× e briga com a correlação por bot_ts existente |
| C2 | Link entre PRs do grupo | Uma linha no corpo da PR + segundo passe | Sem ordem entre irmãos, simetria garantida não existe; o corpo da PR carrega o aviso que hoje vive na cabeça do operador |
| E1 | Preflight bloqueia drift | Só relatório informativo; bloqueio adiado | Os pins-exceção de hoje (rc.56 vs âncora rc.53) fariam um check ingênuo bloquear deploy legítimo; 1 incidente medido não paga política de exceções |

### Lacunas — o que um usuário real sentiria falta e nenhum item cobre

Jornada: pedir → acompanhar → ser perguntado → revisar → aprovar → ver funcionando.

1. **Rejeição muda de steering (o "ser perguntado" quebrado no retorno).** Quando alguém responde e a resposta é rejeitada por autorização, o silêncio é total — em **todos** os canais, inclusive Slack. Medido hoje: o principal usr_2756c382… respondeu clarification 2× na janela do caso 3 e foi descartado sem nenhum sinal [ledger]. Se é um segundo humano ou um principal não-linkado do operador, o ledger não sabe dizer — e essa indecidibilidade é o argumento do A5 (a pergunta ao operador está na ação do A5). Em qualquer das hipóteses, a pessoa achou que respondeu. A3 cobre só recusa de *pedido* não-task; nenhum item cobre "sua resposta foi ignorada e o porquê".
2. **O usuário-alvo não tem como revisar.** A jornada assume "revisar → aprovar", mas o artefato de revisão é uma PR de código — que o usuário-alvo, por definição, não lê. O preview (G) é o artefato de revisão dele, e **nada liga o preview a uma decisão**: não há "vi o preview, aprovo" que dispare qualquer coisa. Hoje o único veredito in-channel é o de *plano* (antes do trabalho); o de *resultado* não existe em nenhum item. Enquanto isso não existir, o merge é sempre um ato de fé de um dev.
3. **O preview morre antes do revisor chegar.** TTL de 1h [config da VPS] contra espera de revisão medida em 3-4h+ hoje (e dias, realisticamente). O link do preview na PR estará morto na hora do clique. O re-trigger existe (`human_request`, 3 medidos) mas não há botão/afordância para o revisor pedi-lo. A metade "TTL + reap auditado" já tem dono e prazo — é o **G4′** do Passe 2, junto com a aceitação em voo; a metade "afordância de re-trigger para o revisor" continua sem item.
4. **Nenhuma noção de expectativa de tempo.** "🔨 Desenvolvendo" não diz se faltam 5 minutos ou 5 horas — e as durações reais existem no ledger (FE ~25-40 min típicos). Um "costuma levar ~30 min" na mensagem de status custaria pouco e mataria a ansiedade que hoje só o operador (que conhece os números) não sente. Nenhum item de B cobre.
5. **Custo invisível para quem pede.** `budget_consumed` existe no ledger; o solicitante nunca vê "isso custou US$ 3". Irrelevante para o POC single-user, relevante no primeiro cliente pagante. Registro, não urgência.

### Conflitos com a priorização atual das fases

(Referência: a tabela de fases no topo de `docs/BACKLOG-DSE.md`, priorização do operador de 09/08 — Fase 1: projeto A com rc de A1+A3 primeiro e `/goal` para o resto; 1.5: preview em voo; 2: projeto B com A4+B2+B5 em bloco; 3: C com C1-mínimo primeiro; 4: D inteiro; 5: E por oportunidade + F1.)

1. **B3/B4 estão na Fase 2, mas são metade do A6 (Fase 1).** O veredito pelo canal sem a pergunta renderizada é um botão sem contexto; a pergunta sem rota de resposta é o status quo. O bloco do parque (B3+B4+A6+E5) deveria ser o `/goal` da Fase 1, com a parte B2/B5 da Fase 2 vindo depois. Argumento: o critério de aceitação da própria Fase 1 ("caso 3 sem SSH") é inatingível sem B3/B4.
2. **E5 na Fase 5 ("por oportunidade") contradiz o A6 na Fase 1.** Se o canal de vereditos nascer só com retry/reauthor, o primeiro parque de spec própria ruim volta a exigir SSH — exatamente o que a Fase 1 quer matar. O discard precisa nascer no mesmo botão.
3. **E11 na Fase 5 está tarde demais para o que ele é.** Não é robustez — é a régua que as decisões das Fases 2-4 vão usar (inclusive "quanto custou o C1?"). Carona imediata.
4. **Dentro da Fase 1, a ordem "A1+A3 primeiro" precisa de um passo no meio: A2-filtro-bot antes do A3.** Senão cada orientação postada no GitHub é re-ingerida pelo próprio bot e o contador de lixo dobra [código: o filtro existe e não é aplicado no webhook].
5. **G6-lite pertence à Fase 1.5, não ao "condicional".** A aceitação em voo já degradou 12× em produção, parte em classes que o e2e local pegaria. Uma linha de DoD ("rc que toca preview roda o e2e k3d local") custa quase nada e o gatilho do backlog já disparou parcialmente.
6. **Sem conflito na Fase 3 (C) e 4 (D) como ordenadas** — mas com forma alterada: C1 na forma (b) (irmão espera o plano) e D como script+pipeline-existente antes de qualquer estágio automatizado. A tese do operador ("D é a eliminação da skill manual") sobrevive — pela metade barata dela.

---

*Método: Passe 1 produzido por verificação direta de código (arquivo:linha conferidos em f14962b) e queries read-only no Postgres de produção; Passe 2 e 3 são julgamento sobre esse registro; o conjunto passou por verificação adversarial (3 revisores independentes — fatos, vereditos, completude) e as quedas foram incorporadas com a marca "a verificação derrubou/exigiu" no próprio texto (E2, D3, A5, E10, B8, C1, G4). Divergências entre backlog e realidade estão marcadas item a item — 10 itens tinham pelo menos uma evidência envelhecida ou falsificada pelo código de hoje (A2 contagem, A4 "nunca testado", B2, B7 "fica implementing", B8 "não existe", C4 ambas, D3 "não usa" — e o acúmulo afirmado não existe, E10 "órfãos" são TTL de 72h, G4 "não implementado", C1 "Planner antes do fan-out" pressupõe ponto de workflow inexistente). A divergência código-vs-comentário do B6 (docstring que promete uma mensagem por grupo) ficou fora dessa contagem por ser mentira do comentário, não do backlog.*
