"""Publicação: sobe as peças para um storage público e agenda via Buffer (padrão) ou Metricool.

Buffer (API GraphQL, inclusa no plano gratuito):
  Instagram  carrossel (várias imagens) + stories com os mesmos slides do feed, um por quadro
  LinkedIn   carrossel em PDF (documento nativo do LinkedIn) ou imagens (LINKEDIN_FORMAT)
  X          tweets de texto (frente X, agent/xarm.py), canal "twitter" do Buffer
  Plano gratuito: 10 posts agendados por canal ao mesmo tempo. Um post completo usa 1 carrossel
  + 1 story por slide no Instagram; antes de agendar, a fila é conferida para nunca publicar
  pela metade (BUFFER_QUEUE_LIMIT).
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import db
from .config import cfg

log = logging.getLogger(__name__)
DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


# ---------- quando publicar ----------

def next_slot(after: datetime | None = None, slots: list[str] | None = None) -> datetime:
    """Próximo horário da grade. Slot "TUE 10:00" = toda terça; "12:30" = todo dia (grade do X)."""
    tz = ZoneInfo(cfg.timezone)
    now = after or datetime.now(tz)
    candidates = []
    for slot in slots or cfg.slots:
        parts = slot.split()
        h, m = map(int, parts[-1].split(":"))
        if len(parts) == 1:
            dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt <= now + timedelta(minutes=15):
                dt += timedelta(days=1)
        else:
            delta = (DAYS.index(parts[0].upper()) - now.weekday()) % 7
            dt = (now + timedelta(days=delta)).replace(hour=h, minute=m, second=0, microsecond=0)
            if dt <= now + timedelta(minutes=15):
                dt += timedelta(days=7)
        candidates.append(dt)
    return min(candidates)


def next_x_slot(after: datetime | None = None) -> datetime:
    return next_slot(after, cfg.x_slots)


def asap() -> datetime:
    return (datetime.now(ZoneInfo(cfg.timezone)) + timedelta(minutes=5)).replace(second=0, microsecond=0)


def story_indices(content: dict, total: int) -> list[int]:
    """Quadros de stories publicados: capa, ilustração, primeiro dado, CTA (até STORY_FRAMES)."""
    if cfg.story_frames <= 0 or cfg.story_frames >= total:
        return list(range(total))
    ill = next((i for i, s in enumerate(content["slides"]) if s["layout"] == "illustration"), None)
    order = [0, ill, 1, total - 1] + list(range(2, total - 1))
    picked: list[int] = []
    for i in order:
        if i is not None and i not in picked and len(picked) < cfg.story_frames:
            picked.append(i)
    return sorted(picked)


# ---------- storage público (Supabase Storage) ----------

def to_jpeg(png: Path, width: int | None = 1080) -> Path:
    """JPEG para publicar (o feed do Instagram recusa PNG: erro 500 ao criar o carrossel).

    Parte do original em 2× e reduz para `width` com Lanczos (texto mais nítido do que renderizar
    direto em 1×). Qualidade 95 e sem subamostragem de cor, que borra texto colorido (Azure).
    width=None mantém a resolução do original.
    """
    from PIL import Image

    jpg = png.with_name(f"{png.stem}{'' if width is None else f'-{width}'}.jpg")
    if not jpg.exists() or jpg.stat().st_mtime < png.stat().st_mtime:
        img = Image.open(png).convert("RGB")
        if width and img.width != width:
            img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
        img.save(jpg, "JPEG", quality=95, subsampling=0, optimize=True, progressive=True)
    return jpg


async def upload(client: httpx.AsyncClient, path: Path, key: str) -> str:
    ctype = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".mp4": "video/mp4"}.get(path.suffix, "image/png")
    r = await client.post(
        f"{cfg.supabase_url}/storage/v1/object/{cfg.supabase_bucket}/{key}",
        content=path.read_bytes(),
        headers={"Authorization": f"Bearer {cfg.supabase_key}", "Content-Type": ctype, "x-upsert": "true"},
    )
    r.raise_for_status()
    return f"{cfg.supabase_url}/storage/v1/object/public/{cfg.supabase_bucket}/{key}"


async def make_pdf(feed: list[Path], out: Path) -> Path:
    """Junta as PNGs do carrossel num PDF 1080×1350 por página (carrossel nativo do LinkedIn)."""
    from playwright.async_api import async_playwright

    pages = "".join(f'<img src="{p.as_uri()}">' for p in feed)
    html = out.with_suffix(".html")
    html.write_text(
        "<html><head><style>@page{size:1080px 1350px;margin:0}body{margin:0}"
        "img{display:block;width:1080px;height:1350px;page-break-after:always}</style></head>"
        f"<body>{pages}</body></html>", encoding="utf-8")
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page()
        await page.goto(html.as_uri())
        await page.pdf(path=str(out), width="1080px", height="1350px", print_background=True)
        await b.close()
    return out


# ---------- Buffer ----------

BUFFER_API = "https://api.buffer.com"
CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}"""


