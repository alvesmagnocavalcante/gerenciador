# AGENTS.md — Guia de Arquitetura e Desenvolvimento para Agentes

Este documento orienta agentes autônomos e desenvolvedores futuros na manutenção e evolução da plataforma **PyAutomation Manager**.

---

## 🧭 Visão Geral da Arquitetura

O sistema é construído como um monólito modular enxuto, sem camadas desnecessárias de microsserviços.

### Fluxo de Execução de uma Automação

1. **Gatilho:** A automação pode ser acionada manualmente (via interface web HTMX ou API JSON) ou agendada pelo **APScheduler** (`run_automation_job`).
2. **Controle de Concorrência:**
   - O `_lock` em `app/services/executor.py` impede que duas execuções da mesma automação rodem em paralelo.
   - O semáforo global `_semaphore` garante que no máximo `MAX_CONCURRENT_EXECUTIONS` rodem ao mesmo tempo no host.
3. **Ambiente Virtual Isolado & Dependências:**
   - Antes de executar, o método `ensure_venv` verifica se existe a `.venv` na pasta `data/automations/auto_{id}/.venv`.
   - Se não existir, invoca `uv venv <caminho>`.
   - Detecta e instala dependências automaticamente via `uv pip install`:
     - Se houver `pyproject.toml`, executa `uv pip install --python <bin> -r pyproject.toml`.
     - Se houver `requirements.txt`, executa `uv pip install --python <bin> -r requirements.txt`.
   - Inclui o diretório de trabalho no `PYTHONPATH` para garantir que pacotes internos do repositório sejam importados sem erros de `ModuleNotFoundError`.
   - Disponibiliza ação na interface e na API (`rebuild_automation_venv`) para reinstalação e reconstrução limpa sob demanda.
   - Escreve o arquivo físico `.env` no diretório de trabalho e injeta as variáveis no `os.environ` do processo a partir de `parse_env_vars`.
4. **Execução Segura em Subprocess:**
   - O processo é iniciado via `subprocess.Popen` com lista de argumentos (`[python_bin, entrypoint, *args]`).
   - `shell=True` **NUNCA** deve ser utilizado.
   - O PID é registrado no banco e no dicionário `_active_processes` para permitir cancelamento imediato pelo usuário.
5. **Captura de Logs & Sanitização:**
   - Stdout e Stderr são combinados e sanitizados via regex em `app/core/security.py` para ofuscar tokens do GitHub, senhas em URLs e credenciais.
   - Limite de tamanho configurado em `MAX_LOG_SIZE_BYTES` evita estouro de memória.
6. **Retries:** Se o script falhar e `max_retries > 0`, a rotina repete a execução após `retry_delay_seconds`.
7. **Modo Efêmero & Limpeza de Repositório (`cleanup_mode`):**
   - Configuração por automação para evitar acúmulo de arquivos e metadados no disco:
     - `none`: Mantém os arquivos no disco para execuções subsequentes mais rápidas.
     - `delete_git`: Apaga a pasta `.git` após clonar/executar, economizando espaço de versionamento.
     - `delete_repo`: Modo efêmero total. Apaga o diretório do repositório logo após a execução, clonando sob demanda no próximo disparo.
   - Ação manual `clean_automation_workspace` disponível via botão na interface web para limpeza imediata.

---

## ⏰ Padrões do Agendador (APScheduler 3.x) & Fuso Horário

- Utilizamos a linha estável **APScheduler 3.x** (`apscheduler>=3.10,<4.0`).
- **NÃO migre para a versão 4.x** sem alinhamento prévio, pois a API da v4 é substancialmente diferente e experimental.
- Os jobs são persistidos com `SQLAlchemyJobStore` na tabela `apscheduler_jobs`.
- Sempre use `max_instances=1` para cada job agendado.
- A função alvo do job deve ser sempre `app.services.executor.run_automation_job` recebendo os IDs por parâmetro.
- **Fuso Horário:** Configurado via `settings.TIMEZONE` (padrão: `America/Sao_Paulo`). O scheduler e os triggers (`CronTrigger`, `IntervalTrigger`) usam esse fuso para disparar no horário local esperado pelo usuário.
- **Renderização de Datas:** Todos os templates Jinja2 usam o filtro `| localdatetime('%d/%m/%Y %H:%M:%S')` para converter timestamps UTC em horário local antes de exibir.

---

## 🎨 Frontend: HTMX + Tailwind CSS

- A interface não utiliza React/Vue/Node.js. Tudo é renderizado pelo FastAPI via **Jinja2**.
- O **HTMX** é utilizado para dinamismo:
  - Botão de execução manual dispara `hx-post` e insere feedback sem recarregar a tela.
  - O visualizador de logs usa `hx-get="/executions/{id}/logs" hx-trigger="every 2s" hx-swap="outerHTML"` para atualizar a saída do terminal em tempo real enquanto o status for `RUNNING` ou `PENDING`.

---

## 🔒 Diretrizes de Segurança Invioláveis

1. **Sem injeção de comandos:** Jamais passar strings concatenadas para interpretadores de shell.
2. **Path Traversal:** Validar sempre que o script principal está dentro do diretório do projeto ou repositório.
3. **Credenciais:** Tokens de repositórios privados do GitHub nunca devem aparecer em texto puro nos logs gravados no banco.
4. **Isolamento de banco em testes:** Em fixtures de teste, configure sempre `SessionLocal.configure(bind=engine_test)` para garantir que threads de background compartilhem a mesma transação/banco de teste.
