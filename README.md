# Ongma

Ongma centraliza, agenda, executa e acompanha automações Python, repositórios Git e chamadas HTTP.

- Painel web simples;
- execução manual ou agendada;
- logs, progresso e etapas em tempo real;
- execução local no servidor, sem necessidade de agente;
- execução remota opcional com o **Ongma Agent**;
- ambiente virtual isolado para cada automação.

## Instalação rápida com Docker

### Requisitos

- Windows: Docker Desktop;
- Linux: Docker Engine;
- porta local `8000` disponível.

Não é necessário instalar Python, clonar o repositório, baixar arquivos do GitHub ou criar `.env`.

### 1. Iniciar

```powershell
docker run -d --name ongma -p 127.0.0.1:8000:8000 --restart unless-stopped -v ongma-data:/app/data m4gno/ongma:latest
```

O Docker baixa a imagem automaticamente quando ela ainda não existe. Confirme que o container está saudável:

```powershell
docker ps --filter "name=ongma"
docker logs --tail 100 ongma
```

### 2. Criar o primeiro administrador

Obtenha o token de posse gerado na primeira inicialização:

```powershell
docker exec ongma cat /app/data/setup-token
```

Abra `http://localhost:8000`, informe o token e defina o usuário e a senha do administrador.

O token não aparece nos logs e é removido depois do cadastro. Se o arquivo não existir, a instalação já possui um usuário ou foi configurada por variáveis de ambiente.

### Persistência

O volume Docker `ongma-data` contém:

- banco SQLite;
- usuários e senhas;
- automações e seus arquivos;
- histórico de execuções;
- chaves internas da instalação.

Parar, remover ou recriar somente o container não apaga esses dados.

### Atualizar sem perder dados

Use exatamente o mesmo volume ao recriar o container:

```powershell
docker pull m4gno/ongma:latest
docker stop ongma
docker rm ongma
docker run -d --name ongma -p 127.0.0.1:8000:8000 --restart unless-stopped -v ongma-data:/app/data m4gno/ongma:latest
```

### Reinstalar completamente do zero

> Atenção: este procedimento apaga definitivamente usuários, banco, automações, histórico e chaves.

```powershell
docker rm -f ongma
docker volume rm ongma-data
docker image rm m4gno/ongma:latest
docker run -d --name ongma -p 127.0.0.1:8000:8000 --restart unless-stopped -v ongma-data:/app/data m4gno/ongma:latest
```

Não execute dois containers simultaneamente com o mesmo volume `ongma-data`, pois o banco é SQLite.

## Docker Compose e configuração avançada

### Linux

Requisitos: Docker Engine e Docker Compose v2.24 ou superior.

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

Para gerenciar as credenciais externamente ou criar o administrador sem a tela de setup, preencha:

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
docker run --rm --entrypoint python m4gno/ongma:latest -c "import secrets; print(secrets.token_urlsafe(48))"
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

A instalação rápida publica a porta somente em `127.0.0.1`, portanto não fica acessível por outras máquinas da rede. Para acesso remoto, use HTTPS com proxy reverso e controle de firewall; não exponha o setup diretamente à internet.

Em uma instalação nova, informe o token de configuração e defina o administrador. Em instalações configuradas por variáveis de ambiente, entre com `ADMIN_USERNAME` e `ADMIN_PASSWORD`.

> Alterar `ADMIN_PASSWORD` no `.env` não modifica uma conta que já foi criada no banco.

## Configuração principal

| Variável | Padrão | Uso |
|---|---|---|
| `SECRET_KEY` | gerada no volume | Assina sessões; uma variável explícita tem precedência. |
| `API_KEY` | gerada no volume | Autentica a API e o Ongma Agent; uma variável explícita tem precedência. |
| `SETUP_TOKEN` | arquivo no volume | Autorização opcional do primeiro cadastro gerenciada externamente. |
| `ADMIN_USERNAME` | `admin` | Usuário administrador inicial. |
| `ADMIN_PASSWORD` | vazia | Se definida, cria o administrador legado; caso contrário, usa `/setup`. |
| `TIMEZONE` | `America/Sao_Paulo` | Fuso do agendador e das datas do painel. |
| `MAX_CONCURRENT_EXECUTIONS` | `5` | Limite de automações locais simultâneas. |
| `DEFAULT_TIMEOUT_SECONDS` | `3600` | Timeout padrão das automações. |

Consulte [.env.example](.env.example) para as demais opções.

