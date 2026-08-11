# DSE — fase 1

Monorepo Python: `packages/` (bibliotecas), `services/` (13 componentes), Temporal + Postgres + Vault.

## Comandos

Nada ativa o venv automaticamente, e `ruff`/`mypy`/`pytest` só existem dentro dele.
**Sempre prefixe, ou exporte o PATH uma vez por sessão:**

```
export PATH="$PWD/.venv/bin:$PATH"
```

- lint (compile + ruff + mypy ratchet): `make lint` — 1,1s, é o job `quality` do CI
- suites sem docker: `python scripts/test_matrix.py --group contracts --group tooling --group packages` — 5,2s
- uma suite: `python scripts/test_matrix.py --suite services/orchestrator --reports-dir test-results`
- suite completa (precisa de `make up`): `make test`
- listar suites: `python scripts/test_matrix.py --list`

Cada suite roda em processo próprio e escreve `test-results/<suite-com-hifens>.xml`.
Nunca chame `pytest` direto na raiz: cada componente tem seu próprio `tests/conftest.py`
e um único processo resolve o primeiro para todos.

## Invariantes

- mypy gateia só `packages/contracts`, `dse_audit`, `dse_identity`. `services/orchestrator`
  tem baseline de 426 erros; não trate erro herdado dele como erro seu.
- Comando git do DSE nunca executa código do repositório do cliente. Todo call site novo
  precisa fixar `core.hooksPath` num diretório vazio — já foi corrigido em três call sites
  separados (#46, hygiene, #52) porque a regra vivia em cada um deles.
- Ids de work item irmãos são hasheados (`sha256(event_id:repo)`), nunca sufixados:
  `pod_name_for` trunca em 63 e o id já tem 67 chars.
- `awaiting_human_review` é sucesso, não travamento. O DSE nunca aprova o próprio trabalho.
- **Não existe posse de teste** (decisão de operador, 2026-08-10). O DSE altera qualquer
  teste — inclusive as specs que o próprio laço escreveu — e a supervisão é o diff da PR.
  Saíram junto: o revert pós-turno, o oráculo de autoria (`-dse`, subject de commit), o
  rename guard, o parque `spec_conflict` e o reauthor. Sobrou UMA autoria: a porta 5, em
  que o Tester conserta a spec que ele acabou de escrever e que nem carrega. Laço que não
  converge termina só de um jeito: `escalated`, pelos freios de sempre (teto de tentativas,
  `coder_not_converging`, duplo no-op, diff vazio, teto de gasto).
- `repo_bindings` não é "os repositórios do tenant" — tem uma linha por binding.
  O conjunto candidato é `repo_bindings UNION repo_profiles`.

## Pegadinhas

- `tests (control-plane)` é sensível a contenção: usa um Temporal time-skipping cujo relógio
  só avança em janelas ociosas. Falha num teste diferente a cada vez. Rerun com fila vazia passa.
  Isso **não** autoriza tratar outra falha como ambiente — reproduza no commit anterior.
- Um work item iniciado por `temporal workflow start` não cria linha em `work_items`.
  Espera keyed em `work_items.status` trava para sempre; espere o status do WORKFLOW.
- `_tail(stdout or stderr)` descarta stderr sempre que stdout é não-vazio — foi assim que
  todo gate L1 publicou a evidência errada por dois dias (#60). Cuidado com esse `or`.
- `--suite services/orchestrator` **não roda a suite inteira**: os arquivos de
  `SUITE_SHARDS` (`test_plan_approval_timeout.py`, `test_phase4_merge_base_and_learning.py`,
  `test_iteration_caps_debounce.py`) só saem em `--group control-plane-slow`. Verde no
  `--suite` com o CI vermelho nesses três já aconteceu. Ao tocar no orchestrator, rode os
  DOIS grupos: `--group control-plane --group control-plane-slow`.

## Definição de pronto

`make lint` verde e a suite da área tocada verde, com o `test-results/<suite>.xml` no disco.
Correção de bug: o teste de regressão é escrito, roda **vermelho**, e é **commitado vermelho**
antes do fix — daí em diante qualquer mexida nele aparece no diff.
