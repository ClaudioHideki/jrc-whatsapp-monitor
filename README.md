# JRC Monitor WhatsApp v2

Versão atualizada do monitor local.

## O que mudou no dashboard

- continua atualizando automaticamente a cada 60 segundos;
- continua usando somente GET nas Evolution APIs;
- separa visualmente cada fonte/API;
- agrupa as instâncias por cliente dentro de cada API;
- cada cliente pode ser expandido/recolhido;
- junta Caixa / Instância;
- junta Número / Perfil WhatsApp;
- junta Status / Código;
- mantém Última desconexão;
- adiciona uma legenda completa para 🟢 🟡 🔴 ⚫ ⚪;
- explica que um código 401 pode ser apenas histórico;
- mantém pesquisa e filtros.

## Rodar no Windows

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Preencha o `.env` com login, senha, SECRET_KEY e as chaves das APIs ativas.

Depois:

```powershell
.\.venv\Scripts\python.exe app.py
```

Abra:

```text
http://127.0.0.1:5000
```

## Segurança

A aplicação consulta as Evolution usando exclusivamente:

```text
GET /instance/fetchInstances
```

As chaves ficam apenas no backend e não são enviadas ao navegador.


## v3 visual
- identidade azul institucional JRC
- cabeçalho azul-marinho
- fundo azul-claro no tema claro
- tema escuro azul profundo
- seletor Claro/Escuro com preferência salva no navegador
- colunas padronizadas entre todos os clientes
- seta para expandir/recolher
- imagem institucional na tela de login
- lógica de consulta GET preservada


## Ajustes v4 sobre a base
- KPIs clicáveis com filtro por status.
- Removida a seção visual "Fontes Evolution" do corpo da página.
- Clientes recolhidos por padrão.
- Bolinhas de status por cliente são clicáveis.
- Clicar em uma bolinha abre o cliente e mostra apenas aquele status.
- Clicar novamente na mesma bolinha remove o filtro local.
- Legenda movida para o final e recolhível.
- Atualização automática de 60 segundos preservada.
- Consultas Evolution continuam somente GET.
