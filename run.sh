#!/bin/zsh
# Sobe o agente. Primeira vez: ./run.sh setup
set -e
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1
VENV="$HOME/.venvs/social-media-agent"
if [[ "$1" == "setup" ]]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r requirements.txt
  "$VENV/bin/python" -m playwright install chromium
  [[ -f .env ]] || cp .env.example .env
  echo "Pronto. Preencha o .env e rode ./run.sh"
  exit 0
fi
if [[ "$1" == "test" ]]; then
  exec "$VENV/bin/python" -m tests.test_pipeline
fi
exec "$VENV/bin/python" -m agent.main
