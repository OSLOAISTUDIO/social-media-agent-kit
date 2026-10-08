"""Dispara agora a pergunta semanal no chat principal (a mesma de segunda, 9h).
Uso: railway run --service agent python -m scripts.kickoff
Os botões são tratados pelo bot que está no ar no Railway.
"""
import json
import urllib.request

from agent.config import BRAND_NAME, cfg

kb = {"inline_keyboard": [[{"text": "1 post", "callback_data": "n:1"}, {"text": "2 posts", "callback_data": "n:2"}],
                          [{"text": "Nenhum esta semana", "callback_data": "n:0"}]]}
body = {
    "chat_id": cfg.allowed_chat_ids[0],
    "text": f"Bom dia. Começando a semana de conteúdo da {BRAND_NAME}\n<b>Quantos posts faço esta semana?</b>",
    "parse_mode": "HTML",
    "reply_markup": kb,
}
req = urllib.request.Request(f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
                             data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
res = json.load(urllib.request.urlopen(req))
print("enviado" if res.get("ok") else res)
