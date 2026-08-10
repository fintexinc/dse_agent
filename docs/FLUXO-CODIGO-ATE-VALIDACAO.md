# Do pedido ao veredito: como o DSE escreve e valida código hoje, e por que ele não converge

**Status:** diagnóstico, não proposta de implementação
**Base de evidência:** ledger de produção (24 h e histórico completo) e leitura do código em `main` (`d3b3dc1`), com 31 agentes mapeando em paralelo e refutação adversária sobre cada hipótese
**Escopo:** o caminho `Planner → Coder → Tester → L1 → PR`, e apenas ele

---

## 1. O número que define o problema

> **O laço de correção nunca convergiu. Nenhuma vez, na história inteira do sistema.**

As **14 pull requests** que o DSE já produziu, de 2026-07-24 a 2026-08-06, têm
todas a mesma assinatura no ledger: **exatamente uma** execução de
`l1_pipeline_run` e **zero** eventos de `l1_failed_retrying`.

Nenhum work item que entrou no laço Coder→L1 jamais saiu com uma PR.

O sistema não é lento a convergir. Ele **só entrega quando acerta de primeira**.

### As últimas 24 horas

| | |
|---|---|
| work items admitidos | 26 |
| turnos de modelo pagos | 77 |
| execuções do gate L1 | 31 |
| execuções do L1 que passaram | 3 |
| **pull requests produzidas** | **0** |
| **custo** | **US$ 97,74** |

Uma única frase — *"On the reports dashboard, show at a glance whether each
report is still in progress or finished"* — foi submetida **12 vezes** como 12
work items independentes. Juntos consumiram **38 turnos de Coder, 27 execuções
de L1, ~7 h de relógio e US$ 74,88**. Todos morreram: 3 no teto de retentativas,
4 em `tester_retry_cap_exhausted` antes de o L1 rodar, 5 mortos à mão.

### O que reprovou

| gate | rodadas | | mensagem | rodadas |
|---|---|---|---|---|
| `build` | **20** | | `build failed (exit=1)` | **19** |
| `test` | 18 | | `summary: 403 errors` | 8 |
| `lint` | 8 | | `secret scanner failed (exit=1)` | 5 |
| `typecheck` | 7 | | `4 type error(s) in the files this change touched` | 4 |
| `secret_scan` | 5 | | `lint could not run: killed (exit=134)` | 4 |
| `l1_manifest` | 1 | | `lint could not run: killed (exit=137)` | 3 |

Uma mensagem — `build failed (exit=1)` — respondeu por **19 das 31 rodadas**.
Não são dezenove problemas: nas execuções em que li o detalhe era sempre a mesma
classe de erro de tipagem de template, e três vezes seguidas o **mesmo** erro,
literalmente na mesma linha.

---

## 2. Como o fluxo funciona hoje

### 2.1 A sequência

```
intake (Slack/Jira/GitHub)
   └─ ingest gateway  →  resolve tenant, resolve repo (cascata determinística)
        └─ WorkItemLifecycleWorkflow  (Temporal)
             ├─ route_repos            ← modelo, se a cascata não decidiu
             ├─ planner_turn           ← lê o repositório, produz PlanArtifact
             ├─ provision_sandbox      ← 1 Pod por work item, clone real
             ├─ run_coder_turn         ← escreve código de produção
             ├─ run_tester_turn        ← escreve testes, roda os que escreveu
             ├─ run_l1_pipeline        ← 9 estágios, em série
             └─ finalize_pr
```

Se o L1 reprova, o fluxo volta ao Coder com `fix_context`. É esse laço que nunca
fechou.

### 2.2 Custo medido de uma rodada

VPS de 4 vCPU, média de 31 execuções:

| etapa | tempo |
|---|---|
| `provision_sandbox` | ~48 s |
| `run_coder_turn` | ~3 min |
| `run_tester_turn` | ~2 min |
| `run_l1_pipeline` | **~8,7 min** (520 s médios) |
| **rodada completa** | **~12 min** + um turno pago |

Dentro do L1 — e **99,8 % do tempo está em quatro estágios**:

| estágio | ordem | tempo médio |
|---|---|---|
| `lint` | 3º | 79 s |
| `typecheck` | 4º | 14 s |
| `test` | 5º | **297 s** |
| `build` | 6º | 42 s |
| `sast`, `secret_scan`, `plan_compliance` | 7º–9º | < 15 s somados |

