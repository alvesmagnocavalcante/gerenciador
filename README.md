# PyAutomation Manager ⚡

Plataforma moderna, segura e enxuta para centralizar, executar, agendar e monitorar automações Python, desenvolvida como uma alternativa prática e simplificada ao Prefect.

---

## 🎯 Objetivo & Filosofia

O **PyAutomation Manager** foi projetado seguindo os princípios **KISS**, **DRY** e **YAGNI**, priorizando simplicidade de operação, desempenho e segurança sem dependências desnecessárias (evitando inicialmente Celery, Redis, Kubernetes ou filas complexas).

### Funcionalidades Principais

- **Gerenciamento de Automações:** Cadastro, edição, ativação/pausa e exclusão de rotinas.
- **Variáveis de Ambiente (.env):** Seção dedicada para gerenciar credenciais e configurações em formato padrão `.env` (`CHAVE=VALOR`), gravando fisicamente o arquivo `.env` na raiz do projeto e injetando as variáveis no processo em tempo de execução. Detecção automática de modelos `.env.example`.
- **Origens Diversas:**
  - Scripts Python locais.
  - Repositórios GitHub (clone sob demanda, seleção de branch, rastreamento de commit sha e botão explícito de atualização).
  - Webhooks / Chamadas HTTP (GET, POST, PUT com headers e payloads).
- **Ambientes Isolados:** Cada automação opera em seu próprio ambiente virtual (`.venv`) gerenciado de forma ultra-rápida via `uv`, com detecção e instalação automática de dependências tanto de `requirements.txt` quanto de `pyproject.toml`.
- **Reconstrução sob Demanda:** Botão dedicado na interface para reinstalar ou reconstruir a virtualenv e suas dependências com feedback em tempo real.
- **Modo Efêmero e Limpeza de Disco:** Opção de apagar o repositório ou os metadados do `.git` após cada execução, mantendo o disco sempre limpo, além de botão para limpeza manual sob demanda.
- **Motor de Agendamento (APScheduler 3.x):** Suporte a agendamentos por intervalo (segundos), cron expressions, diários e semanais, com persistência em banco e cálculo de próxima execução.
- **Execução Segura via `subprocess`:** Sem `shell=True`, com timeout configurável, cancelamento de tarefas em andamento, retries automáticos com backoff e sanitização de logs (redação de tokens e credenciais).
- **Dashboard & Interface Interativa:** Construída com Jinja2, HTMX (para polling em tempo real de logs e execuções) e Tailwind CSS.
- **Arquitetura Flexível:** Funciona tanto com o agendador embutido no processo web quanto com um processo de worker dedicado (`worker/main.py`).

---

## 🛠️ Stack Tecnológica

| Camada | Tecnologia | Justificativa |
|---|---|---|
| **Linguagem** | Python 3.12+ (testado no Python 3.13) | Performance moderna e tipagem estática |
| **Backend & Web** | FastAPI | Alta performance assíncrona, documentação automática e OpenAPI |
| **Agendador** | APScheduler 3.11.x (versão estável de longo prazo) | Persistência confiável com `SQLAlchemyJobStore` |
| **Banco de Dados** | SQLite (WAL mode) / PostgreSQL | Leve para desenvolvimento e preparado para produção |
| **ORM & Migrações** | SQLAlchemy 2.0 + Alembic | Modelagem robusta e versionamento de schema |
| **Frontend** | Jinja2 + HTMX + Tailwind CSS | Interface reativa sem complexidade de SPAs em NodeJS |
| **Gerenciador de Pacotes** | `uv` | Instalação e criação de venvs ultra-rápidas |
| **Testes** | `pytest` | Testes automatizados cobrindo executor, agendador e rotas |

---

## 📁 Estrutura do Projeto

