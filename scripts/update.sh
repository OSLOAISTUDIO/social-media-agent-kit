#!/bin/zsh
# Atualiza esta instalação com a versão mais nova do kit (patch do repositório público).
#
# Uso:  ./scripts/update.sh            última versão publicada (branch main do kit)
#       Primeira vez (instalação sem este script), na pasta da instalação:
#       zsh <(curl -fsSL https://raw.githubusercontent.com/OSLOAISTUDIO/social-media-agent-kit/main/scripts/update.sh)
#       ./scripts/update.sh v1.1.0     uma versão específica (tag)
#       ./scripts/update.sh --push     atualiza e já envia ao GitHub da empresa (o Railway faz o deploy)
#
# O que faz: confere que não há alteração pendente, busca o kit, junta a versão nova (git merge, sem
# reescrever o histórico da empresa), instala dependências novas, roda os testes e mostra o que mudou
# e as variáveis novas a cadastrar. O pacote da marca (brands/<slug>/) nunca é tocado pelo kit.
set -e
# Roda na raiz do repositório da instalação (também quando chamado pelo curl, sem arquivo local)
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
KIT_URL="https://github.com/OSLOAISTUDIO/social-media-agent-kit.git"
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

TESTS_OK=1
if [[ -x "$VENV/bin/pip" ]]; then
  echo "Instalando dependências novas..."
  "$VENV/bin/pip" install -q -r requirements.txt
  echo "Rodando os testes..."
  if "$VENV/bin/python" -m tests.test_agents >/dev/null 2>&1; then
    echo "  ✓ testes dos agentes ok"
  else
    TESTS_OK=0; echo "  ✗ testes dos agentes falharam: rode ./run.sh test para ver o detalhe"
  fi
  "$VENV/bin/python" -m tests.test_pipeline >/dev/null 2>&1 && echo "  ✓ teste do pipeline ok" || \
    echo "  ! teste do pipeline falhou (pode ser teste antigo, específico da marca): confira com ./run.sh test"
fi

echo "\n===== O que mudou ($OLD → $NEW) ====="
awk -v v="$NEW" '$0 ~ "^## "v {p=1; print; next} p && /^## / {exit} p' CHANGELOG.md
if (( PUSH && ! TESTS_OK )); then
  echo "\nNão enviei ao GitHub porque os testes dos agentes falharam. Corrija e rode: git push origin HEAD"
  exit 1
elif (( PUSH )); then
  git push origin HEAD
  echo "\nEnviado ao GitHub da empresa: o Railway começa o deploy (se estiver conectado ao repositório)."
else
  echo "\nPróximo passo: git push origin HEAD  (ou railway up --detach, se o Railway não estiver ligado ao GitHub)"
fi
