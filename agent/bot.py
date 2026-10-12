"""Bot do Telegram: conversa com o humano e orquestra pesquisa → aprovação → criação → aprovação → publicação.

Callbacks (botões):
  n:<qtd>              quantos posts na semana (em seguida pergunta se há tema indicado)
  tm:<week>:y|n        indicar um tema (texto livre: geral ou título + link) / pode pesquisar
  ap:<topic>           aprovar tema          rj:<topic>   outra ideia        dr:<topic>  direcionar
  ok:<post>            aprovar post          adj:<post>   pedir ajustes
  pub:<post>:now|slot  publicar agora / no próximo horário sugerido
  when:<post>          escolher dia e horário (texto livre)   at:<post>:<epoch>  confirmar agendamento
  cp:<post>            outra legenda         cd:<post>    direcionar a legenda
  tw:<tweet>:ok|new|dir|no   aprovar / outro tweet / direcionar / descartar
  twp:<tweet>:now|slot postar o tweet agora / no próximo horário do X     tww:<tweet>  escolher horário
  twat:<tweet>:<epoch> confirmar agendamento do tweet              twr:<tweet>  tentar de novo
  xs:<campo>:<valor>   configurações da frente X (/x)
"""
from __future__ import annotations

import asyncio
import html
import logging
import traceback
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import ForceReply, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import claude, content, costs, db, llm, prompts, publisher, research, reviewer, video, watchdog, xarm
from .config import BRAND_NAME, brand, cfg
from .models import Intent, ScheduleParse

log = logging.getLogger(__name__)
TZ = ZoneInfo(cfg.timezone)
e = html.escape

# Assunto da marca nas mensagens do bot ([research] short; sem ele, o subject)
SUBJECT = brand["research"].get("short") or brand["research"]["subject"]

HELP = (
    f"<b>Agent de social media · {e(BRAND_NAME)}</b>\n\n"
    f"Toda segunda eu pergunto quantos posts fazer e se você quer indicar um tema (geral ou um conteúdo "
    f"específico, com título e link). Sem tema, pesquiso o que está em alta em {e(SUBJECT)} "
    "e trago as opções para sua aprovação. Depois do OK, crio carrossel, stories e legendas, "
    "mando para aprovação e publico via Buffer.\n\n"
    "/semana  começa (ou recomeça) o ciclo da semana\n"
    "/novopost &lt;assunto&gt;  pede um post avulso\n"
    "/pesquisar &lt;foco&gt;  refaz a pesquisa da semana com um direcionamento\n"
    "/status  situação da semana e custo\n"
    "/pendentes  reenvia posts esperando aprovação\n"
    "/verificar  confere se o último post saiu em todas as redes\n"
    "/x  frente X (Twitter): ligar, desligar, só X, tweets por semana, linguagem e tom\n"
    "/tweet &lt;assunto&gt;  pede um tweet de opinião\n\n"
    "Antes de chegar a você, cada etapa passa pelo Revisor (Claude), e posts que ganham com movimento "
    "recebem um slide animado do Agent Vídeo.\n\n"
    "Também pode escrever livremente (\"faz um post sobre X\") ou mandar um link, vídeo do YouTube ou texto "
    "de referência: nesse caso eu uso só o que você mandou."
)


# ---------------- utilidades ----------------

def week_start(d: date | None = None) -> str:
    d = d or datetime.now(TZ).date()
    return (d - timedelta(days=d.weekday())).isoformat()


DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def fmt(dt: datetime) -> str:
    return f"{DIAS[dt.weekday()]}, {dt:%d/%m às %H:%M}"


def allowed(update: Update) -> bool:
    if not update.effective_chat:
        return False
    cid = update.effective_chat.id
    return cid in cfg.allowed_chat_ids or cid in db.aliases_of(cfg.allowed_chat_ids)


async def on_migrate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """O Telegram converteu um grupo autorizado em supergrupo (novo ID): o agente passa a usar o novo."""
    msg = update.message
    old, new = msg.migrate_from_chat_id, update.effective_chat.id
    if old in cfg.allowed_chat_ids:
        db.add_alias(old, new)
        log.info("grupo %s virou supergrupo %s", old, new)
        await say(ctx, new, f"O grupo virou supergrupo e ganhou um novo ID ({new}). Já estou atendendo aqui. "
                            "Quando puder, troque o ID antigo por este em TELEGRAM_ALLOWED_CHAT_IDS no Railway.")


async def retry(call, attempts: int = 3):
    """Telegram às vezes demora a responder depois de uploads grandes: tenta de novo em timeout/rede."""
    for i in range(attempts):
        try:
            return await call()
        except RetryAfter as ex:
            await asyncio.sleep(float(getattr(ex, "retry_after", 5)) + 1)
        except (TimedOut, NetworkError):
            if i == attempts - 1:
                raise
            await asyncio.sleep(3 * (i + 1))
    return await call()


async def say(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, kb: Kb | None = None):
    return await retry(lambda: ctx.bot.send_message(chat_id, text[:4090], parse_mode=ParseMode.HTML, reply_markup=kb,
                                                    disable_web_page_preview=True))


async def ask(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, placeholder: str):
    """Pergunta que espera texto livre. Em grupo (chat_id negativo) abre o campo de resposta ao bot,
    que é o que o Telegram entrega ao bot com o modo de privacidade ligado."""
    markup = ForceReply(input_field_placeholder=placeholder) if chat_id < 0 else None
    if chat_id < 0:
        text += "\n<i>Responda a esta mensagem.</i>"
    return await retry(lambda: ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup))


def background(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, coro, label: str):
    """Roda tarefas longas (pesquisa, criação, publicação) sem travar o bot e reporta erros no chat."""
    async def runner():
        try:
            await coro
        except Exception as ex:
            log.error("%s falhou:\n%s", label, traceback.format_exc())
            await say(ctx, chat_id, f"Não consegui concluir: {e(label)}.\n<code>{e(str(ex))[:600]}</code>\n"
                                    "Pode tentar de novo pelo mesmo botão ou comando.")
    ctx.application.create_task(runner())


def topic_text(t: dict, label: str) -> str:
    p = t["payload"]
    s = p["scores"]
    total = round(sum(s.get(k, 0) * w for k, w in research.WEIGHTS.items()), 2)
    uses = p.get("practical_uses") or []
    lines = [
        f"<b>{e(label)} · {e(p['title'])}</b>",
        f"<i>{e(p['headline'])}</i>",
        f"Tipo: {e(p.get('angle', '—'))}",
        "",
        "<b>Por que agora</b>", e(p["why_now"]),
        "",
        "<b>Dados</b>",
        *[f"• {e(d['fact'])} (<a href=\"{e(d['source_url'])}\">fonte</a>)" for d in p["data_points"]],
        "",
        "<b>O que o post vai trabalhar</b>", e(p["synopsis"]),
        *(["", "<b>Aplicações práticas</b>", *[f"• {e(u)}" for u in uses]] if uses else []),
        *(["", "<b>Pegada narrativa</b>", e(p["narrative"])] if p.get("narrative") else []),
        "",
        "<b>Roteiro</b>",
        *[f"{i}. {e(x)}" for i, x in enumerate(p["outline"], 1)],
        "",
        f"<b>Pilar {e(BRAND_NAME)}</b> {e(p['pillar'])}",
        f"<b>Score</b> {total}/10 · viral {s['momentum']} · novidade {s.get('novelty', '—')} · "
        f"relevância {s['relevance']} · autoridade {s['ownability']}",
        *([e(reviewer.topic_line(p["_review_topic"]))] if p.get("_review_topic") else []),
        "",
        "<b>Fontes para conferir</b>",
        *[f"• <a href=\"{e(src['url'])}\">{e(src['name'])}</a>{' · ' + e(src['date']) if src.get('date') else ''}"
          for src in p["sources"]],
    ]
    return "\n".join(lines)


