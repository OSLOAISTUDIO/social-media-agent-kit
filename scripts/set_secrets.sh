#!/bin/zsh
# Grava as chaves do agente nas variáveis do Railway. Uso: ./scripts/set_secrets.sh [serviço]
# (padrão: "agent"). Roda no projeto Railway linkado na pasta (railway link).
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
  APIFY_TOKEN
  BUFFER_API_KEY
  SUPABASE_URL
  SUPABASE_SERVICE_KEY
)

echo "Railway: $(railway whoami --json | python3 -c 'import sys,json; print(json.load(sys.stdin)["email"])')"
echo "Cole cada valor e tecle Enter (nada aparece enquanto digita). Enter vazio pula.\n"
changed=0
for key in $KEYS; do
  read -rs "value?$key: "
  echo
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
