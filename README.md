# Ongma

Plataforma moderna, segura e enxuta para centralizar, executar, agendar e monitorar automações Python e integrações HTTP, desenvolvida como uma alternativa prática, leve e auto-hospedada ao Prefect/Airflow — incluindo o **Ongma Agent** para rodar automações em máquinas específicas.

A identidade visual da Ongma usa um símbolo geométrico inspirado na runa **Mannaz** e uma paleta de ciano, azul-petróleo e navy. Verde, âmbar e vermelho são reservados aos estados operacionais de sucesso, alerta e falha.

---

## 🎯 Objetivo & Filosofia

O **Ongma** foi projetado seguindo os princípios **KISS**, **DRY** e **YAGNI**, priorizando simplicidade de operação, desempenho e segurança sem dependências pesadas de infraestrutura (dispensando inicialmente Celery, Redis, Kubernetes ou filas externas).

### O que você pode rodar na Ongma?
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
- **Execução Local ou em Máquinas Específicas (Ongma Agent):**
  - Defina o campo **Máquina / Worker Alvo** (`target_worker`) como `local` para executar no próprio servidor central ou informe o nome de uma máquina específica (ex: `pc-financeiro`, `vm-rpa-01`).
  - O binário leve `ongma-agent.exe` roda na máquina alvo, busca as tarefas destinadas a ela, executa o script ou repositório Git, transmite os logs em tempo real e obedece ao botão de cancelamento da interface web.
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
  - Recuperação de execuções abandonadas por expiração de heartbeat, preservando tarefas ativas em outros processos e tarefas remotas na fila.
- **Dashboard & Logs em Tempo Real:** Interface responsiva com sidebar recolhível, temas claro/escuro, Jinja2 e HTMX. O detalhe da execução atualiza logs e estados automaticamente enquanto o processo está ativo; o dashboard também exibe brevemente o resultado final antes de remover o card de acompanhamento.
- **Progresso Estruturado em Tempo Real:** Barra percentual, etapa atual e tempo decorrido para scripts locais e agentes remotos. Sem evento estruturado, o painel mostra progresso indeterminado.

### Acompanhando funções com RunTrail

O pacote é disponibilizado automaticamente durante execuções locais e remotas; não é
necessário adicioná-lo ao `requirements.txt`:

```python
from runtrail import step


@step
def baixar_arquivo():
    ...


@step("Gerar relatório financeiro")
def gerar_relatorio():
    ...
```

Também funciona com `async def`. Início, conclusão, duração e exceções são registrados
automaticamente na seção **Trilha da execução**. A exceção original sempre é propagada,
preservando o comportamento normal do script. Funções sem `@step` continuam funcionando
sem qualquer instrumentação.

### Informando progresso pela automação

O script informa progresso pelo `stdout`. Use uma linha JSON com o prefixo
`PYAUTO_PROGRESS:` e mantenha `flush=True`:

```python
import json


def report_progress(percent: int, message: str) -> None:
    print(
        "PYAUTO_PROGRESS:" + json.dumps(
            {"percent": percent, "message": message},
            ensure_ascii=False,
        ),
        flush=True,
    )


report_progress(10, "Baixando arquivos")
report_progress(50, "Processando dados")
report_progress(90, "Gerando relatório")
```

