"""Bot do Telegram: conversa com o humano e orquestra pesquisa → aprovação → criação → aprovação → publicação.

Callbacks (botões):
  n:<qtd>              quantos posts na semana (em seguida pergunta se há tema indicado)
  tm:<week>:y|n        indicar um tema (texto livre: geral ou título + link) / pode pesquisar
  ap:<topic>           aprovar tema          rj:<topic>   outra ideia        dr:<topic>  direcionar
  ok:<post>            aprovar post          adj:<post>   pedir ajustes
  pub:<post>:now|slot  publicar agora / no próximo horário sugerido
  when:<post>          escolher dia e horário (texto livre)   at:<post>:<epoch>  confirmar agendamento
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

from . import content, costs, db, llm, prompts, publisher, research
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
    "/verificar  confere se o último post saiu em todas as redes\n\n"
    "Também pode escrever livremente: \"faz um post sobre o lançamento X\"."
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
                Btn("✏️ Pedir ajustes", callback_data=f"adj:{post_id}")]])


def when_kb(post_id: int) -> Kb:
    return Kb([[Btn("🚀 Pode postar agora", callback_data=f"pub:{post_id}:now")],
               [Btn(f"🗓 {fmt(publisher.next_slot())}", callback_data=f"pub:{post_id}:slot")],
               [Btn("📅 Escolher dia e horário", callback_data=f"when:{post_id}")]])


# ---------------- ciclo semanal ----------------

async def ask_week(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int | None = None):
    w = db.get_or_create_week(week_start())
    db.update_week(w["id"], status="asking")
    kb = Kb([[Btn("1 post", callback_data="n:1"), Btn("2 posts", callback_data="n:2")],
             [Btn("Nenhum esta semana", callback_data="n:0")]])
    # Ciclo semanal vai só para o chat principal (o primeiro de TELEGRAM_ALLOWED_CHAT_IDS, ex.: o grupo).
    await say(ctx, chat_id or db.resolve_chat(cfg.allowed_chat_ids[0]),
              f"Bom dia. Começando a semana de conteúdo da {e(BRAND_NAME)}\n<b>Quantos posts faço esta semana?</b>", kb)


async def weekly_job(ctx: ContextTypes.DEFAULT_TYPE):
    await ask_week(ctx)


async def run_research(ctx, chat_id: int, *, week_id: int | None, slots: int, direction: str | None = None,
                       request: str | None = None, label_prefix: str = "Tema"):
    """Pesquisa, guarda candidatos (os primeiros como propostos, o resto como reserva) e envia ao humano."""
    origin = "request" if request else "weekly"
    await say(ctx, chat_id, f"Pesquisando {e(SUBJECT)}. Leva alguns minutos."
              if not request else f"Apurando o assunto: <i>{e(request)}</i>.")
    meter = costs.Meter()
    costs.current.set(meter)  # vale só para esta tarefa
    res = await research.research(n=research.candidates_for(slots), direction=direction, request=request)
    batch = f"{origin}-{uuid.uuid4().hex[:8]}"
    db.add_cost("research", batch, meter.as_dict())
    ids = []
    for i, cand in enumerate(res.candidates):
        status = "proposed" if i < slots else "backlog"
        ids.append(db.add_topic(week_id, cand.model_dump(), status, origin, batch, request or direction))
    if week_id:
        db.update_week(week_id, status="proposing")
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
    if not research.URL_RX.search(text):
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
    await part("legenda Instagram", lambda: say(ctx, chat_id, f"<b>Legenda Instagram</b>\n\n{e(c['caption_instagram'])}"))
    await part("legenda LinkedIn", lambda: say(ctx, chat_id, f"<b>Legenda LinkedIn</b>\n\n{e(c['caption_linkedin'])}"))
    warn = c["_render"].get("overflow")
    note = f"\n⚠️ Slides com texto apertado: {', '.join(warn)}" if warn else ""
    if failed:
        note += f"\n⚠️ Não chegou: {', '.join(dict.fromkeys(failed))}. Use /pendentes para reenviar a prévia."
    c_research = db.cost("research", t.get("batch")) if t.get("batch") else 0.0
    c_post = db.topic_posts_cost(p["topic_id"])
    note += (f"\nCusto OpenAI: pesquisa {costs.usd(c_research)} · criação e ajustes {costs.usd(c_post)} · "
             f"<b>total {costs.usd(c_research + c_post)}</b>")
    rv = c.get("_review")
    if rv and rv.get("scores"):
        status = "aprovado" if rv["approved"] else "com ressalvas"
        note += f"\nRevisão editorial: {status}, nota mínima {rv['min']}/10."
    await say(ctx, chat_id, f"Aprova este post para Instagram e LinkedIn?{note}", post_kb(post_id))


async def ask_when(ctx, chat_id: int, post_id: int):
    await say(ctx, chat_id, "Post aprovado. <b>Posso postar agora ou prefere agendar?</b>\n"
                            "O horário sugerido segue a grade da semana; também dá para escolher outro.", when_kb(post_id))


async def parse_when(ctx, chat_id: int, post_id: int, text: str):
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
        db.set_awaiting(chat_id, "schedule", post_id)
        await ask(ctx, chat_id, "Não entendi o horário (ou ele já passou). Pode escrever de novo? "
                                "Ex.: \"quinta às 18h\", \"amanhã 9h\", \"15/10 12:30\".", "Dia e horário")
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
    await say(ctx, chat_id, f"Feito. {resumo}: carrossel no Instagram, {out['stories']} stories "
                            f"(mesmos slides do feed) e o post do LinkedIn ({len(out['posts'])} publicações).")
    t = db.topic(p["topic_id"])
    if t["week_id"]:
        w = db.row("SELECT * FROM weeks WHERE id = ?", t["week_id"])
        done = db.week_published_count(w["id"])
        if w["n_posts"] and done >= w["n_posts"]:
            db.update_week(w["id"], status="done")
            await say(ctx, chat_id, "Semana fechada. Se quiser mais um post, use /novopost.")


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
             f"Custo OpenAI na semana: {costs.usd(db.cost_since(w['week_start']))}"]
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
            await ask(ctx, chat_id, "Qual tema? Dois formatos:\n"
                                    "• geral: \"tendências de marketing para 2027\"\n"
                                    "• específico: \"Título do conteúdo · https://...\"", "Tema ou título + link")
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

    intent = await llm.structured(prompts.ROLE, prompts.INTENT.format(text=text), Intent, fast=True)
    if intent.kind == "new_post":
        background(ctx, chat_id, run_research(ctx, chat_id, week_id=None, slots=1, request=intent.brief,
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
           .post_init(reschedule_checks)
           .build())
    app.add_handler(CommandHandler(["start", "ajuda", "help"], cmd_start))
    app.add_handler(CommandHandler("semana", cmd_semana))
    app.add_handler(CommandHandler("novopost", cmd_novopost))
    app.add_handler(CommandHandler("pesquisar", cmd_pesquisar))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pendentes", cmd_pendentes))
    app.add_handler(CommandHandler("verificar", cmd_verificar))
    app.add_error_handler(on_error)
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))

    h, m = map(int, cfg.weekly_time.split(":"))
    # No PTB, days vai de 0 = domingo a 6 = sábado; WEEKLY_DAY usa 0 = segunda.
    app.job_queue.run_daily(weekly_job, time=time(h, m, tzinfo=TZ), days=((cfg.weekly_day + 1) % 7,), name="weekly")
    return app
