# Atualizar uma instalação existente

Cada versão nova do kit sai como um **patch**: uma tag (`v1.1.0`) no repositório público, com as
mudanças descritas no [CHANGELOG.md](CHANGELOG.md). O kit nunca mexe no pacote da sua marca
(`brands/<slug>/`); só no motor (`agent/`), nos scripts, nos testes e no modelo `brands/_template`.

## Caminho rápido (instalação feita com git, o caso normal)

Na pasta da instalação da empresa:

```bash
./scripts/update.sh --push
./scripts/set_secrets.sh agent ANTHROPIC_API_KEY
```

1. `update.sh` busca o kit, junta a versão nova com o histórico da empresa (git merge), instala as
   dependências, roda os testes, mostra o que mudou e, com `--push`, envia ao GitHub da empresa. Se
   o Railway estiver conectado a esse repositório, o deploy começa sozinho. Sem `--push`, revise e
   rode `git push origin HEAD` (ou `railway up --detach` se o Railway não estiver ligado ao GitHub).
2. `set_secrets.sh` com o nome das chaves pergunta só por elas (digitação oculta, direto no Railway)
   e reinicia o serviço. Faça isso só quando a versão trouxer variável obrigatória nova.

Conferir depois: `railway logs` deve mostrar `Agent de social media no ar` e a linha do Revisor; no
Telegram, `/status` e `/x`.

### Se der conflito

Conflito só acontece em arquivo do motor que a empresa também alterou. O script lista os arquivos.
Abra cada um, mantenha a mudança do kit e reaplique a sua por cima, depois:

```bash
git add <arquivos> && git commit
./scripts/update.sh --push
```

Para desistir: `git merge --abort`. Personalização de marca deve ficar em `brands/<slug>/`
(brand.toml, knowledge, assets): assim as próximas atualizações entram sem conflito.

## Sem git (pasta baixada como ZIP)

Cada release no GitHub traz o arquivo `.patch` da versão
(github.com/OSLOTEAM/social-media-agent-kit/releases). Na pasta da instalação:

```bash
git init -q 2>/dev/null; git apply --3way social-media-agent-kit-v1.1.0.patch
~/.venvs/social-media-agent/bin/pip install -r requirements.txt
./run.sh test
railway up --detach
```

## Versões e o que cada uma pede

Veja o [CHANGELOG.md](CHANGELOG.md): cada versão lista as variáveis novas e os passos depois de
atualizar. A versão instalada fica no arquivo `VERSION`.
