# Social Media Agent

Agente de social media autônomo para empresas. Toda semana ele pesquisa o que está em alta no
tema da marca, propõe temas no Telegram, cria carrossel e stories no padrão visual da marca,
escreve as legendas no tom dela e publica no Instagram e no LinkedIn via Buffer, sempre com
aprovação humana em dois pontos: o tema e o post pronto. Antes de cada aprovação, um **Revisor**
(Claude Sonnet) confere a etapa; quando o conteúdo ganha com movimento, o **Agent Vídeo** (Claude
Opus) adiciona um slide animado; e a **frente X** escreve tweets de opinião em paralelo.

Versão instalada em `VERSION`; novidades em [CHANGELOG.md](CHANGELOG.md); para atualizar uma
instalação existente, [UPDATE.md](UPDATE.md).

O kit **não traz nenhum tema de conteúdo pré-definido**: na instalação, o setup pergunta o
**tema-macro** da marca, e toda a pesquisa parte dele.

O código é um **motor** único (`agent/`). Tudo o que é de uma empresa (identidade, tom, pilares,
temas de pesquisa, cores, fontes, logos, fotos, documentos) fica num **pacote de marca** em
`brands/<slug>/`, escolhido pela variável `BRAND`. Cada empresa roda como uma instância separada,
nas próprias contas.

**Para instalar numa empresa, siga o [ONBOARDING.md](ONBOARDING.md).**

## Fluxo

```
Segunda 09:00  "Quantos posts esta semana?"  [1] [2] [Nenhum]
      │
Tema?          "Quer indicar algum tema?"  [Indicar um tema] [Pode pesquisar]
               · geral (um assunto ou ângulo para a semana) → vira o direcionamento da pesquisa
               · específico ("Título · https://...", página ou vídeo do YouTube) ou texto colado →
                 o post usa SOMENTE essas referências ("Seu tema"), sem buscar outras fontes
      │
Revisor        Afina o pedido ou direcionamento antes de pesquisar (Claude Sonnet)
      │
Pesquisa       Google Trends, Reddit e X (Apify) + Hacker News + Google News + criadores de
               referência (YouTube/Instagram) + busca na web do modelo → 4 a 5 temas com nota
               (potencial viral, novidade, relevância, autoridade da marca)
      │
Revisor        Classifica: dentro do tema-macro? o que a pesquisa diz está nas fontes (abre os
               links)? aprova, ajusta 1 vez ou descarta
      │
Telegram       Tema com: por que agora, dados com fonte, aplicações práticas, pegada narrativa,
               sinopse, roteiro, pilar e parecer do Revisor. [Aprovar] [Outra ideia] [Direcionar]
      │
Revisor        Revisa a ideia de como montar o post (o ajuste vira orientação da escrita)
      │
Criação        7 a 9 slides (capa, conteúdo, 1 ilustração isométrica com gpt-image-2, CTA) +
               legendas Instagram e LinkedIn → revisão editorial automática (7 critérios, nota 8+)
               → checagem de linguagem → Agent Vídeo decide se entra um slide animado (Claude Opus)
               → render em 2× (feed 4:5 e stories 9:16) + PDF vetorial + MP4 do slide animado
      │
Revisor        Revisa o post pronto (texto + peças): 1 pedido de ajuste, aplicado antes de você ver
      │
Telegram       Prévia com custo por agente (OpenAI, Revisor em destaque, Agent Vídeo), pareceres e
               as FONTES do post (as legendas não citam fontes).
               [Aprovar post] [Pedir ajustes] [Outra legenda] [Direcionar legenda]
      │
Quando?        [Postar agora] [Próximo horário da grade] [Escolher dia e horário (texto livre)]
      │
Buffer         Carrossel no Instagram (JPEG), stories com os mesmos slides, LinkedIn em PDF.
               Confere a fila antes; depois do horário confirma no grupo com os links ou mostra
               o erro com [Tentar de novo] [Esquecer].
```

Pedidos avulsos: `/novopost <assunto>` ou responder a uma mensagem do bot.