async def _gql(client: httpx.AsyncClient, query: str, variables: dict | None = None) -> dict:
    r = await client.post(BUFFER_API, json={"query": query, "variables": variables or {}})
    if r.status_code == 429:
        raise RuntimeError("Buffer: limite de requisições atingido, tente mais tarde.")
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError("Buffer: " + "; ".join(e.get("message", "") for e in data["errors"]))
    return data["data"]


async def buffer_channels(client: httpx.AsyncClient) -> dict[str, str]:
    """{'instagram': id, 'linkedin': id, 'twitter': id}. IDs fixos no .env têm prioridade."""
    found = {k: v for k, v in {"instagram": cfg.buffer_instagram_channel, "linkedin": cfg.buffer_linkedin_channel,
                               "twitter": cfg.buffer_x_channel}.items() if v}
    if len(found) == 3:
        return found
    orgs = (await _gql(client, "{ account { organizations { id name } } }"))["account"]["organizations"]
    for org in orgs:
        chans = (await _gql(client, "query($i: ChannelsInput!) { channels(input: $i) { id service name isLocked isDisconnected } }",
                            {"i": {"organizationId": org["id"]}}))["channels"]
        for c in chans:
            if c["service"] in ("instagram", "linkedin", "twitter") and c["service"] not in found \
                    and not (c["isLocked"] or c["isDisconnected"]):
                found[c["service"]] = c["id"]
    return found


class QueueFull(RuntimeError):
    """A fila do Buffer não comporta o post inteiro (plano gratuito)."""


async def _buffer_queue_check(client: httpx.AsyncClient, channel_id: str, needed: int, label: str) -> None:
    if cfg.buffer_queue_limit <= 0:
        return
    try:
        orgs = (await _gql(client, "{ account { organizations { id } } }"))["account"]["organizations"]
        used = 0
        for org in orgs:
            res = (await _gql(client, """query($i: PostsInput!) { posts(first: 100, input: $i) { edges { node { id } } } }""",
                              {"i": {"organizationId": org["id"],
                                     "filter": {"channelIds": [channel_id], "status": ["scheduled"]}}}))["posts"]
            used += len(res["edges"])
    except Exception as e:  # checagem é proteção extra; se a consulta falhar, segue e o Buffer valida
        log.warning("não consegui ler a fila do Buffer: %s", e)
        return
    if used + needed > cfg.buffer_queue_limit:
        raise QueueFull(
            f"A fila do {label} no Buffer tem {used} posts agendados e este post precisa de {needed} "
            f"(limite do plano: {cfg.buffer_queue_limit}). Escolha um horário depois que os agendados saírem, "
            "poste agora, ou libere a fila no Buffer.")


