#!/bin/zsh
# Cria o pacote de uma marca nova a partir de brands/_template e pergunta o TEMA-MACRO que o agente
# deve focar (o kit não traz nenhum tema pré-definido: toda a pesquisa parte dele).
#
# Uso interativo:  ./scripts/new_brand.sh <slug> "<Nome da marca>"
# Sem perguntas:   ./scripts/new_brand.sh <slug> "<Nome>" "<tema-macro>" ["<tema em inglês>"]
#   ex.: ./scripts/new_brand.sh acme "ACME" "nutrição esportiva" "sports nutrition"
set -e
cd "$(dirname "$0")/.."
SLUG="$1"; NOME="$2"; TEMA="$3"; TEMA_EN="$4"
[[ -z "$SLUG" || -z "$NOME" ]] && { echo 'Uso: ./scripts/new_brand.sh <slug> "<Nome>" ["<tema-macro>"] ["<tema em inglês>"]'; exit 1; }
[[ -e "brands/$SLUG" ]] && { echo "brands/$SLUG já existe"; exit 1; }

if [[ -z "$TEMA" ]]; then
  echo "Qual o tema-macro que o agente deve focar nas pesquisas e nos posts?"
  echo "(o assunto amplo do conteúdo da marca, ex.: \"nutrição esportiva\", \"mercado imobiliário\", \"educação infantil\")"
  read -r "TEMA?Tema-macro: "
  read -r "TEMA_EN?Mesmo tema em inglês, para fontes internacionais (Enter para pular): "
fi
[[ -z "$TEMA" ]] && { echo "O tema-macro é obrigatório."; exit 1; }

cp -R brands/_template "brands/$SLUG"
python3 - "brands/$SLUG" "$SLUG" "$NOME" "$TEMA" "$TEMA_EN" <<'PY'
import json, sys
from pathlib import Path
d, slug, nome, tema, tema_en = sys.argv[1:]
d = Path(d)

def toml_str(v: str) -> str:  # conteúdo de string TOML entre aspas
    return v.replace("\\", "\\\\").replace('"', '\\"')

en_list = f'["{toml_str(tema_en)}"]' if tema_en else "[]"
for f in (d / "brand.toml", d / "knowledge" / "marca.md"):
    s = f.read_text(encoding="utf-8")
    s = s.replace("__SLUG__", slug).replace("__NOME__", nome)
    s = s.replace('["__TEMA_EN__"]', en_list).replace("__TEMA__", toml_str(tema) if f.suffix == ".toml" else tema)
    f.write_text(s, encoding="utf-8")

def fill(x):  # troca o marcador nos termos das fontes do Apify (JSON)
    if isinstance(x, dict):
        return {k: fill(v) for k, v in x.items()}
    if isinstance(x, list):
        return [fill(v) for v in x]
    return x.replace("__TEMA__", tema) if isinstance(x, str) else x

ap = d / "apify_sources.json"
data = fill(json.loads(ap.read_text(encoding="utf-8")))
if tema_en:  # X também em inglês
    for src in data["sources"]:
        if src.get("format") == "x":
            src["input"]["searchTerms"].append(f'"{tema_en}" min_faves:300 -filter:replies')
ap.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

echo "Pacote criado em brands/$SLUG com o tema-macro: $TEMA"
echo "Próximo passo: preencher o resto do brand.toml e knowledge/marca.md e rodar"
echo "  python -m scripts.check_brand $SLUG"