### 2.3 O que o Coder pode fazer — a peça central

O caminho de produção, traçado ponta a ponta:

```
run_coder_turn (activities.py:716)
  → _build_substrate (activities.py:545)
      → RemoteSubstrate            ← produção FORÇA isto (runtime_profile.py:82-84)
          → KubernetesSandboxDriver (kubectl exec)
              → agent-runner _run_claude_agent (executor.py:70)
                  → sdk.ClaudeAgentOptions(allowed_tools=req.allowed_tools)
```

`RemoteSubstrate.run_turn` (`remote_substrate.py:106-121`) monta o
`AgentTurnRequest` **sem passar `allowed_tools`**. O campo assume então o default
do contrato (`agent_turn.py:51-55`):

```python
# File-editing-ONLY toolset (P1) — git/PR/bash never get in.
allowed_tools: list[str] = ["Read", "Write", "Edit", "Glob", "Grep"]
```

**O Coder não pode executar comando nenhum.** Não pode rodar `npm run build`,
nem `tsc`, nem um teste. A verificação de 42 segundos que teria pego o erro
dominante é **fisicamente indisponível** ao único ator capaz de corrigi-lo.

A restrição é **deliberada e documentada** — `executor.py:8-10` diz *"P1: o
substrato SÓ edita arquivos. Nenhuma ferramenta de git/PR/bash entra no toolset;
commit/push permanecem determinísticos no worker"*. O princípio é sólido: nenhuma
decisão de fluxo por um LLM. **Mas ele nunca foi pesado contra a convergência.**

> Nota de método: minha primeira leitura concluiu o oposto — "o Coder pode, só
> não é mandado". Eu havia inferido da ausência de um `CoderToolset` em
> `toolsets.py` e do `run_turn` sem argumento de toolset. Estava errado: a
> restrição do Coder vive noutro mecanismo, o allowlist do SDK, e a classe que
> eu li (`ClaudeAgentSubstrate`) **não é a que roda em produção**.

Comparando os quatro agentes:

| agente | ferramentas | onde |
|---|---|---|
| Planner | só leitura | `PlannerToolset` |
| Tester | leitura + `run_tests` + escrita em caminhos de teste | `TesterToolset` |
| Reviewer | `read_plan` / `read_diff` | `ReviewerToolset` |
| **Coder** | **Read, Write, Edit, Glob, Grep** | default do contrato |

O Tester **tem** um executor (`run_tests`) — e nem o escolhe: código
determinístico o acrescenta ao script depois que o modelo devolve os arquivos
(`activities.py:2324`). O Coder não tem equivalente.

### 2.4 Por onde o conhecimento do repositório chega — e por onde não chega

| canal | Planner | **Coder** | Tester |
|---|---|---|---|
| `AGENTS.md` | ✅ | ❌ | ❌ |
| árvore do repositório | ✅ | ❌ | ❌ |
| `.claude/skills/` | índice (corpos removidos) | **❌ string vazia** | ✅ |
| teste de exemplo | ❌ | ❌ | ✅ (alfabético) |
| `fix_context` do L1 | ❌ | ✅ | ❌ |

Duas linhas dessa tabela custaram o dia:

**`AGENTS.md` não chega ao Coder nem ao Tester.** Só ao Planner
(`_repo_docs_for_planner`, activities.py:1132-1179). Escrevi a regra do
`provideMockStore` lá de manhã e o mesmo erro voltou duas vezes.

**A nota de skills do Coder é vazia em produção.**
`workspace_skills_note(workspace_dir)` (`skill_files.py:143-149`) faz
`Path(workspace_dir)/.claude/skills` — leitura no sistema de arquivos **do
worker**. Mas `k8s_driver.workspace_is_host_visible` é `False`: o workspace vive
no volume do Pod. O diretório não existe no worker, `ws.is_dir()` é falso, e a
função devolve `""`. O Tester escapa porque a nota dele é montada com um comando
executado **dentro** do Pod (`activities.py:2184`).

Consequência direta: as três skills que escrevi hoje — `provideMockStore`,
`setInput`, tipagem do PrimeNG — chegaram ao Tester. **A do PrimeNG, escrita
especificamente para o Coder, foi entregue a ninguém.**

