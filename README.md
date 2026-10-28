# PyAutomation Manager ⚡

Plataforma moderna, segura e enxuta para centralizar, executar, agendar e monitorar automações Python e integrações HTTP, desenvolvida como uma alternativa prática, leve e auto-hospedada ao Prefect/Airflow — incluindo um **Agente Remoto em Go (`.exe`)** para rodar automações em máquinas específicas.

---

## 🎯 Objetivo & Filosofia

O **PyAutomation Manager** foi projetado seguindo os princípios **KISS**, **DRY** e **YAGNI**, priorizando simplicidade de operação, desempenho e segurança sem dependências pesadas de infraestrutura (dispensando inicialmente Celery, Redis, Kubernetes ou filas externas).

### O que você pode rodar no PyAutomation Manager?
Não se limita a RPA! Como cada automação roda em sua própria virtualenv isolada (`.venv`), você pode orquestrar:
- **ETL & Pipelines de Dados:** Extração de bancos SQL, processamento com `pandas`/`polars` e carga em Data Warehouses ou planilhas.
- **RPA & Web Scraping:** Coleta de dados e automação de portais via `Playwright`, `Selenium` (headless ou local em máquina específica), `BeautifulSoup` ou `httpx`.
- **Rotinas de IA & LLMs:** Agentes agendados, classificação de dados, OCR e atualização de bases RAG (OpenAI, Gemini, Anthropic).
- **Relatórios & Alertas:** Envio automático de relatórios por e-mail, WhatsApp, Slack, Discord ou Telegram.
- **DevOps & Backups:** Dumps de banco, upload para S3/Drive, health checks de servidores e limpeza de arquivos.
- **Webhooks & APIs Externas:** Disparo agendado de chamadas HTTP (`GET`, `POST`, `PUT`) para n8n, Make ou microsserviços próprios sem precisar escrever código Python.

---

## ✨ Funcionalidades Principais

- **Gerenciamento Completo de Automações:** Cadastro, edição, ativação/pausa e exclusão (com remoção automática dos arquivos e `.venv` do disco).
- **Execução Local ou em Máquinas Específicas (Agente Remoto em Go `.exe`):**
  - Defina o campo **Máquina / Worker Alvo** (`target_worker`) como `local` para executar no próprio servidor central ou informe o nome de uma máquina específica (ex: `pc-financeiro`, `vm-rpa-01`).
  - O binário leve `pyautomation-agent.exe` (escrito em Go puro, consumindo ~5 MB de RAM) roda na máquina alvo, busca as tarefas destinadas a ela, executa o script ou repositório Git, transmite os logs em tempo real e obedece ao botão de cancelamento da interface web.
- **Variáveis de Ambiente (`.env`):** Seção dedicada para gerenciar credenciais e configurações no formato padrão `CHAVE=VALOR` ou JSON, gravando fisicamente o arquivo `.env` na pasta de trabalho e injetando as variáveis em `os.environ` em tempo de execução. Detecta automaticamente modelos `.env.example` do repositório.
- **Múltiplas Origens:**
  - **Scripts Python Locais:** Edição direta do código pela interface web (distribuído automaticamente para workers remotos sem precisar copiar arquivos manualmente).
  - **Repositórios Git / GitHub:** Clone sob demanda, suporte a repositórios privados (via Token PAT), seleção de branch, rastreamento do commit SHA atual e botão para sincronização manual.
  - **Webhooks / Chamadas HTTP:** Requisições customizadas com método, headers JSON e body.
- **Ambientes Isolados com Cache Inteligente (`uv`):**
  - Cada automação possui sua própria `.venv` gerenciada via `uv`.
  - Detecta e instala automaticamente dependências de `pyproject.toml` e `requirements.txt`.
  - **Cache SHA-256 (`.deps_hash`):** Evita reinstalar pacotes a cada execução se os arquivos de dependências não mudaram.
  - **Reconstrução sob Demanda:** Botão na interface para recriar a `.venv` do zero quando necessário.