async def _buffer_post(client: httpx.AsyncClient, channel: str, text: str, assets: list[dict], when: datetime | None,
                       metadata: dict | None = None) -> dict:
    """when=None publica na hora (shareNow), sem ocupar a fila de agendados."""
    inp = {
        "channelId": channel,
        "text": text,
        "schedulingType": "automatic",
        "mode": "shareNow" if when is None else "customScheduled",
        "assets": assets,
        "aiAssisted": True,
    }
    if when is not None:
        inp["dueAt"] = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if metadata:
        inp["metadata"] = metadata
    res = (await _gql(client, CREATE_POST, {"input": inp}))["createPost"]
    if "message" in res and "post" not in res:
        raise RuntimeError(f"Buffer recusou o post: {res['message']}")
    return res["post"]


async def publish_buffer(c: dict, feed_urls: list[str], story_urls: list[str], pdf_url: str | None,
                         when: datetime, now: bool = False, li_urls: list[str] | None = None,
                         video: dict | None = None) -> list[dict]:
    out = []
    async with httpx.AsyncClient(timeout=120, headers={"Authorization": f"Bearer {cfg.buffer_api_key}"}) as bf:
        ch = await buffer_channels(bf)
        missing = [n for n in cfg.networks if n not in ch]
        if missing:
            raise RuntimeError(f"Canal não conectado no Buffer: {', '.join(missing)}")

        # Confere a fila ANTES de criar qualquer coisa: o post sai inteiro ou não sai.
        if not now:
            if "instagram" in cfg.networks:
                await _buffer_queue_check(bf, ch["instagram"], 1 + len(story_urls), "Instagram")
            if "linkedin" in cfg.networks:
                await _buffer_queue_check(bf, ch["linkedin"], 1, "LinkedIn")

        due = None if now else when
        if "instagram" in cfg.networks:
            ig_meta = {"instagram": {"type": "post", "shouldShareToFeed": True}}
            images = [{"image": {"url": u}} for u in feed_urls]
            assets = feed_assets(feed_urls, video)
            try:
                post = await _buffer_post(bf, ch["instagram"], c["caption_instagram"], assets, due, ig_meta)
            except RuntimeError as e:
                if assets == images:
                    raise
                # carrossel com vídeo recusado: sai só com imagens (o slide animado vira o pôster)
                log.warning("carrossel com vídeo falhou (%s); publicando só com imagens", e)
                post = await _buffer_post(bf, ch["instagram"], c["caption_instagram"], images, due, ig_meta)
            out.append({"label": "Instagram feed", **post})
            # Stories na mesma ordem do carrossel, 1 minuto entre cada quadro.
            for i, u in enumerate(story_urls, 1):
                out.append({"label": f"Story {i}", **await _buffer_post(
                    bf, ch["instagram"], "", [{"image": {"url": u}}], None if now else when + timedelta(minutes=i),
                    {"instagram": {"type": "story", "shouldShareToFeed": False}})})

        if "linkedin" in cfg.networks:
            images = [{"image": {"url": u}} for u in (li_urls or feed_urls)]
            if pdf_url:
                title = c["slides"][0]["title"].replace("\n", " ")
                try:
                    out.append({"label": "LinkedIn", **await _buffer_post(
                        bf, ch["linkedin"], c["caption_linkedin"],
                        [{"document": {"url": pdf_url, "thumbnailUrl": feed_urls[0], "title": title}}], due)})
                except RuntimeError as e:  # documento recusado: cai para imagens
                    log.warning("LinkedIn em PDF falhou (%s); publicando como imagens", e)
                    out.append({"label": "LinkedIn", **await _buffer_post(bf, ch["linkedin"], c["caption_linkedin"], images, due)})
            else:
                out.append({"label": "LinkedIn", **await _buffer_post(bf, ch["linkedin"], c["caption_linkedin"], images, due)})
    return out


def feed_assets(feed_urls: list[str], video: dict | None) -> list[dict]:
    """Imagens do carrossel; o slide animado (se houver) entra como vídeo, com a peça estática de capa."""
    assets = [{"image": {"url": u}} for u in feed_urls]
    if video and video.get("url") and 0 <= video["index"] < len(assets):
        poster = feed_urls[video["index"]]
        assets[video["index"]] = {"video": {"url": video["url"], "thumbnailUrl": poster}}
    return assets