**O Coder também roda com `system_prompt` vazio.** `executor.py` deixa o campo
`None`, o que o SDK converte em `--system-prompt ""`.

Somando: em produção o Coder escreve código com o texto da tarefa, as restrições
do plano e o `fix_context` — **sem AGENTS.md, sem árvore do repositório, sem
skills, sem system prompt e sem poder executar nada.**

---

## 3. Hipóteses para o loop

### H1 — O Coder não pode verificar o próprio trabalho. **(provado)**

Já detalhada em §2.3. O erro dominante custa 42 s de `npm run build` e é
impossível de detectar de dentro do turno.

**Isto não é "o Coder não foi instruído a verificar".** Instruir não resolveria:
a ferramenta não existe no toolset. Qualquer correção aqui exige uma decisão
deliberada sobre a fronteira P1 — dar ao Coder um executor **escopado** (um
`run_build` no molde do `run_tests` do Tester, apendado por código
determinístico, sem bash geral) preserva o princípio e remove a cegueira.

**Como matar a hipótese.** Dar esse executor e medir quantas rodadas continuam
reprovando em `build`. Se continuarem, a hipótese está errada.

---

### H2 — O L1 não tem falha-rápida, e o gate mais decisivo é o penúltimo. **(provado)**

`pipeline.py:120-134` executa os nove estágios incondicionalmente e só ao final
calcula `passed = all(f.passed for f in findings)`. Não há `return` antecipado
em lugar nenhum.

A ausência de falha-rápida é pinada por um teste de regressão que cita "P6" —
mas P6, como `CONVENTIONS.md:153` o define, é sobre **falhar limpo numa
fronteira**, não sobre rodar todos os gates. **A ordem em si não tem
justificativa em lugar algum**: é a ordem em que o docstring do pacote de
trabalho os lista (`pipeline.py:1`).

Aritmética: `build`, que responde em 42 s e carrega o erro real, está agendado
atrás de ~390 s de `lint + typecheck + test`. Uma rodada não consegue saber que
falhou antes do segundo ~580 de um pipeline de ~600 s.

Numa ordem barato-e-decisivo-primeiro (`typecheck 14 → build 42 → lint 79 →
test 297`), o mesmo erro apareceria em **56 s** — **~5,5 min por rodada
reprovada**, e houve 28 delas em 24 h.

Complementar a H1, não alternativa: H1 evita a rodada, H2 barateia a que
acontecer.

---

### H3 — O canal de conhecimento do Coder está quebrado, não apenas ausente. **(provado)**

Detalhado em §2.4. A distinção importa: um canal **ausente** se resolve
escolhendo onde escrever; um canal **quebrado** faz a escrita parecer que
funcionou. Passei o dia escrevendo regras para o Coder num lugar que ele não lê,
e o sintoma — o mesmo erro voltando — era indistinguível de "o modelo ignorou a
regra".

Não encontrei nenhum mecanismo pelo qual o sistema registre o que aprendeu numa
rodada reprovada. O aprendizado é 100 % manual, e hoje ele nem chegava ao
destino.

---

### H4 — O teste de exemplo dado ao Tester é escolhido por ordem alfabética. **(provado)**

`activities.py:2160`: `for candidate in sorted(existing)[:3]`. Lexicográfico
sobre todo caminho de teste do repositório, **sem referência à tarefa, ao plano
ou ao diff** — enquanto o prompt manda *"use EXATAMENTE o runner e o estilo do
TESTE EXISTENTE mostrado abaixo"*. Ele copiou fielmente um `TestBed` de um
componente que não injeta `Store`.

O vizinho do arquivo alterado seria a escolha certa e já está disponível: o diff
é lido no mesmo contexto.

---

### H5 — Falha de infraestrutura conta como reprovação de código. **(forte)**

Das 31 rodadas: **7** foram OOM do lint (exit 134/137), **5** foram
`secret scanner failed (exit=1)` — nunca investigado —, **1** foi um manifesto
válido rejeitado por um timeout grande demais.

O `_infra_failure` já classifica parte disso como `ERROR` em vez de `FAIL`, mas
o workflow trata igual: `coder_retry_count += 1` e mais uma rodada. O plano §8.5
já prescreve o correto — *"`environment` e `dependency` repetem somente a
Activity afetada; não entram automaticamente no Coder"* — e não está
implementado.