- **Modo Efêmero e Limpeza de Disco (`cleanup_mode`):**
  - `none`: Mantém arquivos no disco para execuções rápidas.
  - `delete_git`: Remove a pasta `.git` após clonar, economizando espaço.
  - `delete_repo`: Apaga todo o código do repositório após cada execução, re-clonando sob demanda no próximo disparo.
- **Motor de Agendamento (APScheduler 3.x):**
  - Suporte a agendamentos por intervalo (segundos), expressões Cron de 5 campos, diário e semanal.
  - Persistência em banco (`apscheduler_jobs`) e atualização automática de `next_run_at` a cada execução via event listener.
  - Respeita o fuso horário configurado (`TIMEZONE`, padrão `America/Sao_Paulo`).
- **Execução Segura, Cancelamento e Resiliência:**
  - Sem uso de `shell=True` (proteção contra Command Injection).
  - Controle de concorrência por automação (em memória e via banco) e limite global de execuções simultâneas (`MAX_CONCURRENT_EXECUTIONS`).
  - Cancelamento imediato de execuções em andamento (localmente ou em máquinas remotas via heartbeat) sem disparar retries indevidos.
  - Recuperação automática no startup: execuções que ficaram com status `RUNNING`/`PENDING` após uma queda/reinício do servidor são automaticamente marcadas como `FAILED`.
- **Dashboard & Logs em Tempo Real:** Interface reativa com Jinja2, HTMX (polling automático a cada 2s enquanto a execução está rodando) e Tailwind CSS (com suporte a Dark Mode).

---

## 🛠️ Stack Tecnológica

| Camada | Tecnologia | Justificativa |
|---|---|---|
| **Linguagem Principal** | Python 3.12+ | Performance moderna e tipagem estática |
| **Agente Remoto** | Go (Golang 1.22+) | Compilado em um único `.exe` leve (~5 MB RAM) e sem dependências |
| **Backend & Web** | FastAPI | Alta performance, rotas web e API REST documentada (`/docs`) |
| **Agendador** | APScheduler 3.11.x | Linha 3.x estável com `SQLAlchemyJobStore` |
| **Banco de Dados** | SQLite (WAL mode) | Leve, concorrente (`busy_timeout=5000`) e zero configuração |
| **ORM & Migrações** | SQLAlchemy 2.0 + Alembic | Modelagem segura e versionamento de schema |
| **Frontend** | Jinja2 + HTMX + Tailwind CSS | Interface dinâmica sem complexidade de SPAs em Node.js |
| **Gerenciador de Venvs** | `uv` | Criação de ambientes virtuais e instalação de pacotes ultra-rápidas |
| **Testes** | `pytest` | 25 testes automatizados cobrindo executor, agendador, worker remoto e rotas |

---

## 📁 Estrutura do Projeto

```text
gerenciador/
├── app/
│   ├── main.py                   # Fábrica FastAPI, lifespan, limpeza de órfãs e inicialização
│   ├── core/
│   │   ├── config.py             # Configurações via pydantic-settings (.env)
│   │   ├── database.py           # Conexão SQLite (WAL mode), auto-migração e seed do admin
│   │   └── security.py           # Hash PBKDF2, sessão HMAC com expiração, Path Traversal e sanitização
│   ├── models/
│   │   ├── automation.py         # Modelo de automação (origem, agendamento, target_worker, cleanup_mode)
│   │   ├── execution.py          # Histórico de execuções, PID, logs e duração
│   │   └── user.py               # Usuários do painel
│   ├── services/
│   │   ├── executor.py           # Execução em subprocess, venv uv com cache SHA-256, cancelamento e retries
│   │   ├── scheduler.py          # Jobs APScheduler e listener de atualização de next_run_at
│   │   └── git_service.py        # Clone/pull seguro, validação de URL e sanitização de saída Git
│   ├── api/
│   │   └── routes.py             # API REST JSON autenticada + endpoints do Worker Remoto (/api/worker/*)
│   ├── web/
│   │   ├── auth.py               # Dependências de autenticação (Cookie de Sessão e X-API-Key / Bearer)
│   │   └── routes.py             # Rotas HTML renderizadas via Jinja2 + fragmentos HTMX escapados
│   ├── templates/                # Templates Jinja2 responsivos com Tailwind CSS (Light/Dark)
│   └── static/                   # Arquivos estáticos
├── agent/                        # Agente Remoto em Go para execução em máquinas específicas
│   ├── main.go                   # Código-fonte 100% biblioteca padrão do Go
│   ├── go.mod                    # Módulo Go
│   └── pyautomation-agent.exe    # Executável compilado para Windows
├── worker/
│   └── main.py                   # Processo worker Python opcional para o agendador central
├── tests/
│   ├── conftest.py               # Fixtures de banco SQLite isolado e clientes autenticados
│   ├── test_executor.py          # Testes de execução, timeout, concorrência, cancelamento e segurança
│   ├── test_scheduler.py         # Testes de agendamento interval e cron no APScheduler
│   └── test_web.py               # Testes de login, sessão, XSS, Path Traversal, API e Worker Remoto
├── migrations/                   # Migrações Alembic
├── data/                         # Persistência do SQLite (app.db) e pastas das automações (auto_{id})
├── AGENTS.md                     # Guia de arquitetura e regras para agentes/desenvolvedores
├── pyproject.toml
└── README.md
```