**Frente X (Twitter)**, configurada no `/x`: Desligada, Ligada (em paralelo com Instagram e
LinkedIn) ou Só X (Instagram e LinkedIn pausados, ideal para founders); tweets por semana, linguagem
e tom. Ligada, cada post aprovado gera um tweet de opinião forte sobre o assunto e o agente completa
a cota da semana um pouco por dia (todo dia às `X_TIME`), a partir dos temas pesquisados. Todo tweet
passa pelo Revisor e por você: [Aprovar] [Outro tweet] [Direcionar] [Descartar], depois postar agora,
próximo horário do X ou dia e horário livres. Publica pelo canal do X no Buffer.

**Referências enviadas.** Quando o pedido traz um link, um vídeo do YouTube ou um texto de
referência colado (a partir de ~280 caracteres), o agente usa **somente** esse material: não coleta
tendências nem pesquisa outras fontes, e a revisão editorial reprova qualquer fato de fora dele. A
busca na web só é usada para abrir um link que não pôde ser lido direto.

**YouTube.** Links de vídeo (youtube.com/watch, youtu.be, shorts, live) são lidos pela transcrição,
com título e canal. Se o YouTube bloquear a leitura direta (comum em servidores na nuvem) e houver
`APIFY_TOKEN`, a transcrição vem pelo Apify (`pintostudio/youtube-transcript-scraper`).

## Comandos no Telegram

| Comando | O que faz |
|---|---|
| `/start` | Ajuda. Em chat não autorizado, mostra o Chat ID para liberar no `.env` |
| `/semana` | Começa o ciclo da semana agora |
| `/novopost <assunto>` | Post avulso |
| `/pesquisar <foco>` | Refaz a pesquisa da semana com um direcionamento |
| `/status` | Situação da semana e custo OpenAI acumulado |
| `/pendentes` | Reenvia prévia e botões de posts esperando aprovação |
| `/verificar` | Confere se o último post saiu em todas as redes |
| `/x` | Frente X: modo, tweets por semana, linguagem, tom e "gerar um tweet agora" |
| `/tweet <assunto>` | Tweet de opinião sobre um assunto |

Em grupo, o bot só lê comandos e respostas às mensagens dele; o resto da conversa é ignorado.

## Pacote de marca

```
brands/<slug>/
  brand.toml               identidade, público, tom, pilares (2 a 4), pesquisa, visual, regras de linguagem
  knowledge/               documentos da marca (lidos em todo post; o modelo também pode consultar)
  assets/fonts/            fonte da marca (opcional; sem ela usa Inter)
  assets/logo/             logo claro (sobre foto/fundo escuro) e escuro (sobre fundo claro)
  assets/photos/           fotos para capa e CTA (opcional; sem elas, fundo sólido)
  assets/style/            imagem de referência de estilo para as ilustrações (opcional, recomendada)
  apify_sources.json       termos e comunidades do setor para Google Trends, Reddit e X
  reference_sources.json   criadores de conteúdo que a marca acompanha (opcional)
```

- `brands/exemplo/`: empresa fictícia **Atlas**, sem tema de conteúdo; serve só para os testes e a
  prévia visual (o bot não sobe com `BRAND=exemplo`).
- `brands/_template/`: modelo usado por `./scripts/new_brand.sh <slug> "<Nome>"`, que **pergunta o
  tema-macro** e monta a pesquisa (Google News, Hacker News, Google Trends, X) a partir dele.
- `python -m scripts.check_brand <slug>` lista o que falta no pacote.

## Pesquisa: do curto prazo ao macro

O padrão é neutro: últimos 7 dias, sempre dentro do tema-macro, com os quatro critérios do ranking
com o mesmo peso. Para ajustar, use no `brand.toml` (todos opcionais):

| Campo `[research]` | O que faz |
|---|---|
| `scope` | substitui a abertura do prompt de pesquisa (ex.: "tendências dos últimos 3 meses...") |
| `priorities` | o que priorizar dentro do tema (novidades, aplicações práticas, dados...) |
| `mix` | regra de distribuição dos tipos de tema (Novidade, Tendência, Dados e pesquisa...) |
| `weights` | pesos do ranking (`momentum`, `novelty`, `relevance`, `ownability`); só os informados mudam |
| `news_days` | janela do Google News e do Hacker News em dias (padrão 7) |
| `short` | assunto curto usado nas mensagens do bot |

No `apify_sources.json`, aumente as janelas: Google Trends `"timeRange": "today 3-m"` e Reddit com
`/top/?t=month` e `"time": "month"`.

