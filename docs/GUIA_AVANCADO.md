# Guia avançado do Ongma

Este documento reúne procedimentos que não são necessários na instalação padrão.

## WSL 2 em modo `mirrored`: agente remoto não conecta

> **Uso exclusivo:** aplique este procedimento somente quando o servidor Ongma estiver dentro do WSL 2 em modo `mirrored`. Não use em Linux nativo, Windows sem WSL, VM comum ou servidor externo.

Na máquina do agente:

```powershell
Test-NetConnection IP-DO-SERVIDOR -Port 8000
```

Se `TcpTestSucceeded` for `False`, execute no PowerShell administrativo do servidor Windows:

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

O erro informando que `portproxy` não existe pode ser ignorado. Depois, teste novamente da máquina do agente.

Referência: [rede no WSL](https://learn.microsoft.com/windows/wsl/networking).

## Testar a API

```bash
curl -i \
  -H "X-API-Key: SUA_API_KEY" \
  http://localhost:8000/api/automations
```

Resultado esperado: HTTP `200`. Uma resposta `[]` significa que ainda não existem automações.

Endpoints principais:

```text
GET  /api/automations
POST /api/automations/{id}/run
GET  /api/executions/{id}
POST /api/executions/{id}/cancel
```

OpenAPI: `http://localhost:8000/docs`.

## Executar pelo código-fonte

Requisitos: Python 3.12+, `uv` e Git.

```powershell
Copy-Item .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Testes:

```powershell
uv run pytest -q
go test -C agent ./...
```