def topic_kb(topic_id: int) -> Kb:
    return Kb([[Btn("✅ Aprovar tema", callback_data=f"ap:{topic_id}")],
               [Btn("🔄 Outra ideia", callback_data=f"rj:{topic_id}"),
                Btn("🧭 Direcionar", callback_data=f"dr:{topic_id}")]])


def post_kb(post_id: int) -> Kb:
    return Kb([[Btn("✅ Aprovar post", callback_data=f"ok:{post_id}"),
                Btn("✏️ Pedir ajustes", callback_data=f"adj:{post_id}")],
               [Btn("📝 Outra legenda", callback_data=f"cp:{post_id}"),
                Btn("🧭 Direcionar legenda", callback_data=f"cd:{post_id}")]])


def when_kb(post_id: int) -> Kb:
    return Kb([[Btn("🚀 Pode postar agora", callback_data=f"pub:{post_id}:now")],
               [Btn(f"🗓 {fmt(publisher.next_slot())}", callback_data=f"pub:{post_id}:slot")],
               [Btn("📅 Escolher dia e horário", callback_data=f"when:{post_id}")]])


# ---------------- ciclo semanal ----------------

async def ask_week(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int | None = None):
    chat_id = chat_id or db.resolve_chat(cfg.allowed_chat_ids[0])
    xs = xarm.settings()
    if xarm.mode() == "only":  # só X: a semana é de tweets, sem pergunta de posts
        await say(ctx, chat_id, f"Bom dia. Semana da {e(BRAND_NAME)} no X: vou preparar {xs['per_week']} tweets de opinião "
                                f"ao longo da semana (linguagem {e(xs['language'])}, tom {e(xs['tone'])}) e mando cada um "
                                "para você aprovar. Mude em /x. Para um tweet agora: /tweet &lt;assunto&gt;.")
        return
    w = db.get_or_create_week(week_start())
    db.update_week(w["id"], status="asking")
    kb = Kb([[Btn("1 post", callback_data="n:1"), Btn("2 posts", callback_data="n:2")],
             [Btn("Nenhum esta semana", callback_data="n:0")]])
    # Ciclo semanal vai só para o chat principal (o primeiro de TELEGRAM_ALLOWED_CHAT_IDS, ex.: o grupo).
    xline = (f"\nFrente X ligada: {xs['per_week']} tweets na semana, em paralelo (mude em /x)."
             if xarm.mode() == "on" else "")
    await say(ctx, chat_id, f"Bom dia. Começando a semana de conteúdo da {e(BRAND_NAME)}{xline}\n"
                            "<b>Quantos posts faço esta semana?</b>", kb)


async def weekly_job(ctx: ContextTypes.DEFAULT_TYPE):
    await ask_week(ctx)


async def run_research(ctx, chat_id: int, *, week_id: int | None, slots: int, direction: str | None = None,
                       request: str | None = None, label_prefix: str = "Tema"):
    """Pesquisa, guarda candidatos (os primeiros como propostos, o resto como reserva) e envia ao humano."""
    origin = "request" if request else "weekly"
    if not request:
        await say(ctx, chat_id, f"Pesquisando {e(SUBJECT)}. Leva alguns minutos.")
    elif research.is_reference(request):
        await say(ctx, chat_id, "Lendo as referências enviadas. Vou usar só elas, sem buscar outras fontes.")
    else:
        await say(ctx, chat_id, f"Apurando o assunto: <i>{e(request[:300])}</i>.")
    meter = costs.Meter()
    costs.current.set(meter)  # vale só para esta tarefa
    brief = request or direction
    # Revisor, etapa 0: afina o pedido antes de pesquisar (links e textos de referência seguem como vieram)
    if direction:
        direction = await reviewer.optimize_brief(direction, "direcionamento da pesquisa da semana")
    if request and not research.is_reference(request):
        request = await reviewer.optimize_brief(request, "pedido de post")
    if (request or direction) != brief:
        await say(ctx, chat_id, f"{e(reviewer.label())} afinou o pedido: <i>{e((request or direction)[:400])}</i>")
    res, refs = await research.research(n=research.candidates_for(slots), direction=direction, request=request)
    # Revisor, etapa 1: dentro do tema-macro? o que a pesquisa diz está nas fontes? (1 ajuste por tema)
    res, notes = await reviewer.screen_topics(res, refs)
    batch = f"{origin}-{uuid.uuid4().hex[:8]}"
    db.add_cost("research", batch, meter.as_dict())
    ids = []
    for i, cand in enumerate(res.candidates):
        status = "proposed" if i < slots else "backlog"
        payload = cand.model_dump()
        if refs:  # referências enviadas: seguem com o tema até a escrita e a revisão (só elas valem)
            payload["_refs"] = refs[:12000]
        if notes[i]:
            payload["_review_topic"] = notes[i]
        ids.append(db.add_topic(week_id, payload, status, origin, batch, brief))
    if week_id:
        db.update_week(week_id, status="proposing")
    if claude.notice():
        await say(ctx, chat_id, e(claude.notice().replace("Revisor e Agent Vídeo não rodaram",
                                                          "o Revisor não conferiu os temas")))
    await say(ctx, chat_id, f"<b>Leitura da semana</b>\n{e(res.landscape)}")
    for i, tid in enumerate(ids[:slots], 1):
        label = f"{label_prefix} {i} de {slots}" if slots > 1 else label_prefix
        await say(ctx, chat_id, topic_text(db.topic(tid), label), topic_kb(tid))


async def research_with_theme(ctx, chat_id: int, week_id: int, text: str):
    """Tema indicado na pergunta da semana.
    Sem link: vira o direcionamento da semana. Com link: o primeiro post sai em cima do conteúdo
    ("Seu tema"); se a semana tiver mais posts, os demais seguem a pesquisa normal."""
    w = db.row("SELECT * FROM weeks WHERE id = ?", week_id)
    n = max(w["n_posts"] or 1, 1)
    if not research.is_reference(text):
        db.update_week(week_id, direction=text)
        await run_research(ctx, chat_id, week_id=week_id, slots=n, direction=text)
        return
    await run_research(ctx, chat_id, week_id=week_id, slots=1, request=text, label_prefix="Seu tema")
    if n > 1:
        await run_research(ctx, chat_id, week_id=week_id, slots=n - 1, direction=w["direction"])