**Treze das 31 rodadas** foram gastas com problemas que não eram do código.

---

### H6 — O orçamento de retentativa é um contador só, nunca zerado. **(provado)**

Não existe `automated_repair_count` no código — só no documento de plano. O que
existe é `coder_retry_count`, incrementado em **seis lugares**, **nunca zerado**,
sobrevivendo a `continue_as_new`. Um item que gastou o orçamento no laço de
implementação chega ao laço de revisão já sem saldo, e a mensagem de
escalonamento diz "repetidas" para o que foi a primeira tentativa.

---

## 4. O que foi corrigido hoje, e o que isso ensinou

| conserto | efeito medido |
|---|---|
| `detail` do L1 lia o fluxo errado (`stdout or stderr`) | o motivo real passou a aparecer — foi o que permitiu este diagnóstico |
| resumo do `test` era do pytest, e o jest inverte a ordem | `summary: 275 passed` numa reprovação virou a contagem verdadeira |
| Tester rodava a suíte inteira | turno de ~9 min → ~2 min |
| turno do Coder sem mudança re-armava os gates | 51 min medidos de rodadas idênticas, cortadas |
| Tester não podia reparar o próprio spec | acumulava cópias `-dse`; posse agora é pergunta ao git |
| roteador desistia num 502 de segundos | item parava esperando humano; agora retenta |

**A lição.** Todos eram defeitos reais e nenhum ataca H1, H2 ou H3. Eu estava
melhorando o *diagnóstico* e o *controle do laço* enquanto a causa do custo
estava em **o que o Coder pode fazer e o que ele consegue saber**.

E a ironia registrada: o conserto do `detail` foi o que tornou este documento
possível. Sem ele, `build failed (exit=1)` continuaria sendo a única coisa
visível.

---

## 5. O que eu não sei

1. **Por que o `secret_scan` reprovou 5 vezes.** Nunca olhei.
2. **Se o `test` de 297 s pode cair com segurança.** Dentro do gVisor `nproc`
   devolve 3, o jest cai em execução sequencial; medi 219 s em fila contra 153 s
   com dois workers, mas com três o cgroup mata a suíte.
3. **Qual o efeito real de `allowed_tools` vs `tools` no SDK.** A documentação
   do SDK diz que `allowed_tools` é a lista de **auto-aprovação**, não a de
   disponibilidade — para restringir disponibilidade seria `tools`, que o DSE
   nunca define. O efeito prático é o mesmo (sem `can_use_tool`, nada aprova o
   Bash), mas o mecanismo é negação-na-chamada, não ausência. O teste de
   conformidade afirma a coisa errada sobre isso.
4. **Se um executor escopado cabe no orçamento do turno.** O turno tem
   `max_turns = 8`; um build de 42 s por turno parece aceitável, mas não medi.

---

## 6. Ordem de ataque sugerida

Por razão custo-benefício:

1. **H3 — consertar o canal de skills do Coder.** É de longe o mais barato: a
   nota precisa ser lida **dentro do Pod**, como a do Tester já é. Sem isso,
   qualquer conhecimento que se tente dar ao Coder é escrito no vazio — e não dá
   para avaliar nenhuma outra hipótese enquanto o canal mente.
2. **H1 — dar ao Coder um executor escopado.** Um `run_build` no molde do
   `run_tests` do Tester, acrescentado por código determinístico. Preserva P1 e
   é a única mudança que *elimina* rodadas em vez de barateá-las.
3. **H2 — reordenar o L1 e falhar rápido.** ~5,5 min por rodada reprovada, sem
   perder um gate sequer: muda a ordem, não o conjunto.
4. **H5 — separar falha de infraestrutura de veredito sobre o código.** Já
   escrito no plano §8.5, não implementado. Recupera 13 de 31 rodadas.
5. **H4 — escolher o teste de exemplo pela proximidade com o diff.**
6. **H6 — separar os orçamentos de retentativa.**

**A não fazer agora:** o `ChangeGroupWorkflow` do plano multi-repo. Ele está
correto no que propõe, mas parte da premissa de que a entrega **de um**
repositório converge. O ledger diz que ela nunca convergiu — nem uma vez, em
duas semanas. Construir a coordenação por cima multiplicaria por dois uma
superfície de falha que ainda não fecha uma vez.