```text
gerenciamento/
├── app/
│   ├── main.py                   # Fábrica da aplicação FastAPI e ciclo de vida
│   ├── core/
│   │   ├── config.py             # Configurações do sistema via pydantic-settings
│   │   ├── database.py           # Conexão SQLite (WAL mode) e session maker
│   │   └── security.py           # Hash PBKDF2, autenticação de sessão e máscara de logs
│   ├── models/
│   │   ├── automation.py         # Modelo de automação (origem, agendamento, limites)
│   │   ├── execution.py          # Histórico de execuções, logs e status
│   │   └── user.py               # Usuários e controle de acesso
│   ├── services/
│   │   ├── executor.py           # Execução segura em subprocess com venv uv independente
│   │   ├── scheduler.py          # Gerenciamento de jobs APScheduler e sincronização
│   │   └── git_service.py        # Clone seguro, fetch, checkout e sanitização de URL Git
│   ├── api/
│   │   └── routes.py             # API REST JSON externa (/api/automations, /api/executions)
│   ├── web/
│   │   ├── auth.py               # Dependências de autenticação web e cookies seguros
│   │   └── routes.py             # Rotas renderizadas em HTML via Jinja2 + HTMX
│   ├── templates/                # Templates Jinja2 responsivos com Tailwind CSS
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   ├── login.html
│   │   ├── automations/
│   │   └── executions/
│   └── static/                   # Recursos estáticos
├── worker/
│   └── main.py                   # Processo worker dedicado para agendamento em produção
├── tests/
│   ├── conftest.py               # Fixtures de banco de dados isolado e cliente HTTP
│   ├── test_executor.py          # Testes de execução, timeout e prevenção de concorrência
│   ├── test_scheduler.py         # Testes de agendamento interval e cron com APScheduler
│   └── test_web.py               # Testes de login, dashboard e formulários HTMX
├── migrations/                   # Migrações Alembic
├── data/                         # Diretório de persistência do banco e venvs de automações
├── pyproject.toml
└── README.md
```

---

## 🚀 Como Executar

### 1. Pré-requisitos

- Python 3.12 ou superior
- [uv](https://github.com/astral-sh/uv) instalado (`pip install uv` ou via instalador oficial)
- Git instalado

### 2. Instalação das Dependências

Clone o repositório e sincronize o ambiente:

```bash
uv sync
```

### 3. Executar as Migrações do Banco de Dados

```bash
uv run alembic upgrade head
```

### 4. Iniciar o Servidor Web

```bash
uv run uvicorn app.main:app --reload --port 8000
```

Acesse no navegador:
👉 **http://localhost:8000**

#### Credenciais de Acesso Inicial (Admin):
- **Usuário:** `admin`
- **Senha:** `admin123`

*(As credenciais podem ser customizadas no arquivo `.env` ou variáveis de ambiente).*

---

## ⚙️ Execução em Modo Worker Separado (Opcional)

Para evitar duplicidade de jobs quando houver múltiplos processos web FastAPI (como réplicas em Gunicorn/Uvicorn), você pode desacoplar o agendador:

1. Configure no `.env`:
   ```env
   RUN_SCHEDULER_IN_WEB=false
   ```

2. Inicie a aplicação web normalmente:
   ```bash
   uv run uvicorn app.main:app --port 8000
   ```

3. Em outro terminal ou container, inicie o worker dedicado:
   ```bash
   uv run python worker/main.py
   ```

---

## 🧪 Executando os Testes Automatizados

A suíte de testes do PyAutomation valida isolamento de execução, controle de timeout, prevenção de concorrência duplicada, agendamento do APScheduler e autenticação web:

```bash
uv run pytest -v
```

---

## 🛡️ Segurança e Boas Práticas

1. **Sem Execução Arbitrária (`shell=True`):** Todas as chamadas de comando são executadas via lista de argumentos com caminhos normalizados, prevenindo Command Injection.
2. **Path Traversal Protection:** O executor valida que os arquivos principais das automações residam estritamente dentro da pasta autorizada da automação.
3. **Isolamento de Venvs:** Cada automação possui sua própria pasta `.venv`, impedindo que conflitos de dependências entre bibliotecas afetem o sistema.
4. **Sanitização de Logs:** Tokens de acesso do GitHub (`ghp_...`, `github_pat_...`), credenciais embutidas em URLs (`https://user:pass@host`) e cabeçalhos sensíveis são automaticamente mascarados antes de serem gravados no banco de dados.
5. **Autenticação Segura:** Senhas utilizam PBKDF2-HMAC-SHA256 com 100.000 iterações e salt aleatório único; cookies de sessão são assinados com HMAC.