async def propose_replacement(ctx, chat_id: int, old: dict):
    nxt = db.next_backlog(old["batch"]) if old.get("batch") else None
    if nxt:
        db.set_topic_status(nxt["id"], "proposed")
        await say(ctx, chat_id, topic_text(nxt, "Nova ideia"), topic_kb(nxt["id"]))
        return
    # Reserva acabou: nova pesquisa mantendo o mesmo foco.
    await run_research(ctx, chat_id, week_id=old["week_id"], slots=1,
                       direction=old["brief"] if old["origin"] == "weekly" else None,
                       request=old["brief"] if old["origin"] == "request" else None, label_prefix="Nova ideia")


# ---------------- criação e revisão ----------------

async def send_preview(ctx, chat_id: int, post_id: int):
    p = db.post(post_id)
    c = p["content"]
    feed, story = c["_render"]["feed"], c["_render"]["story"]
    t = db.topic(p["topic_id"])
    failed: list[str] = []

    async def part(label: str, call):
        # Uma parte que falha (ex.: timeout do Telegram) não pode impedir os botões de chegarem.
        try:
            await call()
        except Exception as ex:
            log.warning("prévia: %s falhou (%s)", label, ex)
            failed.append(label)

    await part("cabeçalho", lambda: say(ctx, chat_id, f"<b>Post pronto para aprovação · versão {p['version']}</b>\n{e(t['title'])}"))
    for group, name in ((feed, "Carrossel"), (story, "Stories")):
        for k in range(0, len(group), 10):
            media = [InputMediaPhoto(publisher.to_jpeg(Path(f)).read_bytes(), caption=name if j == 0 and k == 0 else None)
                     for j, f in enumerate(group[k:k + 10])]
            await part(name, lambda media=media: retry(lambda: ctx.bot.send_media_group(chat_id, media), attempts=2))
    vid = c["_render"].get("video")
    if vid and Path(vid["mp4"]).exists():
        cap = f"Slide {vid['index'] + 1} animado: vai como vídeo no carrossel do Instagram (LinkedIn e stories usam a imagem)."
        await part("vídeo", lambda: retry(lambda: ctx.bot.send_video(
            chat_id, Path(vid["mp4"]).read_bytes(), caption=cap, supports_streaming=True, width=1080, height=1350),
            attempts=2))
    await send_captions(ctx, chat_id, c, part)
    await send_approval(ctx, chat_id, post_id, failed)


async def send_captions(ctx, chat_id: int, c: dict, part=None):
    async def plain(label, call):
        await call()
    part = part or plain
    await part("legenda Instagram", lambda: say(ctx, chat_id, f"<b>Legenda Instagram</b>\n\n{e(c['caption_instagram'])}"))
    if "linkedin" in cfg.networks:
        await part("legenda LinkedIn", lambda: say(ctx, chat_id, f"<b>Legenda LinkedIn</b>\n\n{e(c['caption_linkedin'])}"))


def post_cost(p: dict) -> dict[str, float]:
    """Custo do post por agente: pesquisa do tema + criação, ajustes, revisões, vídeo e legendas."""
    t = db.topic(p["topic_id"])
    research_split = db.cost_split("research", t["batch"]) if t.get("batch") else {}
    return db.merge(research_split, db.topic_posts_split(p["topic_id"]))


def cost_lines(split: dict[str, float]) -> str:
    lines = f"Custo: {costs.split_line(split)}"
    if reviewer.active() or split.get("reviewer"):
        lines += f"\nGasto no {e(reviewer.label())}: <b>{costs.usd(split.get('reviewer', 0.0))}</b>"
    return lines


async def send_approval(ctx, chat_id: int, post_id: int, failed: list[str] | None = None):
    p = db.post(post_id)
    c = p["content"]
    t = db.topic(p["topic_id"])
    warn = c["_render"].get("overflow")
    note = f"\n⚠️ Slides com texto apertado: {', '.join(warn)}" if warn else ""
    if failed:
        note += f"\n⚠️ Não chegou: {', '.join(dict.fromkeys(failed))}. Use /pendentes para reenviar a prévia."
    rv = c.get("_review")
    if rv and rv.get("scores"):
        status = "aprovado" if rv["approved"] else "com ressalvas"
        note += f"\nRevisão editorial: {status}, nota mínima {rv['min']}/10."
    rvw = c.get("_reviewer") or {}
    for stage, name in (("plan", "plano do post"), ("post", "post pronto"), ("captions", "legendas")):
        if rvw.get(stage):
            note += f"\n{e(reviewer.summary(rvw[stage]).replace(reviewer.label(), reviewer.label() + ' · ' + name))}"
    v = c.get("_video")
    if v and v.get("needed", True) and c["_render"].get("video"):
        note += f"\nAgent Vídeo: slide animado ({e(v.get('reason', ''))})."
    elif v and not v.get("needed", True):
        note += f"\nAgent Vídeo: sem vídeo neste post ({e(v.get('reason', ''))})."
    if claude.notice():  # Anthropic sem crédito ou fora: o post seguiu sem Revisor e sem vídeo
        note += "\n" + e(claude.notice())
    note += "\n" + cost_lines(post_cost(p))
    # As legendas não citam fontes: elas ficam aqui, para quem aprova conferir.
    srcs = post_sources(t["payload"], c)
    if srcs:
        note += "\n\n<b>Fontes deste post</b>\n" + "\n".join(srcs)
    nets = " e ".join(n.capitalize() if n != "linkedin" else "LinkedIn" for n in cfg.networks)
    await say(ctx, chat_id, f"Aprova este post para {nets}?{note}", post_kb(post_id))


def post_sources(topic: dict, content: dict) -> list[str]:
    """Fontes do tema (com link) e as citadas nos slides, sem repetição."""
    out, seen = [], set()
    for src in topic.get("sources", []):
        url = src.get("url") or ""
        key = url or src.get("name")
        if key and key not in seen:
            seen.add(key)
            out.append(f"• <a href=\"{e(url)}\">{e(src.get('name') or url)}</a>" if url.startswith("http")
                       else f"• {e(src.get('name') or url)}")
    for sl in content.get("slides", []):
        txt = (sl.get("source") or "").strip()
        if txt and txt not in seen:
            seen.add(txt)
            out.append(f"• {e(txt)} (slide)")
    return out[:10]


async def ask_when(ctx, chat_id: int, post_id: int):
    await say(ctx, chat_id, "Post aprovado. <b>Posso postar agora ou prefere agendar?</b>\n"
                            "O horário sugerido segue a grade da semana; também dá para escolher outro.", when_kb(post_id))


