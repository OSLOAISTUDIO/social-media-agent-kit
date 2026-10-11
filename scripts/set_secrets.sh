#!/bin/zsh
# Grava as chaves do agente nas variáveis do Railway. Uso: ./scripts/set_secrets.sh [serviço] [CHAVE ...]
# (serviço padrão: "agent"). Roda no projeto Railway linkado na pasta (railway link).
# Com chaves no fim, pergunta só por elas (ex.: na atualização: ./scripts/set_secrets.sh agent ANTHROPIC_API_KEY).
# Cada valor é digitado aqui com a digitação oculta e vai direto para o Railway via stdin:
# não aparece na tela, no histórico do terminal nem em arquivo. Enter vazio pula a chave.
set -e
cd "$(dirname "$0")/.."
SERVICE="${1:-agent}"
export RAILWAY_CALLER="social-media-agent/set_secrets"

KEYS=(
  TELEGRAM_BOT_TOKEN
  TELEGRAM_ALLOWED_CHAT_IDS
  OPENAI_API_KEY
  ANTHROPIC_API_KEY
  APIFY_TOKEN
  BUFFER_API_KEY
  SUPABASE_URL
  SUPABASE_SERVICE_KEY
)
(( $# > 1 )) && KEYS=("${@:2}")

echo "Railway: $(railway whoami --json | python3 -c 'import sys,json; print(json.load(sys.stdin)["email"])')"
echo "Cole cada valor e tecle Enter (nada aparece enquanto digita). Enter vazio pula.\n"
# Limpa o que o terminal pode colar junto com a chave: sequências de escape (setas como ESC[A,
# marcadores de colagem ESC[200~ / ESC[201~), caracteres de controle e espaços nas pontas.
# Já aconteceu de entrar "ESC[A ESC[A" antes de um token do Telegram, quebrando a URL da API.
clean() {
  print -rn -- "$1" | python3 -c '
import re, sys
v = sys.stdin.read()
v = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", v)   # CSI: setas, colagem com colchetes
v = re.sub(r"\x1b[@-Z\\-_]", "", v)               # outros ESC de 2 bytes
v = re.sub(r"[\x00-\x1f\x7f]", "", v)             # controles restantes
sys.stdout.write(v.strip())'
}

changed=0
for key in $KEYS; do
  read -rs "value?$key: "
  echo
  value="$(clean "$value")"
  half=$(( ${#value} / 2 ))
  if (( ${#value} > 20 && ${#value} % 2 == 0 )) && [[ "${value:0:$half}" == "${value:$half}" ]]; then
    echo "  ! $key parece ter sido colada duas vezes (a mesma chave repetida). Confira antes de seguir."
  fi
  if [[ -n "$value" ]]; then
    print -rn -- "$value" | railway variable set "$key" --stdin --skip-deploys --service "$SERVICE" >/dev/null
    echo "  ✓ $key salva"
    changed=1
  else
    echo "  - $key pulada"
  fi
  unset value
done

if (( changed )); then
  railway redeploy --service "$SERVICE" -y >/dev/null
  echo "\nPronto. O Railway está reiniciando o agente com as novas chaves."
else
  echo "\nNada alterado."
fi
