"""Confere a conexão com cada serviço sem imprimir nenhuma chave.
Uso (com as variáveis do Railway): railway run python -m scripts.check_setup [--fix]
--fix cria o bucket público do Supabase se ele não existir.
"""
import asyncio
import sys

import httpx

from agent import publisher
from agent.config import cfg


async def main():
    results = []

    def ok(name, good, detail=""):
        results.append(f"{'✓' if good else '✗'} {name}{': ' + detail if detail else ''}")

    async with httpx.AsyncClient(timeout=30) as c:
        # Telegram
        r = await c.get(f"https://api.telegram.org/bot{cfg.telegram_token}/getMe")
        ok("Telegram", r.status_code == 200, "@" + r.json().get("result", {}).get("username", "?") if r.status_code == 200 else str(r.status_code))
        ok("Chat autorizado", bool(cfg.allowed_chat_ids), f"{len(cfg.allowed_chat_ids)} chat(s)")

        # OpenAI: modelos configurados existem na conta?
        for m in {cfg.model, cfg.model_fast, cfg.illustration_model}:
            r = await c.get(f"https://api.openai.com/v1/models/{m}", headers={"Authorization": f"Bearer {cfg.openai_api_key}"})
            ok(f"OpenAI {m}", r.status_code == 200, "" if r.status_code == 200 else f"HTTP {r.status_code}")

        # Anthropic (Revisor e Agent Vídeo): modelos configurados existem na conta?
        if cfg.anthropic_api_key:
            for m in {cfg.reviewer_model, cfg.video_model}:
                r = await c.get(f"https://api.anthropic.com/v1/models/{m}",
                                headers={"x-api-key": cfg.anthropic_api_key, "anthropic-version": "2023-06-01"})
                ok(f"Claude {m}", r.status_code == 200, "" if r.status_code == 200 else f"HTTP {r.status_code}")
        else:
            ok("Claude (Revisor e Agent Vídeo)", False, "ANTHROPIC_API_KEY ausente: os dois agentes ficam desligados")

        # Apify
        r = await c.get("https://api.apify.com/v2/users/me", headers={"Authorization": f"Bearer {cfg.apify_token}"})
        ok("Apify", r.status_code == 200, r.json().get("data", {}).get("plan", {}).get("id", "") if r.status_code == 200 else str(r.status_code))

        # Supabase: cria o bucket público se não existir (--fix), sobe um arquivo de teste, lê e apaga
        sb = {"Authorization": f"Bearer {cfg.supabase_key}", "apikey": cfg.supabase_key}
        if "--fix" in sys.argv:
            r = await c.get(f"{cfg.supabase_url}/storage/v1/bucket/{cfg.supabase_bucket}", headers=sb)
            if r.status_code != 200:
                r = await c.post(f"{cfg.supabase_url}/storage/v1/bucket", headers=sb,
                                 json={"id": cfg.supabase_bucket, "name": cfg.supabase_bucket, "public": True})
                ok(f"Supabase bucket '{cfg.supabase_bucket}' criado", r.status_code < 300, "" if r.status_code < 300 else r.text[:160])
            elif not r.json().get("public"):
                r = await c.put(f"{cfg.supabase_url}/storage/v1/bucket/{cfg.supabase_bucket}", headers=sb,
                                json={"id": cfg.supabase_bucket, "name": cfg.supabase_bucket, "public": True})
                ok(f"Supabase bucket '{cfg.supabase_bucket}' tornado público", r.status_code < 300, "" if r.status_code < 300 else r.text[:160])
        key = "_check/ok.txt"
        up = await c.post(f"{cfg.supabase_url}/storage/v1/object/{cfg.supabase_bucket}/{key}", content=b"ok",
                          headers={"Authorization": f"Bearer {cfg.supabase_key}", "Content-Type": "text/plain", "x-upsert": "true"})
        pub = await c.get(f"{cfg.supabase_url}/storage/v1/object/public/{cfg.supabase_bucket}/{key}") if up.status_code < 300 else None
        ok("Supabase upload", up.status_code < 300, "" if up.status_code < 300 else f"HTTP {up.status_code} {up.text[:120]}")
        ok("Supabase bucket público", bool(pub and pub.status_code == 200), "" if pub and pub.status_code == 200 else "a URL pública não abre: marque o bucket como Public")
        await c.request("DELETE", f"{cfg.supabase_url}/storage/v1/object/{cfg.supabase_bucket}/{key}",
                        headers={"Authorization": f"Bearer {cfg.supabase_key}"})

    # Buffer: canais
    async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {cfg.buffer_api_key}"}) as bf:
        try:
            ch = await publisher.buffer_channels(bf)
            for n in cfg.networks:
                ok(f"Buffer {n}", n in ch, "conectado" if n in ch else "canal não encontrado no Buffer")
            from agent import db, xarm
            db.init()
            if xarm.mode() != "off":
                ok("Buffer X (twitter)", "twitter" in ch, "conectado" if "twitter" in ch else
                   "frente X ligada, mas o canal do X não está conectado no Buffer")
        except Exception as e:
            ok("Buffer", False, str(e)[:160])

    print("\n".join(results))


asyncio.run(main())