async def parse_when(ctx, chat_id: int, post_id: int, text: str, kind: str = "post"):
    now = datetime.now(TZ)
    r = await llm.structured("Você converte pedidos de agendamento em data e hora.", prompts.SCHEDULE.format(
        now=now.strftime("%Y-%m-%d %H:%M"), weekday=DIAS[now.weekday()], tz=cfg.timezone, text=text,
        default_time=cfg.slots[0].split()[1] if cfg.slots else "10:00"), ScheduleParse, fast=True)
    when = None
    if r.ok and r.datetime_local:
        try:
            when = datetime.fromisoformat(r.datetime_local).replace(tzinfo=TZ)
        except ValueError:
            when = None
    if not when or when < now + timedelta(minutes=10):
        db.set_awaiting(chat_id, "schedule" if kind == "post" else "tweet_when", post_id)
        await ask(ctx, chat_id, "Não entendi o horário (ou ele já passou). Pode escrever de novo? "
                                "Ex.: \"quinta às 18h\", \"amanhã 9h\", \"15/10 12:30\".", "Dia e horário")
        return
    if kind == "tweet":
        await say(ctx, chat_id, f"Agendo o tweet para <b>{fmt(when)}</b>?", Kb([
            [Btn("✅ Confirmar", callback_data=f"twat:{post_id}:{int(when.timestamp())}")],
            [Btn("📅 Escolher outro horário", callback_data=f"tww:{post_id}"),
             Btn("🚀 Postar agora", callback_data=f"twp:{post_id}:now")]]))
        return
    await say(ctx, chat_id, f"Agendo para <b>{fmt(when)}</b>?", Kb([
        [Btn("✅ Confirmar", callback_data=f"at:{post_id}:{int(when.timestamp())}")],
        [Btn("📅 Escolher outro horário", callback_data=f"when:{post_id}"),
         Btn("🚀 Postar agora", callback_data=f"pub:{post_id}:now")]]))


async def create_post(ctx, chat_id: int, topic_id: int):
    await say(ctx, chat_id, "Tema aprovado. Escrevendo o carrossel, stories e legendas.")
    meter = costs.Meter()
    costs.current.set(meter)
    post_id = await content.create(topic_id)
    db.add_cost("post", post_id, meter.as_dict())
    await send_preview(ctx, chat_id, post_id)


async def revise_post(ctx, chat_id: int, post_id: int, feedback: str):
    await say(ctx, chat_id, "Entendido. Preparando a nova versão.")
    meter = costs.Meter()
    costs.current.set(meter)
    new_id = await content.revise(post_id, feedback)
    db.add_cost("post", new_id, meter.as_dict())
    await send_preview(ctx, chat_id, new_id)


async def do_publish(ctx, chat_id: int, post_id: int, mode: str, when: datetime | None = None):
    p = db.post(post_id)
    if p["status"] == "published":
        await say(ctx, chat_id, "Esse post já foi publicado.")
        return
    when = when or (publisher.asap() if mode == "now" else publisher.next_slot())
    via = "Buffer" if cfg.publisher == "buffer" else "Metricool"
    await say(ctx, chat_id, f"Enviando para o {via}.")
    try:
        out = await publisher.publish(post_id, when, now=mode == "now")
    except publisher.QueueFull as ex:
        # Nada foi criado: oferece outro horário ou postar na hora (que não ocupa a fila).
        await say(ctx, chat_id, f"{e(str(ex))}\nComo prefere seguir?", when_kb(post_id))
        return
    if cfg.publisher == "buffer":
        schedule_check(ctx.application.job_queue, chat_id, post_id, check_time(out, when, mode == "now"))
    if mode == "now":
        resumo = f"Publicando agora pelo {via}"
    else:
        resumo = f"Agendado no {via} para {fmt(when)}"
    vid = " com o slide animado em vídeo" if p["content"]["_render"].get("video") else ""
    li = " e o post do LinkedIn" if "linkedin" in cfg.networks else ""
    await say(ctx, chat_id, f"Feito. {resumo}: carrossel no Instagram{vid}, {out['stories']} stories "
                            f"(mesmos slides do feed){li} ({len(out['posts'])} publicações).\n"
                            f"<b>Custo final do post</b>\n{cost_lines(post_cost(p))}")
    t = db.topic(p["topic_id"])
    if t["week_id"]:
        w = db.row("SELECT * FROM weeks WHERE id = ?", t["week_id"])
        done = db.week_published_count(w["id"])
        if w["n_posts"] and done >= w["n_posts"]:
            db.update_week(w["id"], status="done")
            await say(ctx, chat_id, "Semana fechada. Se quiser mais um post, use /novopost.")


async def regen_captions(ctx, chat_id: int, post_id: int, direction: str | None = None):
    await say(ctx, chat_id, "Escrevendo outra legenda." if not direction else "Reescrevendo a legenda com o direcionamento.")
    meter = costs.Meter()
    costs.current.set(meter)
    await content.new_captions(post_id, direction)
    db.add_cost("post", post_id, meter.as_dict())
    await send_captions(ctx, chat_id, db.post(post_id)["content"])
    await send_approval(ctx, chat_id, post_id)


# ---------------- frente X ----------------

def tweet_text(tw: dict) -> str:
    p = tw["payload"]
    st = p.get("settings") or {}
    lines = [f"<b>Tweet para aprovação</b>{' · sobre o post aprovado' if tw.get('post_id') else ''}", "",
             e(tw["text"]), "",
             f"<i>{len(tw['text'])}/{cfg.x_max_chars} caracteres · linguagem {e(st.get('language', '—'))} · "
             f"tom {e(st.get('tone', '—'))}</i>"]
    if p.get("angle"):
        lines.append(f"<b>Opinião</b> {e(p['angle'])}")
    if p.get("source_url", "").startswith("http"):
        lines.append(f"<b>Fonte</b> <a href=\"{e(p['source_url'])}\">{e(p.get('source_name') or p['source_url'])}</a>")
    if p.get("review"):
        lines.append(e(reviewer.summary(p["review"])))
    if claude.notice():
        lines.append(e(claude.notice().replace("Revisor e Agent Vídeo não rodaram", "o Revisor não conferiu este tweet")))
    lines.append(cost_lines(db.cost_split("tweet", tw["id"])))
    return "\n".join(lines)


def tweet_kb(tid: int) -> Kb:
    return Kb([[Btn("✅ Aprovar tweet", callback_data=f"tw:{tid}:ok")],
               [Btn("🔄 Outro tweet", callback_data=f"tw:{tid}:new"), Btn("🧭 Direcionar", callback_data=f"tw:{tid}:dir")],
               [Btn("✖️ Descartar", callback_data=f"tw:{tid}:no")]])


def tweet_when_kb(tid: int) -> Kb:
    return Kb([[Btn("🚀 Pode postar agora", callback_data=f"twp:{tid}:now")],
               [Btn(f"🗓 {fmt(publisher.next_x_slot())}", callback_data=f"twp:{tid}:slot")],
               [Btn("📅 Escolher dia e horário", callback_data=f"tww:{tid}")]])


async def make_tweet(ctx, chat_id: int, seed: dict, direction: str | None = None, avoid: str | None = None):
    meter = costs.Meter()
    costs.current.set(meter)
    tid = await xarm.write(seed, direction, avoid)
    db.add_cost("tweet", tid, meter.as_dict())
    await say(ctx, chat_id, tweet_text(db.tweet(tid)), tweet_kb(tid))