async def publish_tweet(tweet_id: int, when: datetime, now: bool = False) -> dict:
    """Agenda (ou publica na hora) um tweet aprovado no canal X do Buffer."""
    if not cfg.buffer_api_key:
        raise RuntimeError("Falta BUFFER_API_KEY para publicar no X.")
    t = db.tweet(tweet_id)
    async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {cfg.buffer_api_key}"}) as bf:
        ch = await buffer_channels(bf)
        if "twitter" not in ch:
            raise RuntimeError("Canal do X não conectado no Buffer (conecte em buffer.com > Channels).")
        if not now:
            await _buffer_queue_check(bf, ch["twitter"], 1, "X")
        post = await _buffer_post(bf, ch["twitter"], t["text"], [], None if now else when)
    out = {"via": "buffer", "when": when.isoformat(), "posts": [{"label": "X", **post}]}
    db.update_tweet(tweet_id, status="published", buffer=out, published_at=db.now())
    return out


async def verify_tweet(tweet_id: int) -> dict:
    """Status do tweet no Buffer: sent | error | scheduled | sending."""
    t = db.tweet(tweet_id)
    pid = (t["buffer"].get("posts") or [{}])[0].get("id")
    async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {cfg.buffer_api_key}"}) as bf:
        ch = await buffer_channels(bf)
        orgs = (await _gql(bf, "{ account { organizations { id } } }"))["account"]["organizations"]
        found = {}
        for org in orgs:
            d = await _gql(bf, POSTS_Q, {"i": {"organizationId": org["id"], "filter": {"channelIds": [ch["twitter"]]}}})
            found |= {e["node"]["id"]: e["node"] for e in d["posts"]["edges"]}
    n = found.get(pid, {})
    return {"id": pid, "status": n.get("status", "desconhecido"), "link": n.get("externalLink"),
            "error": (n.get("error") or {}).get("message")}


# ---------- Metricool (alternativa, exige plano pago com API) ----------

MC = "https://app.metricool.com/api"


