"""Sinais de tendência via Apify (Google Trends, Reddit, X e o que estiver em apify_sources.json)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta

import httpx

from .config import BRAND_DIR, cfg

log = logging.getLogger(__name__)
SOURCES_FILE = BRAND_DIR / "apify_sources.json"
APIFY = "https://api.apify.com/v2"
MAX_WAIT = 480  # segundos por actor; a coleta roda em paralelo com as outras fontes


async def run_actor(client: httpx.AsyncClient, actor: str, payload: dict, max_wait: int = MAX_WAIT) -> list[dict]:
    """Roda um actor de forma assíncrona e devolve os itens do dataset.

    A chamada síncrona do Apify corta em 300s e descarta tudo; aqui o run começa sem esperar, o
    status é consultado a cada 30s até `max_wait` e, se não terminar, os itens já coletados são
    aproveitados (o run é abortado para não seguir consumindo crédito).
    """
    r = await client.post(f"{APIFY}/acts/{actor.replace('/', '~')}/runs", json=payload)
    r.raise_for_status()
    run = r.json()["data"]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    while run["status"] in ("READY", "RUNNING") and loop.time() < deadline:
        try:
            r = await client.get(f"{APIFY}/actor-runs/{run['id']}", params={"waitForFinish": 30}, timeout=60)
            r.raise_for_status()
            run = r.json()["data"]
        except httpx.TransportError:  # conexão longa caiu ou estourou: espera e consulta de novo
            await asyncio.sleep(5)
    if run["status"] in ("READY", "RUNNING"):
        try:
            await client.post(f"{APIFY}/actor-runs/{run['id']}/abort")
        except httpx.TransportError:
            pass
    items: list[dict] = []
    for attempt in range(3):
        try:
            items = (await client.get(f"{APIFY}/datasets/{run['defaultDatasetId']}/items",
                                      params={"clean": "true", "format": "json"}, timeout=60)).json()
            break
        except httpx.TransportError:
            await asyncio.sleep(5 * (attempt + 1))
    if run["status"] != "SUCCEEDED":
        log.warning("Apify %s terminou como %s; usando %d itens parciais", actor, run["status"], len(items))
    return items


def _sources() -> list[dict]:
    if not SOURCES_FILE.exists():
        return []
    return [s for s in json.loads(SOURCES_FILE.read_text())["sources"] if s.get("enabled", True)]


# ---------- formatadores: item bruto do actor → linhas de sinal ----------

def _google_trends(items: list[dict]) -> list[str]:
    out = []
    for it in items:
        term = it.get("searchTerm") or it.get("inputUrlOrTerm")
        timeline = [p.get("value", [0])[0] for p in it.get("interestOverTime_timelineData", []) if p.get("value")]
        trend = ""
        if len(timeline) >= 4:
            half = len(timeline) // 2
            a, b = sum(timeline[:half]) / half or 1, sum(timeline[half:]) / (len(timeline) - half)
            trend = f", interesse {'+' if b >= a else ''}{round((b - a) / a * 100)}% na 2ª metade da semana"
        rising = [f"{q.get('query')} ({q.get('formattedValue', q.get('value'))})" for q in (it.get("relatedQueries_rising") or [])[:6]]
        out.append(f"[Google Trends BR] '{term}'{trend}. Buscas em alta: {', '.join(rising) or 'n/d'}")
    return out


def _reddit(items: list[dict]) -> list[str]:
    posts = [i for i in items if i.get("title")]
    posts.sort(key=lambda i: i.get("upVotes") or 0, reverse=True)
    return [f"[Reddit r/{str(p.get('communityName') or '?').removeprefix('r/')} {p.get('upVotes', 0)} votos, "
            f"{p.get('numberOfComments', 0)} coment.] {p['title']} {p.get('url', '')}" for p in posts[:15]]


def _x(items: list[dict]) -> list[str]:
    tw = [i for i in items if i.get("text")]
    tw.sort(key=lambda i: (i.get("likeCount") or 0) + 3 * (i.get("retweetCount") or 0), reverse=True)
    out = []
    for t in tw[:15]:
        who = (t.get("author") or {}).get("userName", "?")
        text = " ".join(t["text"].split())[:220]
        out.append(f"[X @{who} {t.get('likeCount', 0)} likes, {t.get('retweetCount', 0)} RTs] {text} {t.get('url', '')}")
    return out


def _generic(items: list[dict]) -> list[str]:
    out = []
    for it in items[:15]:
        title = it.get("title") or it.get("text") or it.get("name") or json.dumps(it, ensure_ascii=False)[:200]
        out.append(f"- {' '.join(str(title).split())[:220]} {it.get('url', '')}")
    return out


FORMATS = {"google_trends": _google_trends, "reddit": _reddit, "x": _x, "generic": _generic}


async def _run(client: httpx.AsyncClient, src: dict) -> list[str]:
    payload = dict(src["input"])
    if src.get("format") == "x":  # janela da semana
        payload.setdefault("start", (date.today() - timedelta(days=7)).isoformat())
    try:
        items = await run_actor(client, src["actor"], payload, src.get("max_wait", MAX_WAIT))
        lines = FORMATS.get(src.get("format", "generic"), _generic)(items)
        return [f"## {src['name']} (Apify, {len(items)} itens)", *lines]
    except Exception as e:
        log.warning("Apify %s falhou: %s", src["name"], e)
        return [f"## {src['name']}: indisponível nesta rodada"]


async def collect() -> list[str]:
    if not cfg.apify_token:
        return []
    async with httpx.AsyncClient(timeout=120, headers={"Authorization": f"Bearer {cfg.apify_token}"}) as c:
        blocks = await asyncio.gather(*(_run(c, s) for s in _sources()))
    return [line for block in blocks for line in block + [""]]