async def do_publish_tweet(ctx, chat_id: int, tid: int, mode: str, when: datetime | None = None):
    t = db.tweet(tid)
    if t["status"] == "published":
        await say(ctx, chat_id, "Esse tweet já foi enviado ao Buffer.")
        return
    when = when or (publisher.asap() if mode == "now" else publisher.next_x_slot())
    try:
        await publisher.publish_tweet(tid, when, now=mode == "now")
    except publisher.QueueFull as ex:
        await say(ctx, chat_id, f"{e(str(ex))}\nComo prefere seguir?", tweet_when_kb(tid))
        return
    base = datetime.now(TZ) if mode == "now" else when
    ctx.application.job_queue.run_once(tweet_check_job, when=base + timedelta(minutes=3),
                                       data={"chat_id": chat_id, "tweet_id": tid, "attempt": 0}, name=f"tw-{tid}")
    resumo = "Publicando agora no X" if mode == "now" else f"Tweet agendado no X para {fmt(when)}"
    await say(ctx, chat_id, f"Feito. {resumo} pelo Buffer.\n{cost_lines(db.cost_split('tweet', tid))}")


async def tweet_check_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    try:
        r = await publisher.verify_tweet(d["tweet_id"])
    except Exception as ex:
        log.warning("conferência do tweet %s falhou: %s", d["tweet_id"], ex)
        r = {"status": "desconhecido"}
    if r["status"] == "sent":
        link = f" <a href=\"{e(r['link'])}\">ver</a>" if r.get("link") else ""
        await say(context, d["chat_id"], f"✅ Tweet publicado no X.{link}")
    elif r["status"] == "error":
        db.update_tweet(d["tweet_id"], status="failed")
        await say(context, d["chat_id"], f"⚠️ O tweet falhou no Buffer: {e(r.get('error') or 'sem detalhe')}",
                  Kb([[Btn("🔁 Tentar de novo", callback_data=f"twr:{d['tweet_id']}")]]))
    elif d["attempt"] < 6:
        context.job_queue.run_once(tweet_check_job, when=timedelta(minutes=5),
                                   data={**d, "attempt": d["attempt"] + 1}, name=f"tw-{d['tweet_id']}")


