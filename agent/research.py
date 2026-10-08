"""Pesquisa de temas: sinais objetivos de tração + pesquisa na web pelo modelo + ranking."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import quote_plus

import feedparser
import httpx

from . import apify, db, llm, prompts, references
from .config import brand
from .models import ResearchResult, TopicProposal

log = logging.getLogger(__name__)

R = brand["research"]
HN_QUERIES = R.get("hn_queries", [])
NEWS_FEEDS = {
    "Google News BR": "https://news.google.com/rss/search?q={q}+when:7d&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "Google News US": "https://news.google.com/rss/search?q={q}+when:7d&hl=en-US&gl=US&ceid=US:en",
}
NEWS_QUERIES = {
    "Google News BR": R.get("news_br", []),
    "Google News US": R.get("news_us", []),
}

# Temas separados por pesquisa: 4 a 5, para conter custo (web search + Apify).
# 1 post na semana → 4 temas (1 proposto + 3 reservas); 2 posts → 5 (2 + 3 reservas).
MIN_CANDIDATES, MAX_CANDIDATES = 4, 5


def candidates_for(slots: int) -> int:
    return max(MIN_CANDIDATES, min(MAX_CANDIDATES, slots + 3))


# Peso de cada critério no ranking final. Potencial viral (momentum) pesa mais; novidade vem em
# seguida: lançamentos e recursos novos com aplicação prática antes de dados sem novidade de produto.
WEIGHTS = {"momentum": 0.40, "novelty": 0.25, "relevance": 0.20, "ownability": 0.15}


async def _hacker_news(client: httpx.AsyncClient) -> list[str]:
    since = int(time.time()) - 7 * 86400
    seen: dict[str, dict] = {}
    for q in HN_QUERIES:
        try:
            r = await client.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": q, "tags": "story", "numericFilters": f"created_at_i>{since},points>80", "hitsPerPage": 20},
            )
            for h in r.json().get("hits", []):
                seen[h["objectID"]] = h
        except Exception as e:
            log.warning("HN %s: %s", q, e)
    top = sorted(seen.values(), key=lambda h: h.get("points", 0), reverse=True)[:20]
    return [f"[HN {h['points']} pts, {h.get('num_comments', 0)} coment.] {h['title']} {h.get('url') or ''}" for h in top]


async def _news(client: httpx.AsyncClient) -> list[str]:
    out = []
    for feed, queries in NEWS_QUERIES.items():
        for q in queries:
            try:
                r = await client.get(NEWS_FEEDS[feed].format(q=quote_plus(q)))
                entries = feedparser.parse(r.text).entries
                out.append(f"[{feed} · '{q}': {len(entries)} matérias em 7 dias]")
                out += [f"  - {e.title}" for e in entries[:8]]
            except Exception as e:
                log.warning("News %s: %s", q, e)
    return out


async def collect_signals() -> str:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "agent-socialmedia/1.0"}) as c:
        refs, hn, news, ap = await asyncio.gather(references.collect(), _hacker_news(c), _news(c), apify.collect())
    return "\n".join([*refs, *ap, "## Hacker News (7 dias, por pontos)", *hn, "", "## Notícias (7 dias)", *news])


PILLARS = [p["name"] for p in brand.get("pillars", [])]


def normalize_pillar(name: str) -> str:
    """Garante um pilar válido da marca (o modelo às vezes abrevia ou reescreve o nome)."""
    if not PILLARS or name in PILLARS:
        return name
    words = set(re.findall(r"\w{4,}", name.lower()))
    best = max(PILLARS, key=lambda p: len(words & set(re.findall(r"\w{4,}", p.lower()))))
    return best


def score(t: TopicProposal) -> float:
    s = t.scores
    return round(sum(getattr(s, k) * w for k, w in WEIGHTS.items()), 2)


async def research(n: int = MIN_CANDIDATES, direction: str | None = None, request: str | None = None) -> ResearchResult:
    """Devolve candidatos ordenados pelo score composto.

    direction: orientação do humano para a semana (refaz a busca com esse foco)
    request:   pedido avulso de post (o tema já vem dado; o agente só apura e estrutura)
    """
    signals = await collect_signals()
    if request:
        dir_text = (f"PEDIDO DO HUMANO: o post deve ser sobre: {request}. Gere {n} ângulos diferentes "
                    "para esse mesmo assunto, todos apurados com fontes.")
    elif direction:
        dir_text = f"DIRECIONAMENTO DO HUMANO PARA ESTA SEMANA (prioridade máxima): {direction}"
    else:
        dir_text = ""
    prompt = prompts.RESEARCH.format(
        signals=signals, history="\n".join(db.recent_titles()) or "(nenhum)", direction=dir_text, n=n,
    )
    res = await llm.structured(prompts.base_instructions(), prompt, ResearchResult, web=True)
    res.candidates = res.candidates[:n]  # nunca mais do que o pedido
    for cand in res.candidates:
        cand.pillar = normalize_pillar(cand.pillar)
    if not request:
        res.candidates.sort(key=score, reverse=True)
    return res
