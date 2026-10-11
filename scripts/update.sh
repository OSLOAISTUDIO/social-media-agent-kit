#!/bin/zsh
# Atualiza esta instalação com a versão mais nova do kit (patch do repositório público).
#
# Uso:  ./scripts/update.sh            última versão publicada (branch main do kit)
#       ./scripts/update.sh v1.1.0     uma versão específica (tag)
#       ./scripts/update.sh --push     atualiza e já envia ao GitHub da empresa (o Railway faz o deploy)
#
# O que faz: confere que não há alteração pendente, busca o kit, junta a versão nova (git merge, sem
# reescrever o histórico da empresa), instala dependências novas, roda os testes e mostra o que mudou
# e as variáveis novas a cadastrar. O pacote da marca (brands/<slug>/) nunca é tocado pelo kit.
set -e
cd "$(dirname "$0")/.."
KIT_URL="https://github.com/OSLOTEAM/social-media-agent-kit.git"
VENV="$HOME/.venvs/social-media-agent"
REF="main"; PUSH=0
for a in "$@"; do
  case "$a" in
    --push) PUSH=1 ;;
    *) REF="$a" ;;
  esac
done

[[ -d .git ]] || { echo "Esta pasta não é um repositório git. Veja UPDATE.md (caminho sem git)."; exit 1; }
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Há alterações não salvas. Faça commit (ou git stash) antes de atualizar."; git status --short; exit 1
fi
git remote get-url kit >/dev/null 2>&1 || git remote add kit "$KIT_URL"
OLD="$(cat VERSION 2>/dev/null || echo 1.0.0)"
echo "Versão atual: $OLD · buscando o kit..."
git fetch -q kit --tags
TARGET="kit/$REF"; git rev-parse -q --verify "refs/tags/$REF" >/dev/null && TARGET="$REF"
NEW="$(git show "$TARGET:VERSION" 2>/dev/null || echo '?')"
if git merge-base --is-ancestor "$TARGET" HEAD; then
  echo "Já está na versão $OLD (nada novo em $REF)."; exit 0
fi
echo "Aplicando a versão $NEW..."
if ! git merge --no-edit -m "Atualiza o kit para a versão $NEW" "$TARGET"; then
  echo "\nConflito ao juntar a versão nova (arquivos do motor que a empresa também alterou):"
  git diff --name-only --diff-filter=U | sed 's/^/  - /'
  echo "Resolva cada arquivo (mantenha as mudanças do kit e reaplique as suas), depois:"
  echo "  git add <arquivos> && git commit && ./scripts/update.sh"
  echo "Para desistir: git merge --abort"
  exit 1
fi

if [[ -x "$VENV/bin/pip" ]]; then
  echo "Instalando dependências novas..."
  "$VENV/bin/pip" install -q -r requirements.txt
  echo "Rodando os testes..."
  "$VENV/bin/python" -m tests.test_pipeline >/dev/null && "$VENV/bin/python" -m tests.test_agents >/dev/null \
    && echo "  ✓ testes ok" || { echo "  ✗ testes falharam: rode ./run.sh test para ver o detalhe"; exit 1; }
fi

echo "\n===== O que mudou ($OLD → $NEW) ====="
awk -v v="$NEW" '$0 ~ "^## "v {p=1; print; next} p && /^## / {exit} p' CHANGELOG.md
if (( PUSH )); then
  git push origin HEAD
  echo "\nEnviado ao GitHub da empresa: o Railway começa o deploy (se estiver conectado ao repositório)."
else
  echo "\nPróximo passo: git push origin HEAD  (ou railway up --detach, se o Railway não estiver ligado ao GitHub)"
fi