async def x_daily_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Todo dia: completa a cota proporcional de tweets da semana (no máximo 3 por dia)."""
    if xarm.mode() == "off" or not cfg.allowed_chat_ids:
        return
    chat = db.resolve_chat(cfg.allowed_chat_ids[0])
    for _ in range(min(xarm.due_now(), 3)):
        try:
            await make_tweet(ctx, chat, xarm.seed_auto())
        except Exception as ex:
            log.error("tweet automático falhou:\n%s", traceback.format_exc())
            await say(ctx, chat, f"Não consegui preparar o tweet do dia.\n<code>{e(str(ex))[:400]}</code>")
            break


def x_text() -> str:
    st = xarm.settings()
    done = len(db.week_tweets(xarm.week_key()))
    lines = ["<b>Frente X (Twitter)</b>",
             f"Modo: <b>{e(xarm.MODES.get(st['mode'], st['mode']))}</b>",
             f"Tweets por semana: <b>{st['per_week']}</b> (esta semana: {done})",
             f"Linguagem: <b>{e(st['language'])}</b> · Tom: <b>{e(st['tone'])}</b>",
             f"Horários: {', '.join(cfg.x_slots)} · preparo diário às {cfg.x_time}", "",
             "Ligado: cada post aprovado gera um tweet de opinião, e eu completo a cota na semana. "
             "Só X: Instagram e LinkedIn ficam pausados. Todo tweet passa pelo Revisor e por você."]
    return "\n".join(lines)


def x_kb() -> Kb:
    st = xarm.settings()
    mark = lambda on, txt: f"● {txt}" if on else txt  # noqa: E731
    langs = [Btn(mark(st["language"] == x, x), callback_data=f"xs:lang:{i}") for i, x in enumerate(xarm.LANGUAGES)]
    tones = [Btn(mark(st["tone"] == x, x), callback_data=f"xs:tone:{i}") for i, x in enumerate(xarm.TONES)]
    return Kb([
        [Btn(mark(st["mode"] == k, t), callback_data=f"xs:mode:{k}") for k, t in
         (("off", "Desligado"), ("on", "Ligado"), ("only", "Só X"))],
        [Btn(mark(st["per_week"] == n, str(n)), callback_data=f"xs:n:{n}") for n in xarm.COUNTS],
        langs[:3], langs[3:] + [Btn("Outra linguagem", callback_data="xs:lang:x")],
        tones[:3], tones[3:] + [Btn("Outro tom", callback_data="xs:tone:x")],
        [Btn("🐦 Gerar um tweet agora", callback_data="xs:gen:1")]])


async def on_x_setting(update: Update, ctx: ContextTypes.DEFAULT_TYPE, field: str, value: str):
    q = update.callback_query
    chat_id = update.effective_chat.id
    if field == "gen":
        await q.edit_message_reply_markup(None)
        await say(ctx, chat_id, "Preparando um tweet.")
        background(ctx, chat_id, make_tweet(ctx, chat_id, xarm.seed_auto()), "escrever o tweet")
        return
    if field in ("lang", "tone") and value == "x":
        db.set_awaiting(chat_id, "x_language" if field == "lang" else "x_tone")
        await ask(ctx, chat_id, "Descreva a linguagem (ex.: \"irônica, frases curtas, gírias de tech\")." if field == "lang"
                  else "Descreva o tom (ex.: \"contrarian, provocador, sem ser agressivo\").",
                  "Linguagem" if field == "lang" else "Tom")
        return
    if field == "mode":
        xarm.set_value("mode", value)
    elif field == "n":
        xarm.set_value("tweets_per_week", int(value))
    elif field == "lang":
        xarm.set_value("language", xarm.LANGUAGES[int(value)])
    elif field == "tone":
        xarm.set_value("tone", xarm.TONES[int(value)])
    await q.edit_message_text(x_text(), parse_mode=ParseMode.HTML, reply_markup=x_kb())
    if field == "mode":
        msg = {"off": "Frente X desligada.",
               "on": "Frente X ligada, em paralelo com Instagram e LinkedIn.",
               "only": "Só X: Instagram e LinkedIn pausados (não pergunto posts na segunda)."}[value]
        await say(ctx, chat_id, msg + ("" if value == "off" else
                                       f" Vou preparar {xarm.settings()['per_week']} tweets por semana para você aprovar."))


async def cmd_x(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await say(ctx, update.effective_chat.id, x_text(), x_kb())


async def cmd_tweet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    brief = " ".join(ctx.args)
    if not brief:
        db.set_awaiting(chat_id, "tweet_request")
        await ask(ctx, chat_id, "Sobre o que é o tweet? Pode mandar o assunto ou um link.", "Assunto do tweet")
        return
    await say(ctx, chat_id, "Preparando o tweet.")
    background(ctx, chat_id, make_tweet(ctx, chat_id, xarm.seed_request(brief)), "escrever o tweet")


async def tweet_from_post(ctx, chat_id: int, post_id: int):
    """Frente X ligada: depois do OK no post, um tweet de opinião sobre o mesmo assunto."""
    if db.row("SELECT id FROM tweets WHERE post_id = ? AND status != 'discarded'", post_id):
        return
    await say(ctx, chat_id, "Frente X: preparando um tweet de opinião sobre este assunto.")
    await make_tweet(ctx, chat_id, xarm.seed_from_post(post_id))


# ---------------- conferência da publicação ----------------

def check_time(out: dict, when: datetime, now: bool) -> datetime:
    """Alguns minutos depois do último item sair (stories saem 1 por minuto depois do carrossel)."""
    base = datetime.now(TZ) if now else when
    return base + timedelta(minutes=out.get("stories", 0) + 4)


def schedule_check(jq, chat_id: int, post_id: int, at: datetime, attempt: int = 0):
    jq.run_once(check_job, when=max(at, datetime.now(TZ) + timedelta(seconds=30)),
                data={"chat_id": chat_id, "post_id": post_id, "attempt": attempt}, name=f"check-{post_id}")


async def report(ctx, chat_id: int, post_id: int, res: list[dict], final: bool):
    failed = [r for r in res if r["status"] == "error"]
    pending = [r for r in res if r["status"] not in ("sent", "error")]
    links = [f"• {e(r['label'])}: <a href=\"{e(r['link'])}\">ver</a>" for r in res
             if r["status"] == "sent" and r["link"] and not r["label"].startswith("Story ")]
    stories_ok = sum(1 for r in res if r["label"].startswith("Story ") and r["status"] == "sent")
    lines = []
    if failed:
        lines.append("<b>⚠️ Parte da publicação falhou</b>")
        lines += [f"• {e(r['label'])}: {e(r['error'] or 'erro sem detalhe')}" for r in failed]
    elif pending:
        lines.append(f"<b>Publicação em andamento</b> ({len(res) - len(pending)} de {len(res)} já saíram)")
    else:
        lines.append("<b>✅ Publicado</b>")
    lines += links
    if stories_ok:
        lines.append(f"• Stories publicados: {stories_ok}")
    kb = Kb([[Btn("🔁 Tentar de novo o que falhou", callback_data=f"rp:{post_id}")],
             [Btn("✖️ Esquecer", callback_data=f"fg:{post_id}")]]) if failed else None
    if final or failed or not pending:
        await say(ctx, chat_id, "\n".join(lines), kb)
    out = db.post(post_id)["metricool"]
    if failed:
        out["error_reported"] = True  # avisa uma vez; só volta a conferir após "Tentar de novo"
        db.update_post(post_id, metricool=out)
    elif not pending:
        out["verified"] = True
        db.update_post(post_id, metricool=out)


def needs_check(out: dict) -> bool:
    """Conferência ainda faz sentido: não confirmada, não descartada e falha ainda não avisada."""
    return not (out.get("verified") or out.get("dismissed") or out.get("error_reported"))


async def check_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    if not needs_check(db.post(d["post_id"])["metricool"]):
        return  # descartado com "Esquecer", já confirmado ou falha já avisada
    try:
        res = await publisher.verify(d["post_id"])
    except Exception as ex:
        log.warning("conferência do post %s falhou: %s", d["post_id"], ex)
        res = None
    pending = res is None or any(r["status"] not in ("sent", "error") for r in res)
    if pending and d["attempt"] < 6 and not any(r["status"] == "error" for r in res or []):
        schedule_check(context.job_queue, d["chat_id"], d["post_id"], datetime.now(TZ) + timedelta(minutes=5), d["attempt"] + 1)
        return
    if res:
        await report(context, d["chat_id"], d["post_id"], res, final=True)


async def reschedule_checks(app: Application):
    """Ao subir, retoma a conferência de publicações ainda não confirmadas (jobs não sobrevivem a deploy)."""
    if cfg.publisher != "buffer" or not cfg.allowed_chat_ids:
        return
    chat = db.resolve_chat(cfg.allowed_chat_ids[0])
    for r in db.rows("SELECT id FROM posts WHERE status = 'published' ORDER BY id DESC LIMIT 10"):
        out = db.post(r["id"])["metricool"]
        if not needs_check(out) or "when" not in out:
            continue
        when = datetime.fromisoformat(out["when"])
        at = check_time(out, when, False)
        if at < datetime.now(TZ) - timedelta(hours=2):
            continue  # conferência antiga: já foi feita antes do reinício (use /verificar se precisar)
        schedule_check(app.job_queue, chat, r["id"], at)


async def verify_now(ctx, chat_id: int, post_id: int):
    await report(ctx, chat_id, post_id, await publisher.verify(post_id), final=True)


async def retry_publish(ctx, chat_id: int, post_id: int):
    res = await publisher.verify(post_id)
    labels = [r["label"] for r in res if r["status"] == "error"]
    if not labels:
        await say(ctx, chat_id, "Nada falhou nesta publicação.")
        return
    await say(ctx, chat_id, f"Tentando de novo: {e(', '.join(labels))}.")
    await publisher.retry_failed(post_id, labels)
    schedule_check(ctx.application.job_queue, chat_id, post_id, datetime.now(TZ) + timedelta(minutes=3))
    await say(ctx, chat_id, "Reenviado ao Buffer. Confiro em alguns minutos e aviso aqui.")


# ---------------- handlers ----------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text(
            f"Este chat não está autorizado. Chat ID: {update.effective_chat.id}\n"
            "Adicione esse número em TELEGRAM_ALLOWED_CHAT_IDS no .env e reinicie o agente.")
        return
    await say(ctx, update.effective_chat.id, HELP)


async def cmd_semana(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await ask_week(ctx, update.effective_chat.id)


async def cmd_novopost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    brief = " ".join(ctx.args)
    if xarm.mode() == "only":  # só X: pedido de post vira tweet
        if not brief:
            db.set_awaiting(update.effective_chat.id, "tweet_request")
            await ask(ctx, update.effective_chat.id, "Modo só X. Sobre o que é o tweet?", "Assunto do tweet")
            return
        background(ctx, update.effective_chat.id, make_tweet(ctx, update.effective_chat.id, xarm.seed_request(brief)),
                   "escrever o tweet")
        return
    if not brief:
        db.set_awaiting(update.effective_chat.id, "request")
        await ask(ctx, update.effective_chat.id, "Sobre o que é o post? Pode mandar o assunto, um link ou um briefing.",
                  "Assunto do post")
        return
    background(ctx, update.effective_chat.id,
               run_research(ctx, update.effective_chat.id, week_id=None, slots=1, request=brief, label_prefix="Proposta"),
               "apurar o pedido")


async def cmd_pesquisar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    focus = " ".join(ctx.args) or None
    w = db.get_or_create_week(week_start())
    if focus:
        db.update_week(w["id"], direction=focus)
    slots = max((w["n_posts"] or 1) - db.week_published_count(w["id"]), 1)
    background(ctx, update.effective_chat.id,
               run_research(ctx, update.effective_chat.id, week_id=w["id"], slots=slots, direction=focus),
               "pesquisar temas")


async def cmd_pendentes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reenvia prévia e botões de posts esperando aprovação ou horário (ex.: se uma mensagem se perdeu)."""
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    review = db.rows("SELECT id FROM posts p WHERE status = 'review' AND id = "
                     "(SELECT MAX(id) FROM posts WHERE topic_id = p.topic_id) ORDER BY id")
    approved = db.rows("SELECT id FROM posts WHERE status = 'approved' ORDER BY id")
    if not review and not approved:
        await say(ctx, chat_id, "Nenhum post pendente.")
        return
    for r in review:
        background(ctx, chat_id, send_preview(ctx, chat_id, r["id"]), "reenviar a prévia")
    for r in approved:
        await ask_when(ctx, chat_id, r["id"])


