"""Vigia do long polling do Telegram.

Se o bot parar de buscar mensagens (conexão presa, laço de eventos travado), o contêiner continua
"no ar" para o Railway, mas mudo. O vigia mede a última resposta do getUpdates e, passado o limite,
encerra o processo com erro: o Railway reinicia o serviço sozinho (política padrão ON_FAILURE).
"""
from __future__ import annotations

import logging
import os
import threading
import time

from telegram.request import HTTPXRequest

log = logging.getLogger(__name__)
LIMIT = int(os.getenv("POLL_STALL_SECONDS", "600"))
_last = time.monotonic()


class PollingRequest(HTTPXRequest):
    """Request usado só pelo getUpdates: marca a hora de cada resposta (inclusive 409/502, que vêm sem exceção)."""

    async def do_request(self, *args, **kwargs):
        out = await super().do_request(*args, **kwargs)
        global _last
        _last = time.monotonic()
        return out


def stalled_for() -> float:
    return time.monotonic() - _last


def _watch() -> None:
    while True:
        time.sleep(30)
        if stalled_for() > LIMIT:
            log.critical("Telegram sem resposta do getUpdates há %ds: encerrando para o Railway reiniciar.",
                         int(stalled_for()))
            os._exit(1)


def start() -> None:
    global _last
    _last = time.monotonic()
    threading.Thread(target=_watch, name="polling-watchdog", daemon=True).start()
