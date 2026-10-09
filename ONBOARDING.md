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

Exemplo completo e funcionando: `brands/exemplo` (empresa fictícia Atlas).

## 1. O que pedir ao cliente

**Marca**
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
| Apify | Google Trends, Reddit, X, Instagram | plano grátis (US$ 5/mês de crédito) |
| Buffer | publicar no Instagram e LinkedIn | grátis até 3 canais |
| Supabase | hospedar as imagens para o Buffer | grátis |
| Telegram | bot de aprovação + grupo do time | grátis |

Se outra pessoa ou agência fizer a instalação, o cliente a convida como membro no Railway; as
chaves são cadastradas por quem tem acesso, com o script de digitação oculta (passo 4), sem que
ninguém precise ver as chaves.

## 2. Baixar o kit e criar o pacote

```bash
git clone https://github.com/OSLOTEAM/social-media-agent-kit.git <pasta>
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

## 3. Subir no Railway do cliente

Com a conta do cliente logada (`railway login`), na pasta do projeto:

```bash
railway init --name <slug>-social-agent
railway add --service agent
railway volume add --mount-path /data
railway variable set --skip-deploys BRAND=<slug> OPENAI_MODEL=gpt-6-luna OPENAI_MODEL_FAST=gpt-6-luna \
  OPENAI_ILLUSTRATION_MODEL=gpt-image-2 PUBLISHER=buffer TIMEZONE=America/Sao_Paulo
railway up --detach -m "primeiro deploy <slug>"
```

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
4. Conferir todas as conexões e criar o bucket público do Supabase:
   ```bash
   railway run --service agent ~/.venvs/social-media-agent/bin/python -m scripts.check_setup --fix
   ```

## 5. Primeiro ciclo

No grupo: `/semana@<bot>`. Depois de escolher quantos posts, o bot pergunta se você quer indicar um
tema: um direcionamento geral ("tendências de marketing para 2027") vira o foco da pesquisa; um
conteúdo específico com título e link ("Título · https://...") vira a base do post. Ou toque em
"Pode pesquisar" para seguir só com as tendências. Aprovar um tema, aprovar o post e escolher o **horário da grade**
(não "postar agora"), para conferir na fila do Buffer antes de publicar. Depois do horário, o bot
confirma no grupo com os links ou avisa o que falhou.

## Atualizações

O código é o mesmo para todas as marcas. Depois de melhorar o motor, cada empresa recebe a versão
nova com um `railway up` na conta dela (ou pelo deploy automático do GitHub, se o Railway do
cliente tiver acesso ao repositório).
