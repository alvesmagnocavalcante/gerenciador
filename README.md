# Ongma

Ongma centraliza, agenda, executa e acompanha automações Python, repositórios Git e chamadas HTTP.

- Painel web simples;
- execução manual ou agendada;
- logs, progresso e etapas em tempo real;
- execução no servidor ou em outra máquina com o **Ongma Agent**;
- ambiente virtual isolado para cada automação.

## Instalação rápida com Docker

### Linux

Requisitos: Docker Engine e Docker Compose v2.

```bash
mkdir -p ~/ongma
cd ~/ongma

curl -fsSLO https://raw.githubusercontent.com/alvesmagnocavalcante/gerenciador/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/alvesmagnocavalcante/gerenciador/main/.env.example -o .env
chmod 600 .env
```

Gere duas chaves diferentes:

```bash
openssl rand -hex 32
openssl rand -hex 32
```

Edite o `.env`:

```bash
nano .env
```

Preencha pelo menos:

```env
SECRET_KEY=primeira-chave-gerada
API_KEY=segunda-chave-gerada
ADMIN_USERNAME=admin
ADMIN_PASSWORD=senha-com-no-minimo-8-caracteres
```

Inicie:

```bash
docker compose pull ongma
docker compose up -d
docker compose ps
```

### Windows

Requisito: Docker Desktop.

```powershell
New-Item -ItemType Directory -Force ongma
Set-Location ongma

Invoke-WebRequest https://raw.githubusercontent.com/alvesmagnocavalcante/gerenciador/main/compose.yaml -OutFile compose.yaml
Invoke-WebRequest https://raw.githubusercontent.com/alvesmagnocavalcante/gerenciador/main/.env.example -OutFile .env
notepad .env
```

Para gerar cada chave sem instalar Python, execute duas vezes:

```powershell
docker run --rm --entrypoint python ghcr.io/alvesmagnocavalcante/ongma:latest -c "import secrets; print(secrets.token_urlsafe(48))"
```

Use resultados diferentes em `SECRET_KEY` e `API_KEY`, defina `ADMIN_PASSWORD` e inicie:

```powershell
docker compose pull ongma
docker compose up -d
docker compose ps
```

## Primeiro acesso

Acesse:

```text
http://localhost:8000
```

Em outra máquina da rede:

```text
http://IP-DO-SERVIDOR:8000
```

Entre com `ADMIN_USERNAME` e `ADMIN_PASSWORD` definidos no primeiro startup.

> Alterar `ADMIN_PASSWORD` no `.env` não modifica uma conta que já foi criada no banco.

## Configuração principal

| Variável | Padrão | Uso |
|---|---|---|
| `SECRET_KEY` | obrigatória | Assina sessões. Use uma chave aleatória com pelo menos 32 caracteres. |
| `API_KEY` | vazia | Autentica a API e o Ongma Agent. |
| `ADMIN_USERNAME` | `admin` | Usuário administrador inicial. |
| `ADMIN_PASSWORD` | obrigatória no primeiro uso | Senha inicial com pelo menos 8 caracteres. |
| `TIMEZONE` | `America/Sao_Paulo` | Fuso do agendador e das datas do painel. |
| `MAX_CONCURRENT_EXECUTIONS` | `5` | Limite de automações locais simultâneas. |
| `DEFAULT_TIMEOUT_SECONDS` | `3600` | Timeout padrão das automações. |

Consulte [.env.example](.env.example) para as demais opções.

## Criar uma automação

1. Acesse **Automações → Nova automação**.
2. Escolha a origem:
   - **Script local:** cole o código Python no formulário;
   - **Git:** informe URL, branch e token somente se o repositório for privado;
   - **HTTP:** informe URL, método, headers e body.
3. Escolha execução manual, intervalo ou Cron.
4. Use `local` em **Máquina / Worker Alvo** para executar no servidor.
5. Informe o entrypoint, normalmente `main.py`.
6. Adicione variáveis da automação no formato `CHAVE=VALOR`, uma por linha.
7. Salve e clique em **Executar**.

Para projetos Git em Python:

```text
meu-projeto/
├── main.py
└── requirements.txt
```

O Ongma cria a `.venv` e instala `requirements.txt` ou `pyproject.toml` automaticamente.

## Mostrar etapas e progresso

### Etapas com RunTrail

```python
from runtrail import step

@step("Gerando relatório")
def gerar_relatorio():
    ...
```

O `runtrail` é fornecido pelo Ongma. Não o adicione ao `requirements.txt`.

### Progresso percentual

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

report_progress(25, "Baixando arquivos")
report_progress(70, "Processando dados")
report_progress(100, "Concluído")
```

## Ongma Agent

Use o agente quando a automação precisar rodar em outro computador, por exemplo uma máquina com ERP, certificado ou navegador visível.

### Download para Windows

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/alvesmagnocavalcante/gerenciador/main/agent/ongma-agent.exe -OutFile ongma-agent.exe
```

SHA-256 atual:

```text
88a06822b9a866bdba2989bfd1c4d928b6e34663056d960ffbe0b5506b4a8dda
```

### Requisitos da máquina do agente

- Python 3.12 ou superior;
- `uv` recomendado;
- Git apenas para automações de origem Git.

No Windows:

```powershell
winget install --id Python.Python.3.12 -e
winget install --id astral-sh.uv -e
winget install --id Git.Git -e
```

### Configurar

```powershell
.\ongma-agent.exe -configure
```

Informe:

1. URL do servidor, por exemplo `http://192.168.1.10:8000`;
2. valor de `API_KEY` do servidor;
3. nome do worker, por exemplo `pc-financeiro`;
4. pasta de trabalho.

Na automação, use exatamente o mesmo nome em **Máquina / Worker Alvo**.

> Em outra máquina, nunca use `localhost` como URL do servidor.

A configuração fica em `%APPDATA%\Ongma\agent.json`; a chave é protegida pela DPAPI do Windows.

## Atualizar e operar

Atualizar sem apagar os dados:

```bash
docker compose pull ongma
docker compose up -d --force-recreate
```

Comandos principais:

```bash
docker compose ps
docker compose logs -f ongma
docker compose restart ongma
docker compose down
docker compose up -d
```

Os dados ficam no volume `ongma-data`. Não execute `docker compose down -v` salvo se quiser apagar definitivamente o banco e as automações.

## Erros comuns

| Erro | Correção |
|---|---|
| `no configuration file provided` | Entre na pasta que contém `compose.yaml`. |
| Container reiniciando | Execute `docker compose logs --tail=100 ongma` e revise o `.env`. |
| `ADMIN_PASSWORD ... too_short` | Use no mínimo 8 caracteres e recrie o container. |
| O log ainda exige 12 caracteres | Execute `docker compose pull ongma` e recrie com `--force-recreate`. |
| API retorna `401` | Confirme `API_KEY` e recrie o container após alterar o `.env`. |
| Agente não conecta | Use o IP do servidor, valide a porta 8000 e confira o firewall. |
| Git não encontrado no agente | Instale Git e reinicie o agente. |

Procedimentos específicos para **WSL 2**, testes da API e execução pelo código-fonte estão no [guia avançado](docs/GUIA_AVANCADO.md).

## Observações de segurança

- Nunca versione `.env`, banco, tokens ou senhas;
- use um token Git somente de leitura para repositórios privados;
- não envie `API_KEY` em mensagens ou prints;
- mantenha o pacote GHCR público se os usuários precisarem instalar sem autenticação;
- execute somente uma instância enquanto o banco for SQLite.
