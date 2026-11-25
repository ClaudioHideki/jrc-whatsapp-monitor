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

## Monitoramento PABX

A aba **Monitoramento PABX** usa o mesmo login do painel WhatsApp. A conta
configurada em `LOGIN_USER` é a administradora desta instalação. A tela
**Configuração** abre para essa conta e permite testar, salvar, ativar e desativar
a integração. A versão atual monitora um PABX por instalação. O banco associa a
configuração criptografada a um ID, e cada coletor mantém seu próprio token,
estado operacional e cache CDR. Uma futura instalação multiempresa precisará
associar usuários aos IDs e iniciar um coletor por perfil autorizado.

### Configurar e executar

1. Instale as dependências e copie `.env.example` conforme a seção para Windows.
   Defina uma senha do monitor exclusiva e uma `SECRET_KEY` aleatória com pelo
   menos 32 caracteres. Mantenha esses valores no ambiente ou no `.env` local.
   O login do painel fica indisponível enquanto a senha ou a chave de sessão
   estiver ausente ou com um dos valores iniciais do projeto.
2. Gere uma chave separada para criptografar a configuração.

   ```powershell
   .\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Guarde a saída em `PABX_CONFIG_KEY` no servidor. Preserve a chave entre
   reinícios e armazene-a separadamente do backup do SQLite. Para Docker, monte
   um volume persistente e aponte `PABX_CONFIG_DB` para um arquivo nele.
3. Rode `app.py` ou o contêiner do projeto. A configuração `gunicorn.conf.py`
   exige um worker com quatro threads e sem preload. Cada banco permite um
   único processo coletor, inclusive durante a troca de workers. Em produção,
   sirva o monitor atrás de HTTPS e defina
   `SESSION_COOKIE_SECURE=1`. A coleta reinicia com o servidor em execução
   quando a configuração persistida está ativa. Configure 15 segundos para a
   cadência homologada. A pausa começa depois de cada ciclo. Um ciclo faz
   quatro GETs quando as duas listagens cabem em uma página cada. O histórico,
   a exportação e as gravações são consultados sob demanda.
4. Em **Monitoramento PABX → Configuração**, informe a empresa, a origem HTTPS
   do PABX, login de leitura, senha, intervalo e `accountcode`. Para uma CA
   privada, monte o arquivo PEM e informe o caminho em `PABX_CA_BUNDLE`. O
   navegador envia as credenciais ao servidor do monitor no cadastro. O
   SQLite guarda a configuração com criptografia autenticada Fernet, e o
   servidor não preenche novamente login ou senha no formulário.
5. Use **Testar conexão** para validar o login e uma consulta ao painel de
   ramais. Confirme com o fornecedor o escopo das quatro rotas operacionais,
   marque a confirmação e **Ativar monitoramento**, depois salve. O token
   fica em memória, é reaproveitado entre rotas e renovado por novo login
   conforme a validade informada ou após um 401. Um 403 sinaliza falta de
   permissão. A coleta usa o timeout configurável `PABX_TIMEOUT_SECONDS`.
6. Para habilitar o histórico, confirme separadamente a segregação de
   `relatorio-cdr/search` pelo fornecedor e informe o `cliente_id` autorizado.
   O CDR usa o contrato observado no notebook, com filtros de período no JSON
   e a página na URL da consulta. A aplicação valida o `cliente_id` de cada
   linha e rejeita a resposta se houver registro de outro cliente ou sem ID.

A tela de operação informa o horário da última coleta e mantém a última
leitura durante uma falha de comunicação. Se houver rejeição de credenciais,
permissão ou escopo, os dados operacionais são retirados da tela e o horário
da última coleta permanece visível. O estado de um ramal e a pausa do agente
em uma fila são exibidos em colunas distintas.

O CDR apresenta uma linha por registro, inclusive quando o `uniqueid` se
repete. Gráficos, filtros e exportação CSV usam essa unidade de contagem.
Origem e destino são mascarados. As gravações elegíveis da mesma origem
podem ser ouvidas ou baixadas. **Somente com gravação** filtra os registros já
consultados e se aplica ao CSV. O áudio é solicitado por GET somente ao clicar
em **Ouvir gravação** ou **Baixar gravação**. O servidor converte WAV para MP3
em trânsito, inclusive para WAV com áudio GSM. A URL original
fica no servidor. A homologação precisa
confirmar a consolidação de chamadas, a semântica das durações, o fuso do
relatório e a validade e renovação do token. A consulta aceita períodos
de até 31 dias e até 100 páginas. Se o limite for excedido, reduza o período.

**Baixar gravações (ZIP)** inclui os áudios elegíveis dos resultados filtrados
em todas as páginas. O botão solicita os arquivos por GET somente no clique, e
o servidor envia o ZIP em fluxo contínuo. Referências iguais entram uma única
vez. Cada ZIP aceita até 1.000 referências e 256 MiB de áudio, mais os
cabeçalhos do arquivo. O servidor aceita um ZIP por vez e até dois fluxos
de áudio simultâneos, contando o ZIP. Se uma gravação falhar ou o limite for
atingido, `resultado.txt` identifica arquivos omitidos ou incompletos.
Os nomes usam a data e a hora do CDR e o ramal identificado no cadastro da
conta, como `gravacao-2026-11-05_09-00-00_ramal-101.mp3`. Quando o ramal
não pode ser identificado, o nome termina em `ramal-nao-identificado`.

### Coleta local de ramais e filas via HTTP

Na execução restrita ao computador local, `PABX_LOCAL_HTTP_ORIGIN` pode
autorizar uma única origem HTTP exata, formada pelo esquema, endereço e porta
do PABX. Use esse ajuste somente quando o tráfego sem criptografia até o PABX
for aceito para o ambiente. O formulário aceita essa origem em uma sessão
local. O servidor de desenvolvimento escuta em loopback com essa opção ativa.
O Gunicorn ignora essa configuração e exige uma origem HTTPS. Guarde a origem
na configuração persistente do servidor local para retomar a coleta após reiniciar.

Nesse modo, a coleta automática usa POST para obter ou renovar o token e
GET para as listas e os dashboards de ramais e filas.
O dashboard informa o total de chamadas ativas e identifica os ramais em
ligação pelo campo `extensionsInCall` da resposta do escopo autorizado.
Com o cliente CDR confirmado, **Consultar** e **Exportar CSV** usam o POST
de pesquisa do CDR. O servidor valida o `cliente_id` em todas as páginas.

### Testes locais

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Os testes usam respostas sanitizadas e exercitam as rotas Flask, a coleta
automática, os controles de acesso, o CDR e o monitor de WhatsApp.