`percent` deve ser um inteiro entre `0` e `100`; `message` é opcional e limitada a
255 caracteres. As linhas de controle não aparecem no console. O último progresso é
preservado mesmo se o limite de logs for atingido, e uma execução concluída que tenha
informado progresso é finalizada automaticamente em `100%`.

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
| **Testes** | `pytest` e `go test` | Executor, concorrência, migrações, agendador em outro processo, agente remoto e rotas |

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
│   └── ongma-agent.exe           # Executável compilado para Windows
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
PROJECT_NAME="Ongma"
SECRET_KEY="gere-uma-chave-aleatoria-exclusiva-com-pelo-menos-32-caracteres"
API_KEY="sua-chave-secreta-para-workers-e-api"
ADMIN_USERNAME="admin"
ADMIN_PASSWORD="defina-uma-senha-exclusiva-com-pelo-menos-8-caracteres"
TIMEZONE="America/Sao_Paulo"
MAX_CONCURRENT_EXECUTIONS=5
DEFAULT_TIMEOUT_SECONDS=3600
RUN_SCHEDULER_IN_WEB=true
```

| Variável | Obrigatória | Padrão | Finalidade |
|---|---:|---|---|
| `SECRET_KEY` | Sim | - | Assina as sessões do painel. Deve ser aleatória, exclusiva e ter pelo menos 32 caracteres. |
| `ADMIN_USERNAME` | Não | `admin` | Nome do primeiro administrador. |
| `ADMIN_PASSWORD` | Sim no primeiro uso | - | Senha inicial do administrador, com pelo menos 8 caracteres. Alterá-la no `.env` não troca a senha de um usuário já criado. |
| `API_KEY` | Para API/agentes | - | Autentica chamadas em `/api/*` e agentes remotos. Use um segredo diferente de `SECRET_KEY`. |
| `TIMEZONE` | Não | `America/Sao_Paulo` | Fuso usado pelo agendador e pela exibição de datas. |
| `MAX_CONCURRENT_EXECUTIONS` | Não | `5` | Limite global de execuções locais simultâneas. |
| `DEFAULT_TIMEOUT_SECONDS` | Não | `3600` | Timeout padrão de uma automação. |
| `MAX_LOG_SIZE_BYTES` | Não | `5242880` | Quantidade máxima de log persistida por execução. |
| `EXECUTION_LEASE_SECONDS` | Não | `120` | Tempo sem heartbeat para uma execução ser considerada órfã. Mínimo: 30. |
| `HEARTBEAT_INTERVAL_SECONDS` | Não | `5` | Intervalo de heartbeat. Valores aceitos: 1 a 10 segundos. |
| `RUN_SCHEDULER_IN_WEB` | Não | `true` | Mantém o agendador no processo web. Use `false` apenas com o worker separado. |
| `DEBUG` | Não | `false` | Ativa o modo de depuração. |

Use `.env.example` como referência. Gere segredos com `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"`.
O `.env` não deve ser versionado. A aplicação rejeita a chave de sessão padrão antiga;
trocar `SECRET_KEY` invalida as sessões existentes. `ADMIN_PASSWORD` é usada no primeiro
cadastro do administrador e para substituir a senha legada `admin123`; senhas já
personalizadas são preservadas. O campo `DEBUG` aceita booleanos ou os modos `debug`/`release`.

Execute apenas **um agendador por banco**. No modo com processo separado, configure
`RUN_SCHEDULER_IN_WEB=false` para a web e execute `uv run python -m worker.main` em
outro processo. O worker reconcilia cadastros, alterações, pausas e exclusões a cada
2 segundos; a web não grava jobs pendentes em um scheduler desligado. Uma trava de
arquivo impede iniciar dois agendadores para o mesmo banco SQLite.

Execuções locais respeitam `MAX_CONCURRENT_EXECUTIONS` também entre processos que
usam o mesmo banco. A reserva exclusiva por automação é garantida por índice SQLite,
inclusive durante retries. Execuções em curso renovam heartbeat; somente reservas
sem atividade por `EXECUTION_LEASE_SECONDS` (padrão 120 segundos) são recuperadas.
Tarefas remotas `PENDING` continuam aguardando o agente. Cancelamentos de tarefas já
iniciadas mantêm a reserva até a confirmação do executor. Uma requisição HTTP já
enviada pode produzir efeitos externos antes de ser cancelada.

Atualize o Ongma Agent junto com o servidor: o novo agente envia heartbeat também
durante clone, instalação de dependências, requisições HTTP e espera entre retries.
Distribua o executável recompilado e configure sua chave com `ONGMA_KEY`.
O agente interrompe o trabalho se não conseguir confirmar um heartbeat, evitando
continuar uma execução depois de perder contato com o servidor. Ao atualizar,
reinicie os processos web, worker e agentes para que todos usem o novo protocolo.

---

## Executar com Docker e GHCR

A imagem oficial do servidor central é publicada em:

```text
ghcr.io/alvesmagnocavalcante/ongma
```

Ela contém Python 3.12, `uv` e Git. O agente remoto Windows não faz parte da imagem: automações que dependem de navegador visível, certificado local, ERP ou recursos específicos da máquina devem continuar usando o `ongma-agent.exe`.

### Pré-requisitos para usar a imagem

- Docker Engine ou Docker Desktop;
- plugin Docker Compose v2 (`docker compose version`);
- porta TCP `8000` disponível no servidor.

Python, `uv` e Git já estão presentes na imagem do servidor. Eles só precisam ser instalados separadamente nas máquinas que executam o agente remoto.

### Instalação no Linux

1. Baixe somente os arquivos necessários:

```bash
mkdir -p ~/ongma
cd ~/ongma
curl -fsSLO https://raw.githubusercontent.com/alvesmagnocavalcante/gerenciador/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/alvesmagnocavalcante/gerenciador/main/.env.example -o .env
chmod 600 .env
```

O comando `docker compose` deve ser executado dentro desse diretório. A mensagem `no configuration file provided: not found` significa que o terminal não está no diretório que contém `compose.yaml`.

2. Gere dois segredos diferentes:

```bash
openssl rand -hex 32
openssl rand -hex 32
```

Se `openssl` não estiver disponível, execute o comando abaixo duas vezes. Use um resultado em `SECRET_KEY` e outro em `API_KEY`:

```bash
docker run --rm --entrypoint python ghcr.io/alvesmagnocavalcante/ongma:latest -c "import secrets; print(secrets.token_urlsafe(48))"
```

3. Edite o arquivo:

```bash
nano .env
```

Preencha `SECRET_KEY` com o primeiro segredo, `API_KEY` com o segundo e defina `ADMIN_PASSWORD` com pelo menos 8 caracteres. Não adicione espaços ao redor de `=`.

4. Baixe a imagem e inicie o servidor:

```bash
docker compose pull ongma
docker compose up -d
docker compose ps
docker compose logs --tail=100 ongma
```

Quando o log mostrar `Application startup complete`, acesse `http://IP-DO-SERVIDOR:8000`. No próprio servidor, use `http://localhost:8000`.

### Instalação no Windows

No PowerShell:

```powershell
New-Item -ItemType Directory -Force ongma
Set-Location ongma
Invoke-WebRequest https://raw.githubusercontent.com/alvesmagnocavalcante/gerenciador/main/compose.yaml -OutFile compose.yaml
Invoke-WebRequest https://raw.githubusercontent.com/alvesmagnocavalcante/gerenciador/main/.env.example -OutFile .env
notepad .env
```

Gere `SECRET_KEY` e `API_KEY` executando o comando abaixo duas vezes:

```powershell
docker run --rm --entrypoint python ghcr.io/alvesmagnocavalcante/ongma:latest -c "import secrets; print(secrets.token_urlsafe(48))"
```

Use resultados diferentes para as duas chaves, defina `ADMIN_PASSWORD` e execute:

```powershell
docker compose pull ongma
docker compose up -d
docker compose ps
docker compose logs --tail=100 ongma
```

### Primeiro acesso

1. Abra `http://localhost:8000` ou `http://IP-DO-SERVIDOR:8000`.
2. Entre com `ADMIN_USERNAME` e `ADMIN_PASSWORD` configurados no primeiro startup.
3. A senha fica armazenada como hash no banco. Alterar `ADMIN_PASSWORD` posteriormente no `.env` não modifica a conta existente.
4. A documentação da API fica em `http://IP-DO-SERVIDOR:8000/docs`.

O volume nomeado `ongma-data` preserva o SQLite, os códigos e os ambientes virtuais das automações em `/app/data`. Atualizar ou recriar o container não remove esses dados. Não execute `docker compose down -v` a menos que queira apagar definitivamente a instalação.

### Atualização

O Compose não baixa automaticamente uma imagem nova apenas porque a tag se chama `latest`. Force o download e a recriação:

```bash
cd ~/ongma
docker compose pull ongma
docker compose up -d --force-recreate
docker compose logs --tail=100 ongma
```

Para confirmar a regra de senha presente na imagem em execução:

```bash
docker compose exec ongma grep -n ADMIN_PASSWORD /app/app/core/config.py
```

Em produção, fixe uma versão no `compose.yaml`, como `ghcr.io/alvesmagnocavalcante/ongma:X.Y.Z`. Assim uma atualização só ocorre quando essa versão for alterada explicitamente.

### Comandos operacionais

| Ação | Comando |
|---|---|
| Ver estado | `docker compose ps` |
| Acompanhar logs | `docker compose logs -f ongma` |
| Reiniciar | `docker compose restart ongma` |
| Parar preservando dados | `docker compose down` |
| Iniciar novamente | `docker compose up -d` |
| Atualizar imagem | `docker compose pull ongma` seguido de `docker compose up -d --force-recreate` |

### Configurar e executar a primeira automação

1. No menu **Automações**, selecione **Nova automação**.
2. Escolha a origem:
   - **Script Python local:** cole um script simples diretamente no formulário;
   - **Repositório Git:** informe URL, branch ou tag e, para repositório privado, um token com acesso somente de leitura;
   - **HTTP:** informe método, URL, headers JSON e corpo da requisição.
3. Escolha **Manual** para executar sob demanda, **Intervalo** para recorrência simples ou **Cron** para um calendário de cinco campos. O agendador usa `TIMEZONE`.
4. Em **Arquivo principal**, informe um caminho relativo, como `main.py`. Caminhos fora da pasta da automação são rejeitados.
5. Em **Máquina alvo**, use `local` para executar no container ou o nome exato de um agente remoto conectado.
6. Configure timeout, retries e intervalo entre tentativas. Use `cleanup_mode=none` para reaproveitar o clone e a `.venv`; os modos efêmeros economizam disco, mas exigem novo clone nas execuções seguintes.
7. Adicione variáveis específicas da automação no formato `CHAVE=VALOR`, uma por linha. Não coloque esses segredos no repositório Git.
8. Salve e use **Executar**. O painel exibe status, logs, progresso e a trilha das funções decoradas com `@step`.

Para projetos Git em Python, mantenha pelo menos o entrypoint e o arquivo de dependências na raiz esperada:

```text
meu-projeto/
├── main.py
└── requirements.txt
```

O Ongma cria a `.venv` e instala `requirements.txt` ou `pyproject.toml` quando as dependências mudam. O pacote `runtrail` é fornecido pelo executor e não precisa ser adicionado ao `requirements.txt`.

### Solução de problemas

| Sintoma | Causa provável | Correção |
|---|---|---|
| `no configuration file provided` | Comando executado fora da pasta da instalação | Entre no diretório que contém `compose.yaml`. |
| Container fica reiniciando | `.env` inválido ou falha de inicialização | Execute `docker compose logs --tail=100 ongma`. |
| `ADMIN_PASSWORD ... too_short` | Senha abaixo de 8 caracteres ou imagem antiga | Corrija o `.env`, execute `docker compose pull ongma` e recrie o container. |
| O log ainda exige 12 caracteres | Imagem anterior à `v0.1.3` em cache | Use uma tag atual e `docker compose up -d --force-recreate`. |
| Porta `8000` em uso | Outro processo ocupa a porta publicada | Troque o lado esquerdo do mapeamento, por exemplo `8080:8000`, e acesse a porta 8080. |
| Login não muda após editar `.env` | A conta já existe no volume | O `.env` define somente a criação inicial; preserve o volume e use a credencial existente. |
| Git não encontrado no agente | Git ausente no computador remoto ou fora do `PATH` | Instale Git na máquina do agente e reinicie o agente. |
| Agente em outra máquina recebe erro de conexão e `TcpTestSucceeded` retorna `False` | Firewall do Hyper-V bloqueando entrada no WSL em modo `mirrored` | Crie a regra `Ongma-8000` descrita abaixo. |

#### Agente remoto não acessa o WSL em modo `mirrored`

> **Aplicação exclusiva:** siga esta seção somente quando o servidor Ongma estiver executando dentro do **WSL 2 em modo `mirrored`**. Não execute estes comandos em Linux nativo, Windows sem WSL, Docker Desktop com a porta já acessível, máquina virtual comum ou servidor externo.

Este caso ocorre quando o Ongma funciona em `http://localhost:8000` no computador servidor, mas outra máquina não consegue acessar `http://IP-DO-SERVIDOR:8000`. Confirme primeiro, na máquina do agente:

```powershell
Test-NetConnection IP-DO-SERVIDOR -Port 8000
```

Se `TcpTestSucceeded` for `False` e o WSL estiver usando `networkingMode=mirrored`, remova um encaminhamento NAT antigo, caso exista, e libere somente a porta do Ongma no firewall do Hyper-V. Execute no **PowerShell como administrador** do computador servidor:

```powershell
netsh interface portproxy delete v4tov4 `
  listenport=8000 `
  listenaddress=0.0.0.0

Get-NetFirewallHyperVRule `
  -Name "Ongma-8000" `
  -ErrorAction SilentlyContinue |
  Remove-NetFirewallHyperVRule

New-NetFirewallHyperVRule `
  -Name "Ongma-8000" `
  -DisplayName "Ongma Server 8000" `
  -Direction Inbound `
  -VMCreatorId "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}" `
  -Protocol TCP `
  -LocalPorts 8000
```

O primeiro comando pode informar que a regra `portproxy` não existe; nesse caso, prossiga normalmente. No WSL, confirme que o container publica a porta em todas as interfaces:

```bash
docker compose ps
ss -lnt | grep ':8000'
curl -I http://localhost:8000/login
```

O Compose deve mostrar `0.0.0.0:8000->8000/tcp`. Depois, repita na máquina do agente:

```powershell
Test-NetConnection IP-DO-SERVIDOR -Port 8000
curl.exe -I http://IP-DO-SERVIDOR:8000/login
.\ongma-agent.exe -configure
```

No agente, use o IPv4 da placa Ethernet ou Wi-Fi do computador servidor; nunca use `localhost` nem o IP de uma rede Docker. A regra acima é específica para a porta TCP 8000. Consulte a documentação da Microsoft sobre [rede no WSL](https://learn.microsoft.com/windows/wsl/networking) e [`New-NetFirewallHyperVRule`](https://learn.microsoft.com/powershell/module/netsecurity/new-netfirewallhypervrule).

### Tags publicadas

| Tag | Origem | Uso |
|---|---|---|
| `edge` | Último commit da branch principal | Homologação |
| `latest` | Última tag Git `vX.Y.Z` | Instalação estável |
| `X.Y.Z` e `X.Y` | Release versionada | Produção reproduzível |
| `sha-...` | Commit específico | Auditoria e rollback |

Para produção, prefira uma versão explícita no `compose.yaml`, por exemplo `ghcr.io/alvesmagnocavalcante/ongma:1.0.0`, em vez de `latest`.

### Publicação no GHCR

O workflow `.github/workflows/publish-ghcr.yml` executa os testes e publica imagens `linux/amd64` e `linux/arm64` usando o `GITHUB_TOKEN`; nenhum token adicional precisa ser salvo no repositório.

- Pull request: executa os testes, sem publicar.
- Push na branch principal: publica `edge` e a tag do commit.
- Tag `vX.Y.Z`: publica `latest`, `X.Y.Z`, `X.Y` e a tag do commit.
- `workflow_dispatch`: permite uma publicação manual.

Exemplo de primeira versão estável:

```powershell
git tag v0.1.0
git push origin v0.1.0
```

Depois da primeira publicação, confirme em **GitHub → Packages → ongma → Package settings** que a visibilidade está como **Public**. Imagens públicas do GHCR podem ser baixadas anonimamente.

### Limitações operacionais

- Execute apenas uma instância do container enquanto o banco for SQLite.
- Monte `/app/data` em volume persistente.
- Não inclua `.env`, banco ou tokens na imagem; o `.dockerignore` bloqueia esses arquivos.
- Containers Linux não substituem o agente Windows para RPA de desktop.

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
uv run uvicorn app.main:app --host 0.0.0.0 --reload --port 8000
```
*(Ou diretamente pelo executável da venv: `.\.venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --reload --port 8000`)*

O parâmetro `--host 0.0.0.0` permite acesso pelas interfaces de rede do servidor. Sem ele, o Uvicorn aceita apenas conexões locais (`127.0.0.1`). Em outros computadores, use `http://IP-DO-SERVIDOR:8000`. Se iniciar com `uv run python main.py`, a aplicação já usa `0.0.0.0`, mas na porta **8001**; nesse caso, use `http://IP-DO-SERVIDOR:8001`.

Acesse no navegador:
- **Painel Web:** `http://localhost:8000` (usuário de `ADMIN_USERNAME` e senha de `ADMIN_PASSWORD` no primeiro cadastro)
- **Documentação OpenAPI:** `http://localhost:8000/docs`

---

## 🖥️ Como Rodar Automações em uma Máquina Específica (Ongma Agent)

Quando você precisa que uma automação rode em um computador específico (ex: máquina do Financeiro com certificado A3, VM com ERP local ou servidor dedicado):

Baixe o executável Windows diretamente do repositório:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/alvesmagnocavalcante/gerenciador/main/agent/ongma-agent.exe -OutFile ongma-agent.exe
```

SHA-256 da versão atual:

```text
88a06822b9a866bdba2989bfd1c4d928b6e34663056d960ffbe0b5506b4a8dda
```

### Pré-requisitos da Máquina do Agente

O `ongma-agent.exe` é autocontido, mas as ferramentas usadas pelas automações precisam estar disponíveis no `PATH` da conta que inicia o agente:

| Ferramenta | Quando é necessária |
|---|---|
| **Python 3.12+** | Para executar automações Python e criar a `.venv` quando `uv` não estiver disponível |
| **uv** | Recomendado para criar a `.venv` e instalar dependências com maior velocidade; o agente usa `python -m venv` e `pip` como fallback |
| **Git** | Apenas para automações cuja origem é um repositório Git; scripts locais e chamadas HTTP não dependem dele |

No Windows, instale com:

```powershell
winget install --id Python.Python.3.12 -e
winget install --id astral-sh.uv -e
winget install --id Git.Git -e
```

Feche e abra novamente o terminal após a instalação e valide:

```powershell
python --version
uv --version
git --version
```

Se o agente for executado como serviço ou pelo Agendador de Tarefas, valide esses comandos usando a mesma conta do processo. Depois de instalar ou alterar o `PATH`, reinicie o agente para que ele receba o novo ambiente.

### 1. Compilar o Agente (se necessário)
O executável `agent/ongma-agent.exe` pode ser gerado com um único comando:

```powershell
# Compilar para Windows (.exe)
go build -C agent -ldflags="-s -w" -o ongma-agent.exe .

# Ou compilar para Linux
GOOS=linux GOARCH=amd64 go build -C agent -ldflags="-s -w" -o ongma-agent .
```

### 2. Configurar a Automação no Painel Web
No cadastro ou edição da automação, preencha o campo **Máquina / Worker Alvo (Onde Executar)** com o nome identificador da máquina, por exemplo:
```text
pc-financeiro
```
*(Deixe como `local` para as automações que devem continuar rodando no servidor principal).*

### 3. Executar o `ongma-agent.exe` na Máquina Específica
Copie o arquivo `ongma-agent.exe` para a máquina desejada, confirme os pré-requisitos acima e **abra com dois cliques**. Na primeira abertura, o assistente no console solicita:

1. **URL do servidor:** endereço usado para acessar o gerenciador, com protocolo e porta. Ex.: `http://192.168.1.10:8001`. `localhost` só funciona se o servidor estiver no mesmo computador. Se iniciado por `uv run python main.py`, o servidor usa a porta **8001**; o comando Uvicorn da seção anterior usa **8000**.
2. **Chave de API:** valor de `API_KEY` no `.env` do servidor. A digitação fica oculta.
3. **Nome da máquina/worker:** sugere o nome do computador. Use o mesmo valor em **Máquina / Worker Alvo** no painel.
4. **Pasta de trabalho:** sugere uma pasta no perfil do usuário para armazenar repositórios e ambientes virtuais.

O assistente verifica conexão e autenticação sem buscar tarefas. A configuração só é salva após essa verificação e o agente inicia em seguida. Em caso de falha, permite corrigir os campos ou sair.

A configuração fica em `%APPDATA%\Ongma\agent.json`. A chave é protegida pela **DPAPI do Windows**, vinculada ao usuário que a configurou. Não copie esse arquivo para outros usuários/computadores; configure o executável em cada destino. Instalações existentes continuam aceitando `%APPDATA%\PyAutomation\agent.json`.

Nas próximas aberturas, pressione **Enter** para iniciar, **C** para configurar novamente ou **S** para sair. Também é possível abrir diretamente o assistente com `.\ongma-agent.exe -configure`.

Para serviços e execução automatizada, os parâmetros existentes continuam disponíveis, sem assistente. Em Linux, utilize parâmetros ou variáveis de ambiente:

```powershell
.\ongma-agent.exe -server http://IP-DO-SERVIDOR:8000 -key sua-chave-secreta-para-workers-e-api -worker pc-financeiro
```

Você também pode configurar via variáveis de ambiente (`ONGMA_SERVER`, `ONGMA_KEY`, `ONGMA_WORKER`, `ONGMA_WORKDIR`). Os nomes antigos `PYAUTO_*` continuam aceitos para compatibilidade.

Exemplo no PowerShell:

```powershell
$env:ONGMA_SERVER = "http://IP-DO-SERVIDOR:8000"
$env:ONGMA_KEY = "valor-de-API_KEY"
$env:ONGMA_WORKER = "pc-financeiro"
.\ongma-agent.exe
```

Exemplo no Linux:

```bash
export ONGMA_SERVER="http://IP-DO-SERVIDOR:8000"
export ONGMA_KEY="valor-de-API_KEY"
export ONGMA_WORKER="pc-financeiro"
./ongma-agent
```

Precedência: **parâmetros > variáveis `ONGMA_*` > variáveis legadas `PYAUTO_*` > configuração salva > padrões**. Parâmetros e variáveis não alteram o arquivo salvo, exceto quando o assistente é solicitado com `-configure`. Sem parâmetros, definir `ONGMA_SERVER` evita o menu interativo.

| Parâmetro | Variável de Ambiente | Padrão | Descrição |
|---|---|---|---|
| `-server` | `ONGMA_SERVER` | `http://localhost:8000` | URL base do servidor central da Ongma |
| `-key` | `ONGMA_KEY` | *(vazio)* | Valor configurado em `API_KEY` no `.env` do servidor |
| `-worker` | `ONGMA_WORKER` | *Hostname do SO* | Nome da máquina (deve coincidir com **Máquina / Worker Alvo**) |
| `-workdir` | `ONGMA_WORKDIR` | `./worker_data` | Pasta local onde o agente criará as `.venv` e clonará repositórios |
| `-interval` | - | `3` | Intervalo em segundos entre cada consulta ao servidor |
| `-configure` | - | `false` | Abre o assistente de configuração no Windows |

**O que o Ongma Agent faz automaticamente:**
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
go test -C agent ./...
go vet -C agent ./...
```
*(Ou `.\.venv\Scripts\pytest.exe -v`)*

---

## 🛡️ Camadas de Segurança Implementadas

1. **Sem Execução Arbitrária de Shell (`shell=True`):** Todos os processos Python e comandos Git (tanto no servidor quanto no Ongma Agent) rodam com listas de argumentos validadas.
2. **Proteção Estrita contra Path Traversal (`resolve_safe_path`):** O caminho do script (`entrypoint`) é validado tanto no servidor quanto no Ongma Agent para nunca escapar do diretório da automação.
3. **Prevenção de XSS:** Toda saída de logs de terminal e mensagens de retorno em fragmentos HTMX passa por `html.escape()` antes de ser enviada ao navegador.
4. **Autenticação Web & API:** Senhas com hash `PBKDF2-HMAC-SHA256` (100.000 iterações + salt único), cookies `HttpOnly` assinados via HMAC com timestamp de expiração, e proteção das rotas `/api/*`, `/api/worker/*` e `/executions/{id}/logs`.
5. **Sanitização Automática de Logs:** Tokens do GitHub (`ghp_...`, `github_pat_...`), tokens `Bearer`, senhas em URLs (`https://user:pass@host`) e variáveis sensíveis (`API_KEY`, `PASSWORD`, `SECRET`, `TOKEN`) são mascarados com `[REDACTED]` antes de serem persistidos no banco.