async def cmd_verificar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Confere agora, no Buffer, se o último post publicado saiu em todas as redes."""
    if not allowed(update):
        return
    r = db.row("SELECT id FROM posts WHERE status = 'published' ORDER BY id DESC LIMIT 1")
    if not r:
        await say(ctx, update.effective_chat.id, "Nenhum post publicado ainda.")
        return
    background(ctx, update.effective_chat.id, verify_now(ctx, update.effective_chat.id, r["id"]), "conferir a publicação")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    w = db.get_or_create_week(week_start())
    ts = db.week_topics(w["id"])
    lines = [f"<b>Semana de {w['week_start']}</b>", f"Posts pedidos: {w['n_posts'] or 'ainda não definido'}",
             f"Publicados/aprovados: {db.week_published_count(w['id'])}", f"Situação: {w['status']}",
             f"Custo na semana: {costs.split_line(db.split_since(w['week_start']))}"]
    if not cfg.anthropic_api_key:
        lines.append("Revisor e Agent Vídeo: desligados (falta ANTHROPIC_API_KEY)")
    elif claude.problem():
        lines.append(e(claude.notice()))
    else:
        lines.append(f"Revisor ({e(cfg.reviewer)}) e Agent Vídeo ({e(cfg.video_mode)}): prontos")
    xs = xarm.settings()
    if xarm.mode() != "off":
        lines.append(f"Frente X: {e(xarm.MODES[xarm.mode()])} · {len(db.week_tweets(xarm.week_key()))} de "
                     f"{xs['per_week']} tweets na semana")
    if w["direction"]:
        lines.append(f"Direcionamento: {e(w['direction'])}")
    lines += [f"• {e(t['title'])} ({t['status']})" for t in ts if t["status"] != "backlog"]
    await say(ctx, update.effective_chat.id, "\n".join(lines))


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        await q.answer("Não autorizado.")
        return
    await q.answer()
    chat_id = update.effective_chat.id
    kind, *args = q.data.split(":")
    if kind == "xs":  # configurações do X: o cartão é atualizado no lugar
        await on_x_setting(update, ctx, args[0], args[1])
        return
    await q.edit_message_reply_markup(None)  # evita clique duplo

    if kind == "n":
        n = int(args[0])
        w = db.get_or_create_week(week_start())
        db.update_week(w["id"], n_posts=n, status="researching" if n else "done")
        if n == 0:
            await say(ctx, chat_id, "Combinado, sem posts esta semana. Pedidos avulsos continuam valendo: /novopost.")
            return
        await say(ctx, chat_id, f"Combinado, {n} post{'s' if n > 1 else ''}. Quer indicar algum tema? Pode ser um "
                                "direcionamento geral ou um conteúdo específico, com título e link.",
                  Kb([[Btn("🧭 Indicar um tema", callback_data=f"tm:{w['id']}:y"),
                       Btn("🔎 Pode pesquisar", callback_data=f"tm:{w['id']}:n")]]))

    elif kind == "tm":
        week_id, choice = int(args[0]), args[1]
        if choice == "y":
            db.set_awaiting(chat_id, "theme", week_id)
            await ask(ctx, chat_id, "Qual tema? Três formatos:\n"
                                    "• geral: um assunto ou ângulo para a semana (vira o foco da pesquisa)\n"
                                    "• específico: \"Título do conteúdo · https://...\" (página ou vídeo do YouTube)\n"
                                    "• texto de referência colado aqui\n"
                                    "Com link, vídeo ou texto, uso só essas referências.", "Tema, link ou texto")
        else:
            w = db.row("SELECT * FROM weeks WHERE id = ?", week_id)
            background(ctx, chat_id, run_research(ctx, chat_id, week_id=week_id, slots=max(w["n_posts"] or 1, 1),
                                                  direction=w["direction"]), "pesquisar temas")

    elif kind == "ap":
        tid = int(args[0])
        db.set_topic_status(tid, "approved")
        background(ctx, chat_id, create_post(ctx, chat_id, tid), "criar o post")

    elif kind == "rj":
        t = db.topic(int(args[0]))
        db.set_topic_status(t["id"], "rejected")
        await say(ctx, chat_id, "Certo, buscando outra ideia.")
        background(ctx, chat_id, propose_replacement(ctx, chat_id, t), "buscar outra ideia")

    elif kind == "dr":
        db.set_awaiting(chat_id, "direction", int(args[0]))
        await ask(ctx, chat_id, "Qual direção devo seguir? Ex.: \"foca em agentes para atendimento\", "
                                "\"quero algo sobre custo de IA\", \"algo mais ligado a educação corporativa\".",
                  "Direção da pesquisa")

    elif kind == "adj":
        db.set_awaiting(chat_id, "revision", int(args[0]))
        await ask(ctx, chat_id, "O que ajusto? Pode falar de texto, slides específicos, dados, legenda ou foto.",
                  "Ajustes no post")

    elif kind == "ok":
        db.update_post(int(args[0]), status="approved")
        await ask_when(ctx, chat_id, int(args[0]))
        if xarm.mode() == "on":
            background(ctx, chat_id, tweet_from_post(ctx, chat_id, int(args[0])), "escrever o tweet do post")

    elif kind == "cp":
        background(ctx, chat_id, regen_captions(ctx, chat_id, int(args[0])), "escrever outra legenda")

    elif kind == "cd":
        db.set_awaiting(chat_id, "caption", int(args[0]))
        await ask(ctx, chat_id, "Como quer a legenda? Ex.: \"mais curta\", \"abre com o dado de custo\", "
                                "\"tom mais leve\", \"chamada para o diagnóstico\".", "Direção da legenda")

    elif kind == "tw":
        tid, action = int(args[0]), args[1]
        t = db.tweet(tid)
        if action == "ok":
            db.update_tweet(tid, status="approved")
            await say(ctx, chat_id, "Tweet aprovado. <b>Posto agora ou agendo?</b>", tweet_when_kb(tid))
        elif action == "new":
            db.update_tweet(tid, status="discarded")
            seed = {**t["payload"].get("seed", {}), "text": t["payload"].get("seed", {}).get("text", "")}
            background(ctx, chat_id, make_tweet(ctx, chat_id, seed, avoid=t["text"]), "escrever outro tweet")
        elif action == "dir":
            db.set_awaiting(chat_id, "tweet_dir", tid)
            await ask(ctx, chat_id, "Qual direção para o tweet? Ex.: \"mais ácido\", \"puxa para custo\", "
                                    "\"discorda do hype\", \"fala com founders\".", "Direção do tweet")
        else:
            db.update_tweet(tid, status="discarded")
            await say(ctx, chat_id, "Tweet descartado.")

    elif kind == "twp":
        background(ctx, chat_id, do_publish_tweet(ctx, chat_id, int(args[0]), args[1]), "publicar o tweet")

    elif kind == "tww":
        db.set_awaiting(chat_id, "tweet_when", int(args[0]))
        await ask(ctx, chat_id, "Para quando? Ex.: \"hoje 18h\", \"amanhã 9h\", \"sexta 12:30\".", "Dia e horário")

    elif kind == "twat":
        when = datetime.fromtimestamp(int(args[1]), TZ)
        background(ctx, chat_id, do_publish_tweet(ctx, chat_id, int(args[0]), "at", when), "agendar o tweet")

    elif kind == "twr":
        db.update_tweet(int(args[0]), status="approved")
        background(ctx, chat_id, do_publish_tweet(ctx, chat_id, int(args[0]), "now"), "publicar o tweet")

    elif kind == "when":
        db.set_awaiting(chat_id, "schedule", int(args[0]))
        await ask(ctx, chat_id, "Para quando? Ex.: \"quinta às 18h\", \"amanhã 9h\", \"15/10 12:30\".",
                  "Dia e horário")

    elif kind == "pub":
        background(ctx, chat_id, do_publish(ctx, chat_id, int(args[0]), args[1]), "publicar")

    elif kind == "fg":
        out = db.post(int(args[0]))["metricool"]
        out["dismissed"] = True
        db.update_post(int(args[0]), metricool=out)
        await say(ctx, chat_id, "Combinado, não vou mais avisar sobre esse post.")

    elif kind == "rp":
        background(ctx, chat_id, retry_publish(ctx, chat_id, int(args[0])), "tentar publicar de novo")

    elif kind == "at":
        when = datetime.fromtimestamp(int(args[1]), TZ)
        background(ctx, chat_id, do_publish(ctx, chat_id, int(args[0]), "at", when), "agendar")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    group = update.effective_chat.type in ("group", "supergroup")
    if group:
        # Em grupo, só conversa com quem responde a uma mensagem do bot ou o menciona;
        # o resto da conversa do grupo é ignorado.
        reply = update.message.reply_to_message
        to_bot = bool(reply and reply.from_user and reply.from_user.id == ctx.bot.id)
        mention = f"@{ctx.bot.username}".lower()
        if not to_bot and mention not in text.lower():
            return
        text = text.replace(f"@{ctx.bot.username}", "").strip()
    awaiting, ref = db.awaiting(chat_id)
    db.set_awaiting(chat_id, None)

    if awaiting == "schedule":
        background(ctx, chat_id, parse_when(ctx, chat_id, ref, text), "entender o horário")
        return
    if awaiting == "revision":
        background(ctx, chat_id, revise_post(ctx, chat_id, ref, text), "ajustar o post")
        return
    if awaiting == "direction":
        old = db.topic(ref)
        db.set_topic_status(old["id"], "rejected")
        if old["week_id"]:
            db.update_week(old["week_id"], direction=text)
        background(ctx, chat_id, run_research(ctx, chat_id, week_id=old["week_id"], slots=1, direction=text,
                                              request=text if old["origin"] == "request" else None,
                                              label_prefix="Nova ideia"), "pesquisar com o direcionamento")
        return
    if awaiting == "request":
        background(ctx, chat_id, run_research(ctx, chat_id, week_id=None, slots=1, request=text,
                                              label_prefix="Proposta"), "apurar o pedido")
        return
    if awaiting == "theme":
        background(ctx, chat_id, research_with_theme(ctx, chat_id, ref, text), "pesquisar o tema indicado")
        return
    if awaiting == "caption":
        background(ctx, chat_id, regen_captions(ctx, chat_id, ref, text), "reescrever a legenda")
        return
    if awaiting == "tweet_dir":
        t = db.tweet(ref)
        db.update_tweet(ref, status="discarded")
        background(ctx, chat_id, make_tweet(ctx, chat_id, t["payload"].get("seed") or xarm.seed_auto(), direction=text,
                                            avoid=t["text"]), "reescrever o tweet")
        return
    if awaiting == "tweet_when":
        background(ctx, chat_id, parse_when(ctx, chat_id, ref, text, kind="tweet"), "entender o horário")
        return
    if awaiting == "tweet_request":
        background(ctx, chat_id, make_tweet(ctx, chat_id, xarm.seed_request(text)), "escrever o tweet")
        return
    if awaiting in ("x_language", "x_tone"):
        xarm.set_value("language" if awaiting == "x_language" else "tone", text[:120])
        await say(ctx, chat_id, x_text(), x_kb())
        return

    intent = await llm.structured(prompts.ROLE, prompts.INTENT.format(text=text), Intent, fast=True)
    if intent.kind == "new_post" and xarm.mode() == "only":
        background(ctx, chat_id, make_tweet(ctx, chat_id, xarm.seed_request(
            text if research.is_reference(text) else intent.brief)), "escrever o tweet")
    elif intent.kind == "new_post":
        # Com link, vídeo ou texto de referência, vale a mensagem inteira (o resumo perderia o material).
        brief = text if research.is_reference(text) else intent.brief
        background(ctx, chat_id, run_research(ctx, chat_id, week_id=None, slots=1, request=brief,
                                              label_prefix="Proposta"), "apurar o pedido")
    elif intent.kind == "direction":
        w = db.get_or_create_week(week_start())
        db.update_week(w["id"], direction=intent.brief)
        slots = max((w["n_posts"] or 1) - db.week_published_count(w["id"]), 1)
        background(ctx, chat_id, run_research(ctx, chat_id, week_id=w["id"], slots=slots, direction=intent.brief),
                   "pesquisar com o direcionamento")
    else:
        await say(ctx, chat_id, HELP)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """Falhas passageiras de rede do Telegram (Bad Gateway, timeout) viram aviso curto; o resto vai completo."""
    err = ctx.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("Telegram instável: %s", err)
        return
    log.error("erro não tratado", exc_info=err)


def build_app() -> Application:
    app = (Application.builder().token(cfg.telegram_token).concurrent_updates(True)
           # Padrões da biblioteca (5s de leitura, pool de 1 conexão) são curtos para enviar 18 imagens.
           .connect_timeout(20).read_timeout(60).write_timeout(60).media_write_timeout(180)
           .pool_timeout(30).connection_pool_size(16)
           # getUpdates com request próprio para o vigia saber quando o polling para (agent/watchdog.py)
           .get_updates_request(watchdog.PollingRequest(connect_timeout=20, read_timeout=30, pool_timeout=30))
           .post_init(reschedule_checks)
           .build())
    app.add_handler(CommandHandler(["start", "ajuda", "help"], cmd_start))
    app.add_handler(CommandHandler("semana", cmd_semana))
    app.add_handler(CommandHandler("novopost", cmd_novopost))
    app.add_handler(CommandHandler("pesquisar", cmd_pesquisar))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pendentes", cmd_pendentes))
    app.add_handler(CommandHandler("verificar", cmd_verificar))
    app.add_handler(CommandHandler("x", cmd_x))
    app.add_handler(CommandHandler("tweet", cmd_tweet))
    app.add_error_handler(on_error)
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))

    h, m = map(int, cfg.weekly_time.split(":"))
    # No PTB, days vai de 0 = domingo a 6 = sábado; WEEKLY_DAY usa 0 = segunda.
    app.job_queue.run_daily(weekly_job, time=time(h, m, tzinfo=TZ), days=((cfg.weekly_day + 1) % 7,), name="weekly")
    xh, xm = map(int, cfg.x_time.split(":"))
    app.job_queue.run_daily(x_daily_job, time=time(xh, xm, tzinfo=TZ), name="x-daily")  # frente X: cota do dia
    return app
