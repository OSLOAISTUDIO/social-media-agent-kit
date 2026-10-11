"""Frente X (Twitter): tweets de opinião forte, mais autônomos, em paralelo com Instagram/LinkedIn.

Modos (/x no Telegram, guardado no banco; valor inicial em X_MODE):
  off   desligada
  on    junto com Instagram/LinkedIn: cada post aprovado gera um tweet de opinião sobre o assunto, e o
        agente completa a cota semanal sozinho ao longo da semana
  only  só X: Instagram/LinkedIn pausados; a semana inteira vira tweets (ideal para founders)

Cota: X_TWEETS_PER_WEEK por semana, distribuída nos dias (todo dia às X_TIME o agente confere quantos
já deveriam ter saído e prepara o que falta). Todo tweet passa pelo Revisor e pela aprovação humana.
De onde vem o assunto: o post aprovado; temas da pesquisa da semana ainda sem tweet (sem custo
extra de pesquisa); ou, sem nenhum, uma busca rápida na web dentro do tema-macro.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from . import content, db, llm, prompts, reviewer
from .config import BRAND_NAME, MACRO_THEME, cfg

log = logging.getLogger(__name__)
TZ = ZoneInfo(cfg.timezone)

MODES = {"off": "Desligada", "on": "Ligada (junto com Instagram e LinkedIn)",
         "only": "Só X (Instagram e LinkedIn pausados)"}
LANGUAGES = ["direta", "irônica", "despojada", "séria", "provocadora", "didática"]
TONES = ["opinião forte", "polêmico", "neutro", "otimista", "cético"]
COUNTS = [1, 3, 5, 7, 10, 14]


def settings() -> dict:
    return {
        "mode": db.get_setting("x_mode", cfg.x_mode),
        "per_week": int(db.get_setting("x_tweets_per_week", str(cfg.x_tweets_per_week))),
        "language": db.get_setting("x_language", cfg.x_language),
        "tone": db.get_setting("x_tone", cfg.x_tone),
    }


def set_value(key: str, value) -> None:
    db.set_setting(f"x_{key}", str(value))


def mode() -> str:
    m = settings()["mode"]
    return m if m in MODES else "off"


def week_key(d: date | None = None) -> str:
    d = d or datetime.now(TZ).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def due_now(now: datetime | None = None) -> int:
    """Quantos tweets faltam para a cota proporcional até hoje (ex.: 5/semana na quarta = 3)."""
    now = now or datetime.now(TZ)
    per_week = settings()["per_week"]
    target = math.ceil(per_week * (now.weekday() + 1) / 7)
    return max(0, target - len(db.week_tweets(week_key(now.date()))))


# ---------------- escrita ----------------

class Tweet(BaseModel):
    text: str = Field(description="O tweet pronto para publicar")
    angle: str = Field(description="A opinião central, em uma frase")
    subject: str = Field(description="Assunto em até 6 palavras")
    source_name: str = Field(description="Veículo/fonte do fato comentado (vazio se for só opinião)")
    source_url: str = Field(description="URL da fonte do fato (vazio se for só opinião)")


TWEET = """Você é a voz da {brand} no X (Twitter). Escreva UM tweet de opinião sobre o assunto abaixo.

O que é um bom tweet aqui:
- Uma opinião forte e uma visão clara de quem entende de {theme}: toma posição, diz o que isso
  significa, o que vem depois ou o que todo mundo está entendendo errado. Nada de resumo de notícia.
- Linguagem: {language}. Tom: {tone}.
- Até {max_chars} caracteres (conte espaços). Frases curtas. Primeira linha que para o scroll.
- Sem link no texto, no máximo 1 hashtag (de preferência nenhuma), sem emoji em excesso.
- O fato comentado precisa ser verdadeiro e estar na fonte; a opinião é da marca.
- Pode ser ácido com ideias, nunca com pessoas: nada de ofensa, acusação sem prova ou difamação.
- Segue as regras de linguagem da marca (ex.: sem travessão, sem exclamação, se elas valerem).

Tweets recentes (não repita assunto nem estrutura):
{history}

{direction}
# Assunto
{seed}"""

SEED_WEB = """Busque na web um assunto que está gerando conversa AGORA (últimos dias) em {theme}, com
espaço para uma opinião forte e útil para o público da marca. Evite os assuntos dos tweets recentes.
Use uma fonte primária com data."""

FIX = """Reescreva o tweet abaixo corrigindo: {problems}
Mantenha a opinião e o assunto. Até {max_chars} caracteres.

