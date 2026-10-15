# Onboarding de uma empresa nova

O agente é um **motor** único (`agent/`) que roda para qualquer marca. Tudo que é específico de
uma empresa fica num **pacote** em `brands/<slug>/`. Cada empresa roda como um serviço separado,
nas contas dela, com `BRAND=<slug>`.

```
brands/<slug>/
  brand.toml               identidade, público, tom, pilares, pesquisa, visual, regras de linguagem
  knowledge/               documentos da marca (o agente lê em todo post e pode consultar)
  assets/fonts/            fonte da marca (.otf/.ttf, regular e medium)
  assets/logo/             logo claro (sobre foto) e escuro (sobre fundo claro), SVG ou PNG
  assets/photos/           6+ fotos da marca para capa e CTA (≥ 2700 px de altura)
  assets/style/            imagem de referência para as ilustrações (opcional, recomendada)
  apify_sources.json       fontes de tendência (Google Trends, Reddit, X)
  reference_sources.json   criadores que a marca acompanha (YouTube, Instagram)
```

O kit não traz tema de conteúdo pré-definido: o setup pergunta o **tema-macro** da marca.
`brands/exemplo` (empresa fictícia Atlas) serve só para testes e prévia visual.

## 1. O que pedir ao cliente

**Marca**
- **Tema-macro do conteúdo**: o assunto amplo que o agente vai pesquisar e sobre o qual vai postar
  (ex.: "nutrição esportiva"), de preferência também em inglês para fontes internacionais.
- Apresentação da empresa: quem é, o que oferece, para quem, como fala, o que nunca dizer.
- 2 a 4 pilares ou serviços (aparecem no último slide de todo post).
- Identidade visual: cores (fundo, texto, acento), arquivo da fonte, logo claro e escuro.
- Fotos para capa e CTA (ou autorização para usar um banco de imagens específico).
- 2 ou 3 criadores de conteúdo de referência do setor (YouTube e/ou Instagram).
- Site, @ do Instagram e página do LinkedIn.

**Contas (no nome do cliente)**
| Conta | Para quê | Custo de referência |
|---|---|---|
| Railway | servidor 24/7 | plano Hobby, cerca de US$ 5/mês |
| OpenAI (API) | pesquisa, escrita, ilustrações | cerca de US$ 0,20 a 0,40 por post |
| Anthropic (API) | Revisor (Claude Sonnet) e Agent Vídeo (Claude Opus) | cerca de US$ 0,10 a 0,60 por post |
| Apify | Google Trends, Reddit, X, Instagram | plano grátis (US$ 5/mês de crédito) |
| Buffer | publicar no Instagram, LinkedIn e X | grátis até 3 canais |
| Supabase | hospedar as imagens para o Buffer | grátis |
| Telegram | bot de aprovação + grupo do time | grátis |

Cada empresa roda o agente na **própria** plataforma de backend (Railway, Render, Fly.io, VPS ou
outra). Nunca use a conta ou o projeto de outra empresa, nem o da OSLO. Se outra pessoa ou agência
fizer a instalação, a empresa a convida como membro da conta dela; as chaves são cadastradas por
quem tem acesso, com o script de digitação oculta (passo 4), sem que ninguém precise vê-las.

## 2. Baixar o kit e criar o pacote

```bash
git clone https://github.com/OSLOAISTUDIO/social-media-agent-kit.git <pasta>
cd <pasta>
./run.sh setup
```

Para versionar a instalação da empresa no GitHub dela, aponte o remoto para um repositório
próprio (`git remote set-url origin <repo da empresa>`). Assim a empresa tem o código e o pacote
de marca dela, e o kit original continua disponível para atualizações (`git remote add kit ...`).

### Criar e preencher o pacote

```bash
./scripts/new_brand.sh <slug> "<Nome da marca>"
```

O script pergunta o **tema-macro** (e a versão em inglês, opcional) e já monta a pesquisa a partir
dele: consultas do Google News e do Hacker News, termos do Google Trends e do X. O Reddit fica
desligado até você listar as comunidades do tema em `apify_sources.json`. Sem perguntas:
`./scripts/new_brand.sh <slug> "<Nome>" "<tema-macro>" "<tema em inglês>"`.

