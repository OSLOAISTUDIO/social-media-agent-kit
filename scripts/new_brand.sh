#!/bin/zsh
# Cria o pacote de uma marca nova a partir de brands/_template.
# Uso: ./scripts/new_brand.sh <slug> "<Nome da marca>"     ex.: ./scripts/new_brand.sh acme "ACME"
set -e
cd "$(dirname "$0")/.."
SLUG="$1"; NOME="$2"
[[ -z "$SLUG" || -z "$NOME" ]] && { echo 'Uso: ./scripts/new_brand.sh <slug> "<Nome>"'; exit 1; }
[[ -e "brands/$SLUG" ]] && { echo "brands/$SLUG já existe"; exit 1; }
cp -R brands/_template "brands/$SLUG"
for f in "brands/$SLUG/brand.toml" "brands/$SLUG/knowledge/marca.md"; do
  python3 - "$f" "$SLUG" "$NOME" <<'PY'
import sys; p, slug, nome = sys.argv[1:]
s = open(p, encoding="utf-8").read().replace("__SLUG__", slug).replace("__NOME__", nome)
open(p, "w", encoding="utf-8").write(s)
PY
done
echo "Pacote criado em brands/$SLUG. Próximo passo: preencher e rodar"
echo "  python -m scripts.check_brand $SLUG"