Tweet: {text}"""


def _problems(text: str) -> list[str]:
    p = [v.replace("caption_instagram", "tweet") for v in
         content.language_violations({"caption_instagram": text, "caption_linkedin": "", "slides": []})]
    if len(text) > cfg.x_max_chars:
        p.append(f"- tweet com {len(text)} caracteres (máximo {cfg.x_max_chars})")
    if "http" in text:
        p.append("- tire o link do texto")
    return p


def seed_from_post(post_id: int) -> dict:
    p = db.post(post_id)
    t = db.topic(p["topic_id"])
    pl = {k: v for k, v in t["payload"].items() if not k.startswith("_")}
    return {"kind": "post", "post_id": post_id, "topic_id": p["topic_id"],
            "text": json.dumps({"tema": pl.get("title"), "por_que_agora": pl.get("why_now"),
                                "dados": pl.get("data_points"), "sinopse": pl.get("synopsis"),
                                "fontes": pl.get("sources"),
                                "capa_do_post": p["content"]["slides"][0]["title"]}, ensure_ascii=False)}


def seed_auto() -> dict:
    """Tema da pesquisa recente ainda sem tweet; sem nenhum, busca na web."""
    used = {json.loads(r["payload"] or "{}").get("seed", {}).get("topic_id")
            for r in db.rows("SELECT payload FROM tweets ORDER BY id DESC LIMIT 60")}
    since = (datetime.now(TZ) - timedelta(days=7)).isoformat(timespec="seconds")
    for r in db.rows("SELECT id, payload FROM topics WHERE created_at >= ? AND status IN ('backlog','proposed','approved',"
                     "'rejected') ORDER BY id DESC", since):
        if r["id"] in used:
            continue
        pl = {k: v for k, v in json.loads(r["payload"]).items() if not k.startswith("_")}
        return {"kind": "topic", "topic_id": r["id"],
                "text": json.dumps({"tema": pl.get("title"), "por_que_agora": pl.get("why_now"),
                                    "dados": pl.get("data_points"), "fontes": pl.get("sources")}, ensure_ascii=False)}
    return {"kind": "web", "text": SEED_WEB.format(theme=MACRO_THEME)}


def seed_request(brief: str) -> dict:
    return {"kind": "request", "text": f"Pedido do humano: {brief}\nApure o fato na web (fonte primária com data)."}


async def write(seed: dict, direction: str | None = None, avoid: str | None = None) -> int:
    """Escreve, confere (linguagem, tamanho, Revisor com 1 pedido de ajuste) e guarda o tweet.
    Devolve o id do tweet, pronto para ir ao Telegram."""
    st = settings()
    extra = []
    if direction:
        extra.append(f"Direcionamento do humano (obrigatório): {direction}")
    if avoid:
        extra.append(f"Escreva algo claramente diferente deste tweet anterior: {avoid}")
    prompt = TWEET.format(brand=BRAND_NAME, theme=MACRO_THEME, language=st["language"], tone=st["tone"],
                          max_chars=cfg.x_max_chars, history="\n".join(f"- {x}" for x in db.recent_tweets(12)) or "-",
                          direction="\n".join(extra), seed=seed["text"])
    web = seed["kind"] in ("web", "request")
    tw = await llm.structured(prompts.ROLE, prompt, Tweet, web=web)
    if probs := _problems(tw.text):
        tw = await llm.structured(prompts.ROLE, FIX.format(problems="; ".join(probs), max_chars=cfg.x_max_chars,
                                                           text=tw.text), Tweet)
    rv = await reviewer.review_text(
        "um tweet de opinião para o X", tw.text,
        f"Assunto: {seed['text'][:4000]}\nFonte: {tw.source_name} {tw.source_url}\nOpinião central: {tw.angle}",
        f"opinião forte e clara; linguagem {st['language']} e tom {st['tone']}; até {cfg.x_max_chars} caracteres; "
        "fato correto segundo a fonte; nada ofensivo ou difamatório; primeira linha que prende.")
    note = None
    if rv:
        note = {"verdict": rv.verdict, "issues": rv.issues[:3], "applied": False}
        if rv.verdict == "ajustar" and rv.change_request:
            tw = await llm.structured(prompts.ROLE, prompt + f"\n\n# Pedido do Revisor (obrigatório)\n{rv.change_request}",
                                      Tweet, web=web)
            if probs := _problems(tw.text):
                tw = await llm.structured(prompts.ROLE, FIX.format(problems="; ".join(probs),
                                                                   max_chars=cfg.x_max_chars, text=tw.text), Tweet)
            note["applied"] = True
    payload = {**tw.model_dump(), "seed": {k: v for k, v in seed.items() if k != "text"} | {"text": seed["text"][:6000]},
               "settings": st, "review": note, "direction": direction}
    return db.add_tweet(week_key(), tw.text.strip(), payload, seed.get("post_id"))