Também opcionais: `[content] copy` e `captions` (regras de texto da marca, que prevalecem sobre as
genéricas e entram na revisão editorial) e, em `[visual]`, `font_display`/`font_display_file`
(fonte de títulos e números), `cover_photo = false` (capa só com texto) e `slide_counter = false`
(sem contador de slides). Exemplos comentados em `brands/_template/brand.toml`.

## Rodar local

```bash
./run.sh setup            # cria o ambiente e instala dependências
./run.sh test             # testes sem chamar nenhuma API (pipeline + Revisor, Agent Vídeo, legendas, X)
BRAND=exemplo ~/.venvs/social-media-agent/bin/python -m tests.render_sample   # gera peças de amostra
./scripts/new_brand.sh minhamarca "Minha Marca"                               # pergunta o tema-macro
BRAND=minhamarca ./run.sh   # sobe o bot (precisa do .env preenchido)
```

## Estrutura

```
agent/
  main.py          entrada (python -m agent.main)
  config.py        variáveis de ambiente + carregamento do pacote de marca
  bot.py           Telegram: ciclo semanal, botões, pedidos, revisões, conferência da publicação
  research.py      sinais (Hacker News, Google News) + pesquisa com web search + ranking
  apify.py         Google Trends, Reddit, X (execução assíncrona com resultados parciais)
  references.py    criadores de referência: YouTube (RSS) e Instagram (Apify)
  content.py       criação/revisão, revisão editorial, checagem de linguagem e de layout, legendas novas
  claude.py        cliente Claude (Anthropic): saída estruturada, leitura de páginas, custo por agente
  reviewer.py      Agent Revisor: pedido, temas, plano, post, legendas e tweets (1 ajuste por etapa)
  video.py         Agent Vídeo: decide, escreve a animação (CSS) e grava o MP4 do slide animado
  xarm.py          frente X: configurações, cota semanal, escrita e revisão dos tweets
  watchdog.py      vigia do polling do Telegram (reinicia o processo se o bot travar)
  illustration.py  slide de ilustração (gpt-image-2 + referência de estilo da marca)
  render/          template das peças + Playwright (PNG 2×, JPEG para redes, PDF vetorial)
  publisher.py     Supabase Storage → Buffer (ou Metricool) + conferência + arquivo
  knowledge.py     documentos da marca no prompt + busca/leitura da base
  prompts.py       instruções do agente, montadas a partir do brand.toml
  costs.py         custo por pesquisa/criação/ajuste, separado por agente (OpenAI, Revisor, Vídeo)
  db.py            estado (SQLite em DATA_DIR)
brands/            pacotes de marca
scripts/           new_brand, check_brand, set_secrets, check_setup, kickoff, buffer_status, update
tests/             teste do pipeline com APIs simuladas + render de amostra
```

## Custos de referência

| Item | Custo |
|---|---|
| OpenAI (pesquisa + escrita + revisão com `gpt-6-luna`) | centavos por post |
| Ilustração (`gpt-image-2`, 1 por post) | cerca de US$ 0,17 |
| Revisor (`claude-sonnet-5-5`: pedido, temas, plano, post) | cerca de US$ 0,10 a 0,25 por post |
| Agent Vídeo (`claude-opus-5-5`, só quando entra vídeo) | cerca de US$ 0,15 a 0,40 por vídeo |
| Tweet (escrita + Revisor) | cerca de US$ 0,01 a 0,03 |
| Apify (Google Trends, Reddit, X, Instagram) | cerca de US$ 0,35 a 0,50 por pesquisa; plano grátis de US$ 5/mês |
| Buffer | grátis até 3 canais e 10 posts agendados por canal |
| Railway | plano Hobby, cerca de US$ 5/mês |
| Supabase | grátis |

A prévia de cada post no Telegram mostra o custo real daquele post por agente, com o gasto do
Revisor em destaque; a confirmação da publicação repete o custo final.

## Pontos a conferir no primeiro uso

- No primeiro post, use o **horário da grade** e confira na fila do Buffer antes de usar "postar agora".
- Buffer grátis: stories espelhando todos os slides ocupam quase toda a fila (1 post agendado por
  vez). Para agendar vários, use `STORY_FRAMES=4` ou plano pago.
- Fontes de marca precisam permitir uso comercial e embutir em imagens.
