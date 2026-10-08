"""Referências editoriais: conteúdo recente de criadores que a marca acompanha (brands/<slug>/reference_sources.json).

Entram na pesquisa como sinal do que está em alta e da pegada narrativa. YouTube pelo feed RSS
público do canal; Instagram pelo actor apify/instagram-post-scraper.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape

import httpx

from . import apify
from .config import BRAND_DIR, BRAND_NAME, cfg

log = logging.getLogger(__name__)
FILE = BRAND_DIR / "reference_sources.json"


def _conf() -> dict:
    return json.loads(FILE.read_text()) if FILE.exists() else {}


def _clip(text: str, n: int = 240) -> str:
    t = " ".join(unescape(text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


async def _youtube(client: httpx.AsyncClient, ch: dict, since: datetime) -> list[str]:
    try:
        r = await client.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": ch["channel_id"]})
        r.raise_for_status()
    except Exception as e:
        log.warning("YouTube %s: %s", ch["name"], e)
        return [f"[YouTube {ch['name']}] indisponível nesta rodada"]
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
        pub = re.search(r"<published>(.*?)</published>", entry)
        when = datetime.fromisoformat(pub.group(1)) if pub else None
        if when and when < since:
            continue
        title = re.search(r"<title>(.*?)</title>", entry, re.S)
        link = re.search(r'<link rel="alternate" href="(.*?)"', entry)
        views = re.search(r'<media:statistics views="(\d+)"', entry)
        desc = re.search(r"<media:description>(.*?)</media:description>", entry, re.S)
        # Descrições começam com chamadas do canal (links, cupons): fica só o texto sobre o vídeo.
        about = [l for l in unescape(desc.group(1) if desc else "").splitlines()
                 if "http" not in l and "@" not in l and len(l.strip()) > 25][:2]
        n_views = f"{int(views.group(1)):,}".replace(",", ".") if views else "0"
        out.append(f"[YouTube {ch['name']} · {when:%d/%m} · {n_views} views] "
                   f"{_clip(title.group(1) if title else '', 140)}"
                   f"{' | ' + _clip(' '.join(about), 200) if about else ''} {link.group(1) if link else ''}")
    return out


async def _instagram(client: httpx.AsyncClient, profiles: list[dict], days: int) -> list[str]:
    if not cfg.apify_token or not profiles:
        return []
    payload = {
        "username": [p["username"] for p in profiles],
        "resultsLimit": max(p.get("limit", 8) for p in profiles),
        "onlyPostsNewerThan": f"{days} days",
        "skipPinnedPosts": True,
    }
    try:
        async with httpx.AsyncClient(timeout=120, headers={"Authorization": f"Bearer {cfg.apify_token}"}) as ap:
            items = await apify.run_actor(ap, "apify/instagram-post-scraper", payload)
    except Exception as e:
        log.warning("Instagram (Apify): %s", e)
        return ["[Instagram] indisponível nesta rodada"]
    items.sort(key=lambda i: i.get("timestamp") or "", reverse=True)
    out = []
    for it in items[:20]:
        who = it.get("ownerUsername") or "?"
        when = (it.get("timestamp") or "")[:10]
        reach = f"{it.get('likesCount', 0)} curtidas, {it.get('commentsCount', 0)} coment."
        if it.get("videoViewCount"):
            reach += f", {it['videoViewCount']} views"
        out.append(f"[Instagram @{who} · {when} · {it.get('type', '')} · {reach}] {_clip(it.get('caption', ''), 260)} {it.get('url', '')}")
    return out


async def collect() -> list[str]:
    conf = _conf()
    if not conf:
        return []
    days = int(conf.get("days", 14))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with httpx.AsyncClient(timeout=300, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        yt = await asyncio.gather(*(_youtube(c, ch, since) for ch in conf.get("youtube", [])))
        ig = await _instagram(c, conf.get("instagram", []), days)
    lines = [l for block in yt for l in block] + ig
    if not lines:
        return []
    return [f"## Referências editoriais (criadores que a {BRAND_NAME} acompanha, últimos {days} dias)", *lines, ""]
