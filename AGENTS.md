# AGENTS.md — Guia de Arquitetura e Desenvolvimento para Agentes

Este documento orienta agentes autônomos e desenvolvedores futuros na manutenção e evolução da plataforma **PyAutomation Manager**.

---

## 🧭 Visão Geral da Arquitetura

O sistema é construído como um monólito modular enxuto em Python (FastAPI + APScheduler + SQLite WAL + HTMX), acompanhado de um **Agente Remoto opcional em Go** (`agent/main.go` -> `pyautomation-agent.exe`) para execução distribuída em máquinas específicas.

### Fluxo de Execução de uma Automação

1. **Gatilho & Roteamento (`target_worker`):**
   - A automação pode ser acionada manualmente (via interface web HTMX ou API JSON autenticada) ou agendada pelo **APScheduler** (`run_automation_job`).
   - Se `Automation.target_worker` for `"local"` (padrão), a execução roda localmente no servidor via `_worker_thread`.
   - Se `Automation.target_worker` contiver o nome de uma máquina específica (ex: `"pc-financeiro"`), `run_automation_job` cria o registro `Execution` com status `PENDING` aguardando que o `pyautomation-agent.exe` daquela máquina busque a tarefa via `POST /api/worker/poll?worker_name=...`.
2. **Controle de Concorrência & Recuperação de Órfãs:**
   - O `_lock`, o conjunto `_running_automations` e a verificação no banco (`Execution.status.in_(["PENDING", "RUNNING"])`) em `app/services/executor.py` impedem que duas execuções da mesma automação rodem em paralelo (mesmo entre processos Web, Worker e Agentes Go remotos).
   - O semáforo global `_semaphore` garante que no máximo `MAX_CONCURRENT_EXECUTIONS` rodem ao mesmo tempo no host central.
   - Na inicialização (`lifespan` da web e startup do worker), `cleanup_orphaned_executions()` marca execuções `PENDING`/`RUNNING` órfãs de reinicializações anteriores como `FAILED`.
3. **Ambiente Virtual Isolado & Cache de Dependências:**
   - Antes de executar, `ensure_venv` verifica se existe a `.venv` na pasta `data/automations/auto_{id}/.venv` (ou no `workdir` do agente Go).
   - Se não existir, invoca `uv venv <caminho>`.
   - Calcula o hash SHA-256 (`_compute_deps_hash`) dos arquivos `pyproject.toml` e `requirements.txt` e compara com `.venv/.deps_hash`, só executando `uv pip install` quando os arquivos mudam ou quando `force_reinstall=True`.
   - Inclui o diretório de trabalho no `PYTHONPATH` para garantir que pacotes internos do repositório sejam importados sem erros de `ModuleNotFoundError`.
   - Escreve o arquivo físico `.env` no diretório de trabalho e injeta as variáveis no `os.environ` do processo a partir de `parse_env_vars`.
4. **Execução Segura em Subprocess & Cancelamento:**
   - O caminho do script (`entrypoint`) é validado via `resolve_safe_path` (`Path.is_relative_to` no Python e `filepath.Rel` no Go) tanto para automações `local` quanto `git`.
   - O processo é iniciado sem `shell=True`.
   - O cancelamento (`cancel_execution`) encerra processos locais imediatamente ou sinaliza `cancel_requested: true` no próximo heartbeat (`POST /api/worker/executions/{id}/heartbeat`) do Agente Go remoto, bloqueando novas tentativas de retry.
5. **Captura de Logs, Sanitização & Prevenção de XSS:**
   - Stdout e Stderr são combinados e sanitizados via regex em `app/core/security.py` para ofuscar tokens do GitHub, senhas em URLs e credenciais.
   - Na renderização HTML/HTMX (`executions_get_logs` e alertas dinâmicos), o conteúdo é escapado com `html.escape()`.
   - Limite de tamanho configurado em `MAX_LOG_SIZE_BYTES` evita estouro de memória.
6. **Retries:** Se o script falhar (e não tiver sido cancelado manualmente) e `max_retries > 0`, a rotina repete a execução após `retry_delay_seconds`.
7. **Modo Efêmero, Limpeza de Repositório (`cleanup_mode`) e Exclusão:**
   - `none`, `delete_git` ou `delete_repo` (suportados tanto pelo executor Python local quanto pelo Agente Go remoto).
   - Ao excluir uma automação (`automations_delete`), `delete_automation_files` encerra execuções ativas e apaga o diretório `data/automations/auto_{id}` do disco.

---

## ⏰ Padrões do Agendador (APScheduler 3.x) & Fuso Horário

- Utilizamos a linha estável **APScheduler 3.x** (`apscheduler>=3.10,<4.0`).
- **NÃO migre para a versão 4.x** sem alinhamento prévio, pois a API da v4 é substancialmente diferente e experimental.
- Os jobs são persistidos com `SQLAlchemyJobStore` na tabela `apscheduler_jobs`.
- O listener `_job_listener` escuta `EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED` para manter a coluna `Automation.next_run_at` sempre atualizada no banco após cada disparo.
- Sempre use `max_instances=1` para cada job agendado.
- A função alvo do job deve ser sempre `app.services.executor.run_automation_job` recebendo os IDs por parâmetro.
- **Fuso Horário:** Configurado via `settings.TIMEZONE` (padrão: `America/Sao_Paulo`).
- **Renderização de Datas:** Todos os templates Jinja2 usam o filtro `| localdatetime('%d/%m/%Y %H:%M:%S')` para converter timestamps UTC em horário local antes de exibir.

---

## 🔒 Diretrizes de Segurança Invioláveis

1. **Sem injeção de comandos:** Jamais passar strings concatenadas para interpretadores de shell.
2. **Path Traversal:** Validar sempre com `resolve_safe_path` que o script principal está dentro do diretório da automação.
3. **Credenciais e XSS:** Tokens nunca devem aparecer em texto puro nos logs gravados no banco, e saídas de log/erro em respostas HTML diretas devem usar `html.escape()`.
4. **Autenticação em Todas as Rotas:** Rotas web exigem `Depends(get_current_user)` (cookie assinado com HMAC + timestamp de expiração) e rotas `/api/*` (incluindo `/api/worker/*`) exigem `Depends(get_api_user)` (cookie de sessão ou header `X-API-Key` / `Bearer`).
5. **Isolamento de banco em testes:** Em fixtures de teste, configure sempre `SessionLocal.configure(bind=engine_test)` para garantir que threads de background compartilhem a mesma transação/banco de teste.