---

## ⚙️ Configuração (`.env`)

Crie um arquivo `.env` na raiz do projeto:

```env
PROJECT_NAME="PyAutomation Manager"
SECRET_KEY="troque-por-uma-chave-secreta-longa-e-aleatoria"
API_KEY="sua-chave-secreta-para-workers-e-api"
ADMIN_USERNAME="admin"
ADMIN_PASSWORD="sua-senha-segura"
TIMEZONE="America/Sao_Paulo"
MAX_CONCURRENT_EXECUTIONS=5
DEFAULT_TIMEOUT_SECONDS=3600
RUN_SCHEDULER_IN_WEB=true
```

---

## 🚀 Como Executar o Servidor Central

### 1. Pré-requisitos
- Python 3.12+
- [uv](https://github.com/astral-sh/uv) instalado
- Git instalado

### 2. Instalar as Dependências
```powershell
uv sync
```

### 3. Executar as Migrações do Banco (Opcional — o startup também atualiza o schema)
```powershell
uv run alembic upgrade head
```

### 4. Iniciar o Servidor Web
```powershell
uv run uvicorn app.main:app --reload --port 8000
```
*(Ou diretamente pelo executável da venv: `.\.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000`)*

Acesse no navegador:
- **Painel Web:** `http://localhost:8000` (Credenciais padrão: `admin` / `admin123`)
- **Documentação OpenAPI:** `http://localhost:8000/docs`

---

## 🖥️ Como Rodar Automações em uma Máquina Específica (Agente em Go `.exe`)

Quando você precisa que uma automação rode em um computador específico (ex: máquina do Financeiro com certificado A3, VM com ERP local ou servidor dedicado):

### 1. Compilar o Agente (se necessário)
O executável `agent/pyautomation-agent.exe` já pode ser gerado com um único comando (sem dependências externas):

```powershell
# Compilar para Windows (.exe)
go build -C agent -ldflags="-s -w" -o pyautomation-agent.exe main.go

# Ou compilar para Linux
GOOS=linux GOARCH=amd64 go build -C agent -ldflags="-s -w" -o pyautomation-agent main.go
```

### 2. Configurar a Automação no Painel Web
No cadastro ou edição da automação, preencha o campo **Máquina / Worker Alvo (Onde Executar)** com o nome identificador da máquina, por exemplo:
```text
pc-financeiro
```
*(Deixe como `local` para as automações que devem continuar rodando no servidor principal).*

### 3. Executar o `pyautomation-agent.exe` na Máquina Específica
Copie apenas o arquivo `pyautomation-agent.exe` para a máquina desejada (que deve ter Python/uv instalados para rodar os scripts) e execute:

```powershell
.\pyautomation-agent.exe -server http://IP-DO-SERVIDOR:8000 -key sua-chave-secreta-para-workers-e-api -worker pc-financeiro
```

Você também pode configurar via variáveis de ambiente (`PYAUTO_SERVER`, `PYAUTO_KEY`, `PYAUTO_WORKER`, `PYAUTO_WORKDIR`):

| Parâmetro | Variável de Ambiente | Padrão | Descrição |
|---|---|---|---|
| `-server` | `PYAUTO_SERVER` | `http://localhost:8000` | URL base do PyAutomation Manager central |
| `-key` | `PYAUTO_KEY` | *(vazio)* | Valor configurado em `API_KEY` no `.env` do servidor |
| `-worker` | `PYAUTO_WORKER` | *Hostname do SO* | Nome da máquina (deve coincidir com **Máquina / Worker Alvo**) |
| `-workdir` | `PYAUTO_WORKDIR` | `./worker_data` | Pasta local onde o agente criará as `.venv` e clonará repositórios |
| `-interval` | - | `3` | Intervalo em segundos entre cada consulta ao servidor |

**O que o Agente Go faz automaticamente:**
1. Consulta `POST /api/worker/poll?worker_name=pc-financeiro`.
2. Baixa scripts Python locais diretamente da API ou clona/atualiza repositórios Git.
3. Cria a `.venv` isolada usando `uv` (ou `python -m venv` como fallback) e instala `pyproject.toml` / `requirements.txt` com cache SHA-256 (`.deps_hash`).
4. Injeta as variáveis `.env` configuradas no painel.
5. Transmite os logs em tempo real (`heartbeat` a cada 2s) para o painel web e encerra o processo imediatamente se você clicar em **"Cancelar Execução"** no navegador.
6. Respeita o `timeout`, `max_retries` e `cleanup_mode` (`delete_git` / `delete_repo`).

---

## 🔌 Uso da API REST Externa (`/api/*`)

Todas as rotas sob `/api` são protegidas. Você pode acessá-las estando logado no navegador (via cookie de sessão) ou enviando a chave configurada em `API_KEY` no `.env`:

```bash
# Listar automações
curl -H "X-API-Key: sua-chave-secreta" http://localhost:8000/api/automations

# Disparar uma automação manualmente
curl -X POST -H "X-API-Key: sua-chave-secreta" http://localhost:8000/api/automations/1/run

# Consultar status e logs de uma execução
curl -H "X-API-Key: sua-chave-secreta" http://localhost:8000/api/executions/1

# Cancelar uma execução em andamento
curl -X POST -H "X-API-Key: sua-chave-secreta" http://localhost:8000/api/executions/1/cancel
```
*(Também aceita o header `Authorization: Bearer sua-chave-secreta`).*

---

## 🧪 Executando os Testes Automatizados

```powershell
uv run pytest -v
```
*(Ou `.\.venv\Scripts\pytest.exe -v`)*

---

## 🛡️ Camadas de Segurança Implementadas

1. **Sem Execução Arbitrária de Shell (`shell=True`):** Todos os processos Python e comandos Git (tanto no servidor Python quanto no agente Go) rodam com listas de argumentos validadas.
2. **Proteção Estrita contra Path Traversal (`resolve_safe_path`):** O caminho do script (`entrypoint`) é validado tanto no servidor quanto no agente Go para nunca escapar do diretório da automação.
3. **Prevenção de XSS:** Toda saída de logs de terminal e mensagens de retorno em fragmentos HTMX passa por `html.escape()` antes de ser enviada ao navegador.
4. **Autenticação Web & API:** Senhas com hash `PBKDF2-HMAC-SHA256` (100.000 iterações + salt único), cookies `HttpOnly` assinados via HMAC com timestamp de expiração, e proteção das rotas `/api/*`, `/api/worker/*` e `/executions/{id}/logs`.
5. **Sanitização Automática de Logs:** Tokens do GitHub (`ghp_...`, `github_pat_...`), tokens `Bearer`, senhas em URLs (`https://user:pass@host`) e variáveis sensíveis (`API_KEY`, `PASSWORD`, `SECRET`, `TOKEN`) são mascarados com `[REDACTED]` antes de serem persistidos no banco.
