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
   - O índice parcial único `uq_execution_active_automation` impede duas execuções `PENDING`/`RUNNING` da mesma automação, inclusive entre processos. O poll remoto assume a tarefa com UPDATE condicional; `_lock` e `_running_automations` são otimizações locais.
   - O semáforo `_semaphore` limita threads por processo; a contagem e o claim com `BEGIN IMMEDIATE` garantem `MAX_CONCURRENT_EXECUTIONS` entre processos locais que usam o mesmo SQLite. Retries usam um laço e mantêm a reserva ativa.
   - `cleanup_orphaned_executions()` recupera apenas reservas com heartbeat expirado. A manutenção renova tarefas locais ativas, inclusive na fila; tarefas remotas `PENDING` continuam aguardando o agente. `owner_id`, `heartbeat_at` e `cancel_requested` persistem propriedade, atividade e cancelamento.
3. **Ambiente Virtual Isolado & Cache de Dependências:**
   - Antes de executar, `ensure_venv` verifica se existe a `.venv` na pasta `data/automations/auto_{id}/.venv` (ou no `workdir` do agente Go).
   - Se não existir, invoca `uv venv <caminho>`.
   - Calcula o hash SHA-256 (`_compute_deps_hash`) dos arquivos `pyproject.toml` e `requirements.txt` e compara com `.venv/.deps_hash`, só executando `uv pip install` quando os arquivos mudam ou quando `force_reinstall=True`.
   - Inclui o diretório de trabalho no `PYTHONPATH` para garantir que pacotes internos do repositório sejam importados sem erros de `ModuleNotFoundError`.
   - Escreve o arquivo físico `.env` no diretório de trabalho e injeta as variáveis no `os.environ` do processo a partir de `parse_env_vars`.
4. **Execução Segura em Subprocess & Cancelamento:**
   - O caminho do script (`entrypoint`) é validado via `resolve_safe_path` (`Path.is_relative_to` no Python e `filepath.Rel` no Go) tanto para automações `local` quanto `git`.
   - O processo é iniciado sem `shell=True`.
   - O cancelamento (`cancel_execution`) sinaliza `cancel_requested` no banco e encerra subprocessos locais conhecidos. Execuções já iniciadas mantêm a reserva até a confirmação; o agente recebe a solicitação no heartbeat. Tarefas canceladas na fila não iniciam subprocessos nem chamadas HTTP.
5. **Captura de Logs, Sanitização & Prevenção de XSS:**
   - Stdout e Stderr são combinados e sanitizados via regex em `app/core/security.py` para ofuscar tokens do GitHub, senhas em URLs e credenciais.
   - Na renderização HTML/HTMX (`executions_get_logs` e alertas dinâmicos), o conteúdo é escapado com `html.escape()`.
   - Python e Go drenam a saída para buffers limitados. O limite `MAX_LOG_SIZE_BYTES` é aplicado em bytes UTF-8; logs e mensagens de erro são sanitizados. O executor Python persiste snapshots durante a execução.
   - Linhas `PYAUTO_PROGRESS:{"percent": 50, "message": "Etapa"}` são extraídas do stdout, validadas e persistidas em `Execution.progress_*`. Elas não aparecem no console. O buffer preserva o último evento mesmo depois de truncar logs.
   - O runtime interno **RunTrail** é adicionado ao `PYTHONPATH` local e distribuído pelo servidor ao agente Go. Funções decoradas com `from runtrail import step` emitem linhas `RUNTRAIL_EVENT:`; o servidor valida, remove essas linhas dos logs e persiste estados idempotentes em `execution_steps`. Exceções sempre são propagadas pelo decorator.
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
- Apenas um processo pode possuir o agendador por banco (trava de arquivo). Com `RUN_SCHEDULER_IN_WEB=false`, a web calcula previsões e o worker reconcilia o banco a cada 2 segundos. Não compartilhe instâncias APScheduler entre processos.
- A função alvo do job deve ser sempre `app.services.executor.run_automation_job` recebendo os IDs por parâmetro.
- **Fuso Horário:** Configurado via `settings.TIMEZONE` (padrão: `America/Sao_Paulo`).
- **Renderização de Datas:** Todos os templates Jinja2 usam o filtro `| localdatetime('%d/%m/%Y %H:%M:%S')` para converter timestamps UTC em horário local antes de exibir.

---

## 🔒 Diretrizes de Segurança Invioláveis

1. **Sem injeção de comandos:** Jamais passar strings concatenadas para interpretadores de shell.
2. **Path Traversal:** Validar sempre com `resolve_safe_path` que o script principal está dentro do diretório da automação.
3. **Credenciais e XSS:** Tokens nunca devem aparecer em texto puro nos logs gravados no banco, e saídas de log/erro em respostas HTML diretas devem usar `html.escape()`.
4. **Autenticação em Todas as Rotas:** Rotas web exigem `Depends(get_current_user)` (cookie assinado com HMAC + timestamp de expiração) e rotas `/api/*` (incluindo `/api/worker/*`) exigem `Depends(get_api_user)` (cookie de sessão ou header `X-API-Key` / `Bearer`).
5. **Isolamento de banco em testes:** Configure caminhos e segredos de teste antes de importar a aplicação. `SessionLocal`, inicialização e jobstore devem usar o mesmo engine de teste; cada requisição recebe sua própria sessão. Nunca execute fixtures contra `data/app.db`.
