# Prompt de instalação

Cole o texto abaixo numa sessão nova do Claude Code, aberta na pasta da empresa (onde ficam os
materiais dela: apresentação, identidade visual, fontes, logos, fotos). Troque o que está entre
colchetes.

---

Quero instalar o Social Media Agent para a empresa [NOME DA EMPRESA], usando o kit público
https://github.com/OSLOTEAM/social-media-agent-kit (última versão; veja VERSION e CHANGELOG.md).
Siga o ONBOARDING.md do kit do início ao fim e me peça só o que você não conseguir resolver sozinho.

Contexto da empresa:
- O que faz e para quem: [resumo em 2 ou 3 frases, ou "leia os materiais desta pasta"]
- Tema-macro do conteúdo: [ex.: "nutrição esportiva"] (em inglês: [ex.: "sports nutrition"])
- Materiais da marca nesta pasta: [onde estão apresentação, cores, fontes, logos e fotos]
- Criadores de referência do setor (opcional): [canais do YouTube e perfis do Instagram]
- Frente X (Twitter): [desligada | ligada junto com Instagram e LinkedIn | só X], [N] tweets por
  semana, linguagem [ex.: direta], tom [ex.: opinião forte]

Como quero que você trabalhe:
1. Clone o kit numa subpasta `social-media-agent`, rode `./run.sh setup` e crie o pacote da marca com
   `./scripts/new_brand.sh <slug> "<Nome>" "<tema-macro>" "<tema em inglês>"`.
2. Preencha `brands/<slug>/brand.toml`, `knowledge/` e `assets/` a partir dos materiais desta pasta
   (identidade, público, tom, pilares, cores, fontes com licença comercial, logos claro e escuro,
   fotos). Rode `python -m scripts.check_brand <slug>` até sair PRONTA e gere peças de amostra com
   `tests.render_sample` para eu aprovar o visual antes de subir.
3. Versione no GitHub da empresa (repositório privado próprio) e mantenha o remoto `kit` apontando
   para o repositório público, para as atualizações (`./scripts/update.sh`).
4. Suba no Railway da empresa (serviço `agent`, volume em `/data`, `BRAND=<slug>`). Confira com
   `railway whoami` que está na conta certa antes de qualquer comando.
5. Chaves: nunca peça nem leia chaves no chat. Eu cadastro com `./scripts/set_secrets.sh agent`
   (digitação oculta, direto no Railway). Contas necessárias: Telegram (bot no @BotFather + grupo),
   OpenAI, Anthropic (Revisor e Agent Vídeo; sem crédito o post segue sem eles), Apify (opcional),
   Buffer (Instagram, LinkedIn e, se usar, X) e Supabase (bucket público).
6. Rode `railway run --service agent <venv>/bin/python -m scripts.check_setup --fix` e só siga quando
   tudo estiver ✓.
7. Primeiro ciclo no grupo do Telegram: `/semana`, aprovar tema e post, e agendar no horário da grade
   (não "postar agora") para conferir na fila do Buffer. Se a frente X estiver ligada, configure em `/x`.

Regras: personalização da marca fica em `brands/<slug>/` (não altere o motor em `agent/` sem me
perguntar, para as próximas atualizações do kit entrarem sem conflito); dados confidenciais de
clientes da empresa nunca entram nos posts.