1. Preencha `brands/<slug>/brand.toml` (tudo marcado PENDENTE) e `knowledge/marca.md`.
2. Coloque fontes, logos, fotos e a referência de ilustração em `assets/`.
3. Ajuste `apify_sources.json` (termos e comunidades do setor) e `reference_sources.json`.
   Para uma pesquisa mais macro, veja "Pesquisa: do curto prazo ao macro" no README (`scope`,
   `weights`, `news_days` e janelas maiores no Apify).
4. Confira o que falta:

```bash
~/.venvs/social-media-agent/bin/python -m scripts.check_brand <slug>
```

5. Veja as peças sem gastar API (usa `tests/sample_post.json` ou `tests/sample_post_<slug>.json`):

```bash
BRAND=<slug> ~/.venvs/social-media-agent/bin/python -m tests.render_sample
```

6. Faça commit no repositório da empresa.

## 3. Subir na plataforma da empresa

O agente é um **worker** (sem porta HTTP, usa long polling do Telegram): precisa de um processo
sempre ligado, variáveis de ambiente, um volume persistente em `/data` e deploy a partir do Dockerfile.
Qualquer plataforma com isso serve. O exemplo abaixo é o Railway, com a conta **da empresa** logada
(`railway whoami` confere antes de tudo):

```bash
railway init --name <slug>-social-agent
railway add --service agent
railway volume add --mount-path /data
railway variable set --skip-deploys BRAND=<slug> OPENAI_MODEL=gpt-6-luna OPENAI_MODEL_FAST=gpt-6-luna \
  OPENAI_ILLUSTRATION_MODEL=gpt-image-2 PUBLISHER=buffer TIMEZONE=America/Sao_Paulo
railway up --detach -m "primeiro deploy <slug>"
```

Em outra plataforma, crie o serviço a partir do Dockerfile do repositório da empresa, monte o volume
em `/data`, cadastre as variáveis do `.env.example` e confira nos logs a linha `Agent de social media
no ar`. Os scripts `set_secrets.sh`, `kickoff.py` e `buffer_status.py` usam o CLI do Railway: fora
dele, cadastre as chaves pelo painel da plataforma (sem colar em chat).

Sem as chaves, o serviço sobe e fica aguardando (não entra em loop de erro).

## 4. Chaves e grupo de aprovação

1. Telegram: criar o bot no @BotFather, criar o grupo do time e adicionar o bot.
2. Cadastrar as chaves (digitação oculta, direto no Railway):
   ```bash
   ./scripts/set_secrets.sh agent
   ```
   Erros comuns, que o `check_setup` mostra como HTTP 401: colar a mesma chave duas vezes (fica
   com o dobro do tamanho; o script avisa) e colar a chave de um serviço no campo de outro. Setas
   e marcadores de colagem que o terminal mande junto são limpos automaticamente.
3. No grupo, mandar `/start@<bot>`: o bot responde o ID do grupo. Rodar o script de novo só com
   `TELEGRAM_ALLOWED_CHAT_IDS` (grupo primeiro, IDs pessoais depois, separados por vírgula).
4. Frente X (opcional): conectar o perfil do X no Buffer e, no Telegram, ligar em `/x` (modo,
   tweets por semana, linguagem e tom).
5. Conferir todas as conexões e criar o bucket público do Supabase:
   ```bash
   ~/.venvs/social-media-agent/bin/python -m scripts.check_setup --fix   # com as variáveis carregadas (no Railway: railway run --service agent ...)
   ```

## 5. Primeiro ciclo

No grupo: `/semana@<bot>`. Depois de escolher quantos posts, o bot pergunta se você quer indicar um
tema: um direcionamento geral vira o foco da pesquisa; um link (página ou vídeo do YouTube) ou um
texto de referência vira a base do post, e o agente usa só esse material, sem buscar outras fontes.
Ou toque em "Pode pesquisar" para seguir com as tendências do tema-macro.

As legendas não citam fontes: as fontes de cada post aparecem na prévia de aprovação no Telegram. Aprovar um tema, aprovar o post e escolher o **horário da grade**
(não "postar agora"), para conferir na fila do Buffer antes de publicar. Depois do horário, o bot
confirma no grupo com os links ou avisa o que falhou.

## Atualizações

O código é o mesmo para todas as marcas. Cada versão nova do kit sai como patch (tag + CHANGELOG);
na instalação da empresa, `./scripts/update.sh --push` junta a versão nova, roda os testes e envia ao
GitHub da empresa, e o Railway faz o deploy. Detalhes e caminho sem git: [UPDATE.md](UPDATE.md).