async def _mc_normalize(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(f"{MC}/actions/normalize/image/url",
                         params={"url": url, "userId": cfg.metricool_user_id, "blogId": cfg.metricool_blog_id})
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        return r.text.strip().strip('"')
    return data if isinstance(data, str) else data.get("url") or data.get("data") or url


async def _mc_schedule(client: httpx.AsyncClient, *, network: str, text: str, media: list[str], when: datetime,
                       ig_type: str | None = None) -> dict:
    body: dict = {
        "text": text,
        "providers": [{"network": network}],
        "publicationDate": {"dateTime": when.strftime("%Y-%m-%dT%H:%M:%S"), "timezone": cfg.timezone},
        "autoPublish": True, "draft": False, "shortener": False, "media": media,
    }
    if network == "instagram":
        body["instagramData"] = {"type": ig_type or "POST", "autoPublish": True}
    if network == "linkedin":
        body["linkedinData"] = {"type": "post"}
    r = await client.post(f"{MC}/v2/scheduler/posts",
                          params={"userId": cfg.metricool_user_id, "blogId": cfg.metricool_blog_id}, json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"Metricool {network} {ig_type or ''}: {r.status_code} {r.text[:400]}")
    return r.json() if r.text else {}


async def publish_metricool(c: dict, feed_urls: list[str], story_urls: list[str], when: datetime) -> list[dict]:
    out = []
    async with httpx.AsyncClient(timeout=120, headers={"X-Mc-Auth": cfg.metricool_token}) as mc:
        feed = [await _mc_normalize(mc, u) for u in feed_urls]
        if "instagram" in cfg.networks:
            out.append(await _mc_schedule(mc, network="instagram", text=c["caption_instagram"], media=feed, when=when))
            for i, u in enumerate(story_urls, 1):
                out.append(await _mc_schedule(mc, network="instagram", text="", media=[await _mc_normalize(mc, u)],
                                              when=when + timedelta(minutes=i), ig_type="STORY"))
        if "linkedin" in cfg.networks:
            out.append(await _mc_schedule(mc, network="linkedin", text=c["caption_linkedin"], media=feed, when=when))
    return out


# ---------- orquestração ----------

async def publish(post_id: int, when: datetime, now: bool = False) -> dict:
    """Agenda (ou publica na hora, now=True) o post aprovado. Devolve um resumo para o Telegram."""
    missing = cfg.can_publish()
    if missing:
        raise RuntimeError("Faltam variáveis para publicar: " + ", ".join(missing))

    p = db.post(post_id)
    c = p["content"]
    feed = [Path(x) for x in c["_render"]["feed"]]
    story_all = [Path(x) for x in c["_render"]["story"]]
    story = [story_all[i] for i in story_indices(c, len(story_all))] if cfg.publish_stories else []
    prefix = f"{datetime.now():%Y%m%d}-{post_id:04d}-{c['slug']}"

    async with httpx.AsyncClient(timeout=120) as st:
        feed_urls = [await upload(st, j, f"{prefix}/feed/{j.name}") for j in map(to_jpeg, feed)]
        story_urls = [await upload(st, j, f"{prefix}/storie/{j.name}") for j in map(to_jpeg, story)]
        pdf_url = None
        if cfg.publisher == "buffer" and cfg.linkedin_format == "document" and "linkedin" in cfg.networks:
            vector = c["_render"].get("pdf")
            pdf = Path(vector) if vector and Path(vector).exists() else \
                await make_pdf(feed, Path(p["render_dir"]) / f"{c['slug']}.pdf")
            pdf_url = await upload(st, pdf, f"{prefix}/{pdf.name}")
        # Se o LinkedIn recusar o PDF, as imagens vão na resolução máxima (2160 px)
        li_urls = None
        if cfg.publisher == "buffer" and "linkedin" in cfg.networks:
            li_urls = [await upload(st, j, f"{prefix}/linkedin/{j.name}") for j in (to_jpeg(f, None) for f in feed)]
        # Slide animado do Agent Vídeo: MP4 no lugar da imagem estática do carrossel do Instagram
        video = None
        rv = c["_render"].get("video")
        if cfg.publisher == "buffer" and rv and rv.get("mp4") and Path(rv["mp4"]).exists():
            mp4 = Path(rv["mp4"])
            video = {"index": rv["index"], "url": await upload(st, mp4, f"{prefix}/feed/{mp4.name}")}

    if cfg.publisher == "metricool":
        posts = await publish_metricool(c, feed_urls, story_urls, when)
    else:
        posts = await publish_buffer(c, feed_urls, story_urls, pdf_url, when, now=now, li_urls=li_urls, video=video)

    out = {"via": cfg.publisher, "when": when.isoformat(), "posts": posts, "stories": len(story_urls)}
    db.update_post(post_id, status="published", metricool=out, published_at=db.now())
    archive(post_id)
    return out


# ---------- conferência da publicação (Buffer) ----------

POSTS_Q = """query($i: PostsInput!) { posts(first: 100, input: $i) { edges { node {
  id status externalLink error { message } } } } }"""


def _labels(out: dict) -> list[dict]:
    """Posts agendados com rótulo (publicações antigas não guardavam o rótulo: deduz pela ordem)."""
    posts = out.get("posts") or []
    if all("label" in x for x in posts):
        return posts
    n_st = out.get("stories", 0)
    labels = (["Instagram feed"] if "instagram" in cfg.networks else []) + [f"Story {i}" for i in range(1, n_st + 1)]
    labels += ["LinkedIn"] if "linkedin" in cfg.networks else []
    return [{"label": labels[i] if i < len(labels) else f"Post {i + 1}", **x} for i, x in enumerate(posts)]


async def verify(post_id: int) -> list[dict]:
    """Status real de cada publicação no Buffer: sent | error | scheduled | sending."""
    posts = _labels(db.post(post_id)["metricool"])
    async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {cfg.buffer_api_key}"}) as bf:
        ch = await buffer_channels(bf)
        orgs = (await _gql(bf, "{ account { organizations { id } } }"))["account"]["organizations"]
        found = {}
        for org in orgs:
            d = await _gql(bf, POSTS_Q, {"i": {"organizationId": org["id"], "filter": {"channelIds": list(ch.values())}}})
            found |= {e["node"]["id"]: e["node"] for e in d["posts"]["edges"]}
    res = []
    for x in posts:
        n = found.get(x.get("id"), {})
        res.append({"label": x["label"], "id": x.get("id"), "status": n.get("status", "desconhecido"),
                    "link": n.get("externalLink"), "error": (n.get("error") or {}).get("message")})
    return res


async def retry_failed(post_id: int, labels: list[str]) -> list[dict]:
    """Recria na hora (shareNow) as publicações que falharam, a partir das peças do post, em JPEG."""
    p = db.post(post_id)
    c = p["content"]
    out = p["metricool"]
    feed = [to_jpeg(Path(x)) for x in c["_render"]["feed"]]
    story_all = [Path(x) for x in c["_render"]["story"]]
    story = [to_jpeg(story_all[i]) for i in story_indices(c, len(story_all))]
    prefix = f"{datetime.now():%Y%m%d%H%M}-{post_id:04d}-{c['slug']}-retry"
    new = []
    async with httpx.AsyncClient(timeout=120) as st, \
            httpx.AsyncClient(timeout=120, headers={"Authorization": f"Bearer {cfg.buffer_api_key}"}) as bf:
        ch = await buffer_channels(bf)
        for label in labels:
            if label == "Instagram feed":
                urls = [await upload(st, j, f"{prefix}/feed/{j.name}") for j in feed]
                post = await _buffer_post(bf, ch["instagram"], c["caption_instagram"], [{"image": {"url": u}} for u in urls],
                                          None, {"instagram": {"type": "post", "shouldShareToFeed": True}})
            elif label.startswith("Story "):
                j = story[int(label.split()[1]) - 1]
                url = await upload(st, j, f"{prefix}/storie/{j.name}")
                post = await _buffer_post(bf, ch["instagram"], "", [{"image": {"url": url}}], None,
                                          {"instagram": {"type": "story", "shouldShareToFeed": False}})
            elif label == "LinkedIn":
                full = [to_jpeg(Path(x), None) for x in c["_render"]["feed"]]
                urls = [await upload(st, j, f"{prefix}/linkedin/{j.name}") for j in full]
                post = await _buffer_post(bf, ch["linkedin"], c["caption_linkedin"], [{"image": {"url": u}} for u in urls], None)
            else:
                continue
            new.append({"label": label, **post})
    # substitui as entradas que falharam pelas novas tentativas
    keep = [x for x in _labels(out) if x["label"] not in labels]
    out["posts"] = keep + new
    for flag in ("verified", "error_reported", "dismissed"):  # nova tentativa: volta a conferir
        out.pop(flag, None)
    db.update_post(post_id, metricool=out)
    return new


def archive(post_id: int) -> Path:
    """Copia as peças aprovadas para ARCHIVE_DIR/<data>-<slug>/ (pasta local ou volume no Railway)."""
    p = db.post(post_id)
    c = p["content"]
    dest = cfg.archive_dir / f"{datetime.now():%Y-%m-%d}-{c['slug']}"
    src = Path(p["render_dir"])
    for sub in ("feed", "storie"):
        if (src / sub).exists():
            shutil.copytree(src / sub, dest / sub, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*-frames"))
    for f in ("pecas.html", "stories.html", "carrossel.pdf", f"{c['slug']}.pdf"):
        if (src / f).exists():
            shutil.copy2(src / f, dest / f)
    (dest / "legendas.md").write_text(
        f"# {c['slug']}\n\n## Instagram\n\n{c['caption_instagram']}\n\n## LinkedIn\n\n{c['caption_linkedin']}\n",
        encoding="utf-8",
    )
    return dest
