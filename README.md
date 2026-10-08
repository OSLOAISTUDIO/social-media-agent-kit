# Social Media Agent

Agente de social media autônomo para empresas. Toda semana ele pesquisa o que está em alta no
tema da marca, propõe temas no Telegram, cria carrossel e stories no padrão visual da marca,
escreve as legendas no tom dela e publica no Instagram e no LinkedIn via Buffer, sempre com
aprovação humana em dois pontos: o tema e o post pronto.

O código é um **motor** único (`agent/`). Tudo o que é de uma empresa (identidade, tom, pilares,
temas de pesquisa, cores, fontes, logos, fotos, documentos) fica num **pacote de marca** em
`brands/<slug>/`, escolhido pela variável `BRAND`. Cada empresa roda como uma instância separada,
nas próprias contas.

**Para instalar numa empresa, siga o [ONBOARDING.md](ONBOARDING.md).**

## Fluxo

```
Segunda 09:00  "Quantos posts esta semana?"  [1] [2] [Nenhum]
      │
Pesquisa       Google Trends, Reddit e X (Apify) + Hacker News + Google News + criadores de
               referência (YouTube/Instagram) + busca na web do modelo → 4 a 5 temas com nota
               (potencial viral, novidade, relevância, autoridade da marca)
      │
Telegram       Tema com: por que agora, dados com fonte, aplicações práticas, pegada narrativa,
               sinopse, roteiro e pilar. [Aprovar] [Outra ideia] [Direcionar]
      │
Criação        7 a 9 slides (capa, conteúdo, 1 ilustração isométrica com gpt-image-2, CTA) +
               legendas Instagram e LinkedIn → revisão editorial automática (7 critérios, nota 8+)
               → checagem de linguagem → render em 2× (feed 4:5 e stories 9:16) + PDF vetorial
      │
Telegram       Prévia com custo OpenAI e nota da revisão. [Aprovar post] [Pedir ajustes]
      │
Quando?        [Postar agora] [Próximo horário da grade] [Escolher dia e horário (texto livre)]
      │
Buffer         Carrossel no Instagram (JPEG), stories com os mesmos slides, LinkedIn em PDF.
               Confere a fila antes; depois do horário confirma no grupo com os links ou mostra
               o erro com [Tentar de novo] [Esquecer].
```

Pedidos avulsos: `/novopost <assunto>` ou responder a uma mensagem do bot.

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

- `brands/exemplo/`: empresa fictícia **Atlas**, completa e funcionando (padrão de `BRAND`).
- `brands/_template/`: modelo usado por `./scripts/new_brand.sh <slug> "<Nome>"`.
- `python -m scripts.check_brand <slug>` lista o que falta no pacote.

## Rodar local

```bash
./run.sh setup            # cria o ambiente e instala dependências
./run.sh test             # testes sem chamar nenhuma API
BRAND=exemplo ~/.venvs/social-media-agent/bin/python -m tests.render_sample   # gera peças de amostra
./run.sh                  # sobe o bot (precisa do .env preenchido)
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
  content.py       criação/revisão, revisão editorial, checagem de linguagem e de layout
  illustration.py  slide de ilustração (gpt-image-2 + referência de estilo da marca)
  render/          template das peças + Playwright (PNG 2×, JPEG para redes, PDF vetorial)
  publisher.py     Supabase Storage → Buffer (ou Metricool) + conferência + arquivo
  knowledge.py     documentos da marca no prompt + busca/leitura da base
  prompts.py       instruções do agente, montadas a partir do brand.toml
  costs.py         custo OpenAI por pesquisa/criação/ajuste
  db.py            estado (SQLite em DATA_DIR)
brands/            pacotes de marca
scripts/           new_brand, check_brand, set_secrets, check_setup, kickoff, buffer_status
tests/             teste do pipeline com APIs simuladas + render de amostra
```

## Custos de referência

| Item | Custo |
|---|---|
| OpenAI (pesquisa + escrita + revisão com `gpt-6-luna`) | centavos por post |
| Ilustração (`gpt-image-2`, 1 por post) | cerca de US$ 0,17 |
| Apify (Google Trends, Reddit, X, Instagram) | cerca de US$ 0,35 a 0,50 por pesquisa; plano grátis de US$ 5/mês |
| Buffer | grátis até 3 canais e 10 posts agendados por canal |
| Railway | plano Hobby, cerca de US$ 5/mês |
| Supabase | grátis |

A prévia de cada post no Telegram mostra o custo OpenAI real daquele post.

## Pontos a conferir no primeiro uso

- No primeiro post, use o **horário da grade** e confira na fila do Buffer antes de usar "postar agora".
- Buffer grátis: stories espelhando todos os slides ocupam quase toda a fila (1 post agendado por
  vez). Para agendar vários, use `STORY_FRAMES=4` ou plano pago.
- Fontes de marca precisam permitir uso comercial e embutir em imagens.
