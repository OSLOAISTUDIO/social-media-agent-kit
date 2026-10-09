"""Ponto de entrada: python -m agent.main"""
from __future__ import annotations

import logging
import sys
import time

from . import db
from .bot import build_app
from .config import BRAND_NAME, cfg


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(cfg.data_dir / "agent.log")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    missing = [m for m in cfg.missing() if m != "TELEGRAM_ALLOWED_CHAT_IDS"]
    if missing:
        # Em servidor, espera as variáveis em vez de cair em loop de restart.
        # No Railway, salvar as variáveis dispara um novo deploy automaticamente.
        logging.error("Configure as variáveis: %s. Aguardando.", ", ".join(missing))
        while True:
            time.sleep(3600)
    if cfg.brand_slug == "exemplo":
        sys.exit("BRAND=exemplo é só para testes e prévia visual (não tem tema de conteúdo). Crie a sua marca com "
                 "./scripts/new_brand.sh <slug> \"<Nome>\" (ele pergunta o tema-macro) e defina BRAND=<slug>.")
    if not cfg.knowledge_root.exists():
        sys.exit(f"Base de conhecimento não encontrada: {cfg.knowledge_root}")
    db.init()
    if cfg.can_publish():
        logging.warning("Publicação desativada até configurar: %s", ", ".join(cfg.can_publish()))
    if not cfg.apify_token:
        logging.warning("APIFY_TOKEN ausente: pesquisa usa só Hacker News, Google News e web search.")
    logging.info("Agent de social media no ar · marca %s (%s) · modelo %s", BRAND_NAME, cfg.brand_slug, cfg.model)
    build_app().run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
