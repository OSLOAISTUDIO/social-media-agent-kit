# Versões

Para atualizar uma instalação existente: [UPDATE.md](UPDATE.md) (em resumo, `./scripts/update.sh`).

## 1.1.3 · 2026-10-11

- Documentação: cada empresa sobe o agente na **própria** plataforma de backend (Railway, Render, Fly.io,
  VPS ou outra), sem usar a conta ou o projeto de outra empresa. Railway continua como exemplo. Nenhum
  código mudou, então não precisa atualizar a instalação só por causa desta versão.

## 1.1.2 · 2026-10-11

- **Anthropic sem crédito (ou fora do ar) nunca para o post.** O post, a pesquisa e os tweets seguem
  normalmente, só sem Revisor e sem Agent Vídeo, e o Telegram avisa na prévia, no cartão do tweet,
  depois da pesquisa e no `/status` ("API Anthropic sem crédito: ... recarregue em Plans & Billing").
  Qualquer falha inesperada do cliente Claude também cai nesse caminho.
- Regra de fonte nas legendas por marca (`prompts.CAPTIONS_CITE_SOURCES`; no kit, legendas sem fonte).
- `PROMPT_INSTALACAO.md`: prompt pronto para instalar o agente numa empresa nova com o Claude Code.

## 1.1.1 · 2026-10-11

- `update.sh` roda de qualquer pasta da instalação e também direto do GitHub, para quem ainda não tem o
  script: `zsh <(curl -fsSL https://raw.githubusercontent.com/OSLOTEAM/social-media-agent-kit/main/scripts/update.sh) --push`.
  Se os testes dos agentes falharem, não envia ao GitHub (a versão nova fica no commit local para conferir).
- Pedido livre com link, vídeo ou texto de referência usa a mensagem inteira (o resumo perdia o link).
- Mensagens de aprovação e publicação respeitam `PUBLISH_NETWORKS` (LinkedIn opcional).

## 1.1.0 · 2026-10-11

**Agent Revisor (Claude Sonnet 5.5).** Confere cada etapa antes de ela chegar ao humano, com no
máximo 1 pedido de alteração por etapa:
- otimiza o pedido ou direcionamento antes da pesquisa;
- classifica os temas pesquisados: dentro do tema-macro? o que a pesquisa diz está mesmo nas fontes
  (ele abre os links)? aprova, ajusta uma vez ou descarta (descartados vão para o fim da fila);
- revisa a ideia de como o post vai ser montado (o ajuste vira orientação obrigatória da escrita);
- revisa o post pronto, texto e peças renderizadas;
- revisa legendas novas e tweets.
Se o Claude não estiver configurado ou falhar, o fluxo segue sem o revisor.

**Custo por agente no Telegram.** A prévia e a confirmação da publicação mostram o custo do post
dividido entre OpenAI, Revisor e Agent Vídeo, com o gasto do Revisor em destaque.

**Botões de legenda.** Na prévia do post: "Outra legenda" e "Direcionar legenda" (os slides não mudam).

**Agent Vídeo (Claude Opus 5.5).** Decide, post a post, se um slide animado explica melhor o
conteúdo (fluxo, processo, antes e depois, números crescendo). Quando sim, escreve a animação nas
cores e fontes da marca, o slide entra logo depois da ilustração e vai como vídeo MP4 no carrossel
do Instagram; LinkedIn (PDF) e stories usam o quadro final. `VIDEO_MODE=auto|off|always`.

**Frente X (Twitter).** Tweets de opinião forte, mais autônomos, em paralelo com Instagram e LinkedIn:
- `/x` no Telegram: Desligado, Ligado ou Só X (Instagram e LinkedIn pausados), tweets por semana,
  linguagem e tom (com opção de texto livre);
- ligado: cada post aprovado gera um tweet sobre o assunto, e o agente completa a cota semanal
  sozinho, um pouco por dia, a partir dos temas pesquisados na semana;
- todo tweet passa pelo Revisor e pela aprovação: Aprovar, Outro tweet, Direcionar, Descartar; depois
  postar agora, próximo horário do X ou dia e horário livres; publicação pelo canal do X no Buffer;
- `/tweet <assunto>` pede um tweet na hora.

**Também nesta versão**
- Vigia do polling do Telegram: se o bot fica 10 minutos sem resposta, o processo reinicia sozinho.
- Pesos do ranking voltam a favorecer potencial viral e novidade (0,40 / 0,25 / 0,20 / 0,15) e os
  prompts voltam a priorizar lançamentos e novidades com aplicação prática.
- O modelo de marca volta com consultas e comunidades de marketing e vendas como ponto de partida
  (junto com o tema-macro do setup); o post de amostra dos testes volta a ser o de agentes de IA
  (só como exemplo de estrutura).
- `set_secrets.sh` aceita chaves específicas: `./scripts/set_secrets.sh agent ANTHROPIC_API_KEY`.

**Variáveis novas**
| Variável | Obrigatória | Padrão |
|---|---|---|
| `ANTHROPIC_API_KEY` | para Revisor e Agent Vídeo | (vazia: os dois ficam desligados) |
| `REVIEWER`, `REVIEWER_MODEL`, `REVIEWER_EFFORT` | não | `on`, `claude-sonnet-5-5`, `medium` |
| `VIDEO_MODE`, `VIDEO_MODEL`, `VIDEO_EFFORT`, `VIDEO_SECONDS` | não | `auto`, `claude-opus-5-5`, `high`, `8` |
| `X_MODE`, `X_TWEETS_PER_WEEK`, `X_LANGUAGE`, `X_TONE` | não | `off`, `5`, `direta`, `opinião forte` (o `/x` muda) |
| `X_TIME`, `X_SLOTS`, `X_MAX_CHARS`, `BUFFER_X_CHANNEL_ID` | não | `11:00`, `12:30,18:30`, `280`, descoberto sozinho |

**Depois de atualizar**
1. `./scripts/set_secrets.sh agent ANTHROPIC_API_KEY` (chave em platform.claude.com).
2. Para usar o X: conectar o perfil do X no Buffer (o plano grátis comporta 3 canais) e ligar no `/x`.
3. Banco de dados: as tabelas novas são criadas sozinhas na primeira subida.

## 1.0.0 · 2026-10-10

Primeira versão pública do kit: motor + pacotes de marca, tema indicado na pergunta da semana,
leitura de links e vídeos do YouTube (só as referências enviadas), pesquisa configurável, legendas
sem fonte (fontes só no Telegram), opções visuais por marca.