## Modos de execução

O **Ongma Agent é opcional**. Você pode instalar o Ongma e executar suas automações diretamente no servidor onde a plataforma está rodando, sem instalar ou configurar o agente.

- **Execução local (padrão):** selecione `local` em **Máquina / Worker Alvo**. A automação é executada no ambiente do servidor Ongma, inclusive quando ele está em um container Docker.
- **Execução remota (opcional):** instale o Ongma Agent apenas se precisar executar automações em outra máquina, com acesso aos recursos daquele computador.

> Em instalações Docker, a execução `local` ocorre **dentro do container**, não diretamente no sistema operacional do computador hospedeiro. Para acessar arquivos, dispositivos ou programas externos ao container, configure o ambiente adequadamente ou utilize o agente remoto.

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

## Ongma Agent (opcional)

**Não é necessário instalar o agente para utilizar o Ongma localmente.**

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

Em uma instalação Docker que usa chaves automáticas, consulte a `API_KEY` localmente:

```powershell
docker exec ongma python -c "import json; print(json.load(open('/app/data/internal-credentials.json'))['api_key'])"
```

Trate o valor retornado como segredo e não o envie em mensagens, prints ou logs.

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

## Operar com Docker Compose

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

Os dados ficam no volume declarado como `ongma-data`; o Docker Compose pode adicionar o prefixo do projeto ao nome físico. Não execute `docker compose down -v` salvo se quiser apagar definitivamente o banco e as automações.

## Erros comuns

| Erro | Correção |
|---|---|
| `Conflict. The container name "/ongma" is already in use` | Use o container existente ou execute `docker rm -f ongma` antes de recriá-lo. Remover o container não remove o volume. |
| `No such container: ongma` | Confirme o nome com `docker ps -a`. |
| Arquivo `/app/data/setup-token` inexistente | A instalação já foi configurada ou usa `SETUP_TOKEN`; tente acessar `/login`. |
| Porta `8000` ocupada | Use outra porta no host, como `-p 127.0.0.1:8003:8000`, e acesse `http://localhost:8003`. |
| Container reiniciando ou `unhealthy` | Execute `docker logs --tail 100 ongma`. |
| Builder `ongma-builder` já existe | Execute `docker buildx use ongma-builder`; não tente criá-lo novamente. |
| `no configuration file provided` | Entre na pasta que contém `compose.yaml`. |
| `ADMIN_PASSWORD ... too_short` | Use no mínimo 8 caracteres e recrie o container. |
| API retorna `401` | Confirme `API_KEY` e recrie o container após alterar o `.env`. |
| Agente não conecta | Use o IP do servidor, valide a porta 8000 e confira o firewall. |
| Git não encontrado no agente | Instale Git e reinicie o agente. |

Procedimentos específicos para **WSL 2**, testes da API e execução pelo código-fonte estão no [guia avançado](docs/GUIA_AVANCADO.md).

## Observações de segurança

- Nunca versione `.env`, banco, tokens ou senhas;
- use um token Git somente de leitura para repositórios privados;
- não envie `API_KEY` em mensagens ou prints;
- mantenha o repositório `m4gno/ongma` público no Docker Hub para instalação sem autenticação;
- execute somente uma instância enquanto o banco for SQLite.

## Publicar uma nova versão no Docker Hub

Esta seção é destinada aos mantenedores. Usuários finais precisam apenas executar a imagem publicada.

1. Execute os testes:

```powershell
uv run pytest -q
```

2. Autentique-se e selecione o builder multi-arquitetura:

```powershell
docker login -u m4gno
docker buildx use ongma-builder
docker buildx inspect --bootstrap
```

Se o builder ainda não existir:

```powershell
docker buildx create --name ongma-builder --driver docker-container --use
docker buildx inspect --bootstrap
```

3. Construa e publique AMD64 e ARM64. Ajuste a versão antes de executar:

```powershell
docker buildx build `
  --builder ongma-builder `
  --platform linux/amd64,linux/arm64 `
  --tag m4gno/ongma:0.1.0 `
  --tag m4gno/ongma:latest `
  --push `
  .
```

4. Valide o manifesto publicado:

```powershell
docker buildx imagetools inspect m4gno/ongma:latest
```

O manifesto deve listar `linux/amd64` e `linux/arm64`. Não publique `latest` antes de validar instalação, setup e persistência com um volume novo.

## Licença

Distribuído sob a licença [MIT](LICENSE).
