"""Pesquisa de temas: sinais objetivos de tração + pesquisa na web pelo modelo + ranking."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import quote_plus

from html import unescape

import feedparser
import httpx

from . import apify, db, llm, prompts, references
from .config import brand, cfg
from .models import ResearchResult, TopicProposal

log = logging.getLogger(__name__)

R = brand["research"]
# Consultas de sinais: as do brand.toml; sem elas, o próprio tema-macro da marca (definido no setup)
THEME = R.get("short") or R["subject"]
HN_QUERIES = R.get("hn_queries") or [THEME]
# Janela dos sinais de notícia e Hacker News ([research] news_days; padrão 7 dias)
NEWS_DAYS = int(R.get("news_days", 7))
NEWS_FEEDS = {
    "Google News BR": "https://news.google.com/rss/search?q={q}+when:%dd&hl=pt-BR&gl=BR&ceid=BR:pt-419" % NEWS_DAYS,
    "Google News US": "https://news.google.com/rss/search?q={q}+when:%dd&hl=en-US&gl=US&ceid=US:en" % NEWS_DAYS,
}
NEWS_QUERIES = {
    "Google News BR": R.get("news_br") or [THEME],
    "Google News US": R.get("news_us", []),
}

# Temas separados por pesquisa: 4 a 5, para conter custo (web search + Apify).
# 1 post na semana → 4 temas (1 proposto + 3 reservas); 2 posts → 5 (2 + 3 reservas).
MIN_CANDIDATES, MAX_CANDIDATES = 4, 5


def candidates_for(slots: int) -> int:
    return max(MIN_CANDIDATES, min(MAX_CANDIDATES, slots + 3))


# Pesos do ranking: neutros por padrão; [research] weights no brand.toml sobrescreve os informados.
WEIGHTS = {"momentum": 0.40, "novelty": 0.25, "relevance": 0.20, "ownability": 0.15} | R.get("weights", {})


async def _hacker_news(client: httpx.AsyncClient) -> list[str]:
    since = int(time.time()) - NEWS_DAYS * 86400
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
                out.append(f"[{feed} · '{q}': {len(entries)} matérias em {NEWS_DAYS} dias]")
                out += [f"  - {e.title}" for e in entries[:8]]
            except Exception as e:
                log.warning("News %s: %s", q, e)
    return out


async def collect_signals() -> str:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "agent-socialmedia/1.0"}) as c:
        refs, hn, news, ap = await asyncio.gather(references.collect(), _hacker_news(c), _news(c), apify.collect())
    return "\n".join([*refs, *ap, f"## Hacker News ({NEWS_DAYS} dias, por pontos)", *hn, "",
                      f"## Notícias ({NEWS_DAYS} dias)", *news])


# ---------- links enviados pelo humano ----------

URL_RX = re.compile(r"https?://[^\s<>\"']+")
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")
# Telas de verificação anti-robô: nunca usar como conteúdo
BLOCK_HINTS = ("just a moment", "checking your browser", "verify you are human", "enable javascript",
               "attention required", "access denied", "are you a robot", "captcha")


def _urls(text: str) -> list[str]:
    urls = [u.rstrip(".,;:!?)]}>»\"'") for u in URL_RX.findall(text or "")]
    return list(dict.fromkeys(u for u in urls if u))[:3]


YT_RX = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")
YT_LANGS = ["pt", "pt-BR", "en", "es"]


def _yt_transcript(video_id: str) -> str | None:
    """Transcrição direta (legendas do vídeo). O YouTube às vezes bloqueia servidores em nuvem."""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    try:
        t = api.fetch(video_id, languages=YT_LANGS)
    except Exception:
        try:  # qualquer idioma disponível
            t = next(iter(api.list(video_id))).fetch()
        except Exception:
            return None
    text = " ".join(s.text for s in t.snippets if s.text.strip())
    return text or None


async def _yt_transcript_apify(url: str) -> str | None:
    """Alternativa pelo Apify quando a leitura direta é bloqueada (precisa de APIFY_TOKEN)."""
    if not cfg.apify_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=120, headers={"Authorization": f"Bearer {cfg.apify_token}"}) as ap:
            items = await apify.run_actor(ap, "pintostudio/youtube-transcript-scraper",
                                          {"videoUrl": url, "targetLanguage": "pt"}, max_wait=180)
    except Exception as e:
        log.warning("transcrição pelo Apify falhou: %s", e)
        return None
    for it in items:
        if isinstance(it.get("data"), list):
            text = " ".join(str(seg.get("text", "")) for seg in it["data"]).strip()
        else:
            text = str(it.get("transcript") or it.get("output") or "").strip()
        if text:
            return text
    return None


async def _youtube(client: httpx.AsyncClient, url: str, video_id: str, limit: int) -> tuple[str, bool]:
    """Título, canal e transcrição de um vídeo do YouTube. Devolve (texto, conseguiu_ler)."""
    title, author = "", ""
    try:
        o = await client.get("https://www.youtube.com/oembed",
                             params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"})
        if o.status_code == 200:
            title, author = o.json().get("title", ""), o.json().get("author_name", "")
    except Exception:
        pass
    head = f"[{url}] Vídeo do YouTube: {title or video_id}" + (f" · canal {author}" if author else "")
    text = await asyncio.to_thread(_yt_transcript, video_id) or await _yt_transcript_apify(url)
    if not text:
        return (f"{head}. A transcrição não está acessível: abra este vídeo pela busca na web "
                "(título, descrição, matérias sobre ele) e não invente o conteúdo."), False
    return f"{head}\nTranscrição:\n{text[:limit]}", True


def _html_to_text(raw: str) -> str:
    t = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", raw)
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return " ".join(unescape(t).split())


async def read_links(text: str, max_chars: int = 12000) -> tuple[str, int]:
    """Lê até 3 links do texto (páginas e vídeos do YouTube) e devolve (conteúdo, links não lidos).

    Página bloqueada (status >= 400, tela anti-robô ou texto curto demais) ou vídeo sem transcrição
    não entra como conteúdo: vira uma nota pedindo para o modelo abrir o link pela busca na web."""
    urls = _urls(text)
    if not urls:
        return "", 0
    per = max_chars // len(urls)
    fallback = "abra este link pela busca na web e use o conteúdo real da página (não use telas de bloqueio)."
    parts, unread = [], 0
    async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                 headers={"User-Agent": BROWSER_UA, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"}) as c:
        for url in urls:
            if m := YT_RX.search(url):
                part, ok = await _youtube(c, url, m.group(1), per)
                parts.append(part)
                unread += not ok
                continue
            try:
                r = await c.get(url)
            except Exception as e:
                parts.append(f"[{url}] não foi possível abrir ({e.__class__.__name__}): {fallback}")
                unread += 1
                continue
            ctype = r.headers.get("content-type", "").lower()
            if r.status_code >= 400:
                parts.append(f"[{url}] a página respondeu HTTP {r.status_code}: {fallback}")
                unread += 1
                continue
            if "html" in ctype:
                body = _html_to_text(r.text)
            elif ctype.startswith("text/"):
                body = " ".join(r.text.split())
            else:
                parts.append(f"[{url}] conteúdo do tipo {ctype or 'desconhecido'}: {fallback}")
                unread += 1
                continue
            blocked = len(body) < 400 or (len(body) < 2000 and any(h in body.lower() for h in BLOCK_HINTS))
            if blocked:
                parts.append(f"[{url}] a página não pôde ser lida (provável bloqueio anti-robô): {fallback}")
                unread += 1
                continue
            parts.append(f"[{url}]\n{body[:per]}")
    return "\n\n".join(parts), unread


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


REF_TEXT_MIN = 280  # pedido com link, vídeo ou texto longo colado = referência enviada pelo humano


def is_reference(request: str | None) -> bool:
    return bool(request) and (bool(_urls(request)) or len(request) >= REF_TEXT_MIN)


async def research(n: int = MIN_CANDIDATES, direction: str | None = None,
                   request: str | None = None) -> tuple[ResearchResult, str | None]:
    """Devolve (candidatos, referências). Candidatos ordenados pelo score composto.

    direction: orientação do humano para a semana (refaz a busca com esse foco)
    request:   pedido avulso de post. Se trouxer link, vídeo ou texto de referência, o agente usa
               SOMENTE essas referências: não coleta sinais nem busca outras fontes (a busca na web
               só é liberada para abrir links que não puderam ser lidos direto).
    """
    if is_reference(request):
        links, unread = await read_links(request)
        references = links + ("\n\nTEXTO ENVIADO PELO HUMANO:\n" + request if len(request) >= REF_TEXT_MIN else "")
        web_note = ("Alguns links não puderam ser lidos direto: use a busca na web SOMENTE para abrir esses "
                    "links, nunca para buscar outras fontes." if unread else "Não use busca na web.")
        prompt = prompts.RESEARCH_REFS.format(web_note=web_note, request=request,
                                              references=references.strip() or "(apenas o pedido acima)", n=n)
        res = await llm.structured(prompts.base_instructions(), prompt, ResearchResult, web=bool(unread))
        res.candidates = res.candidates[:n]
        for cand in res.candidates:
            cand.pillar = normalize_pillar(cand.pillar)
        return res, references.strip() or request

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
    return res, None
