"""Criação e revisão do post: plano revisado → texto (modelo) → guarda de linguagem → editor-chefe →
Agent Vídeo (opcional) → render → checagem de layout → Revisor (1 pedido de ajuste) → humano."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import db, illustration, llm, prompts, reviewer, video
from .config import brand, cfg
from .models import Captions, EditorialReview, PostContent
from .render import renderer

log = logging.getLogger(__name__)

# Regras de linguagem da marca verificáveis por máquina (brand.toml → [language]).
LANG = brand.get("language", {})
BANNED = LANG.get("banned", [])
ALL_CHECKS = {
    "travessao": (re.compile(r"—|–|\s--\s"), "travessão como pausa"),
    "exclamacao": (re.compile(r"!"), "exclamação"),
    "paralelismo": (re.compile(r"\bnão (é|são|se trata)\b[^.;]{0,80}?,\s*(é|são|e sim)\b", re.I),
                    "paralelismo negativo ('não é X, é Y')"),
    "dicotomia": (re.compile(r"\bmais do que\b[^.]{0,60},\s", re.I), "dicotomia de efeito ('mais do que A, B')"),
}
CHECKS = [ALL_CHECKS[k] for k in LANG.get("checks", []) if k in ALL_CHECKS]
if LANG.get("brand_regex"):
    CHECKS.append((re.compile(LANG["brand_regex"]), LANG.get("brand_regex_msg", "grafia do nome da marca")))


META = ("_review", "_reviewer", "_video")  # metadados que sobrevivem às reescritas e ao render


def clean(content: dict) -> dict:
    """Remove metadados internos (_render, _pillar...) e o slide animado antes de mandar ao modelo de
    texto (ele não conhece o layout 'motion'; o slide volta ao lugar com video.carry)."""
    return {k: v for k, v in video.strip(content).items() if not k.startswith("_")}


def _texts(content: dict) -> list[tuple[str, str]]:
    out = [("caption_instagram", content["caption_instagram"]), ("caption_linkedin", content["caption_linkedin"])]
    for i, s in enumerate(content["slides"], 1):
        for k in ("eyebrow", "title", "lead", "body", "tag"):
            if s.get(k):
                out.append((f"slide {i} {k}", s[k]))
        for it in s.get("items") or []:
            out += [(f"slide {i} item", it["title"]), (f"slide {i} item", it["text"])]
        for m in s.get("metrics") or []:
            out.append((f"slide {i} metric", m["label"]))
        for lb in s.get("visual_labels") or []:
            out.append((f"slide {i} rótulo do diagrama", lb))
    return out


def language_violations(content: dict) -> list[str]:
    found = []
    for where, text in _texts(content):
        for rx, label in CHECKS:
            if m := rx.search(text):
                found.append(f"- {where}: {label} em \"…{text[max(0, m.start() - 40): m.end() + 40]}…\"")
        low = text.lower()
        for w in BANNED:
            if w in low:
                found.append(f"- {where}: palavra a evitar '{w}'")
    return found


def _structure_problems(content: dict) -> list[str]:
    s = video.strip(content)["slides"]  # o slide animado é extra e não conta nas regras de estrutura
    p = []
    if not s or s[0]["layout"] != "cover":
        p.append("o primeiro slide precisa ser 'cover'")
    if s and s[-1]["layout"] != "cta":
        p.append("o último slide precisa ser 'cta'")
    if not 5 <= len(s) <= 10:
        p.append("o carrossel precisa ter entre 5 e 10 slides")
    for i, sl in enumerate(s, 1):
        need = {"big_number": "big", "metrics": "metrics", "list": "items", "bars": "bars", "line": "series",
                "illustration": "visual_brief"}.get(sl["layout"])
        if need and not sl.get(need):
            p.append(f"slide {i} ({sl['layout']}) está sem '{need}'")
    # Legendas não citam fontes (as fontes vão para o humano no Telegram).
    for cap in ("caption_instagram", "caption_linkedin"):
        if re.search(r"\bfontes?\s*:", content.get(cap) or "", re.I):
            p.append(f"{cap} cita fonte: tire a fonte da legenda (ela aparece só na aprovação no Telegram)")
    # Regra fixa: todo post tem 1 slide de ilustração/diagrama no meio do carrossel.
    ill = [i for i, sl in enumerate(s) if sl["layout"] == "illustration"]
    if len(ill) != 1:
        p.append(f"o post precisa de exatamente 1 slide 'illustration' (tem {len(ill)})")
    elif not 2 <= ill[0] <= len(s) - 3:
        p.append("o slide 'illustration' precisa ficar no meio: depois do 2º slide e antes do penúltimo")
    return p


async def _ask(prompt: str, keep: dict | None = None) -> dict:
    """Pede o post completo ao modelo de texto. keep: versão anterior, de onde volta o slide animado."""
    res = await llm.structured(prompts.base_instructions(), prompt, PostContent, brand_files=True,
                               effort=cfg.content_reasoning_effort)
    out = res.model_dump()
    return video.carry(out, keep) if keep else out


async def _editorial(content: dict, topic: dict, rounds: int = 2) -> dict:
    """Editor-chefe: nota cada critério e reescreve até aprovar (8+ em tudo), no máximo `rounds` vezes."""
    review = None
    for _ in range(rounds):
        review = await llm.structured(
            prompts.base_instructions(),
            prompts.EDITOR.format(topic=json.dumps(topic, ensure_ascii=False),
                                  current=json.dumps(clean(content), ensure_ascii=False)),
            EditorialReview, effort=cfg.content_reasoning_effort)
        log.info("revisão editorial: %s", {c.criterion[:20]: c.score for c in review.scores})
        if review.approved:
            break
        content = await _polish(video.carry(review.content.model_dump(), content))
    notes = {c.criterion: c.score for c in review.scores} if review else {}
    content["_review"] = {"approved": bool(review and review.approved), "scores": notes,
                          "min": min(notes.values()) if notes else None}
    return content


async def _polish(content: dict) -> dict:
    """Até 2 rodadas de correção de estrutura e linguagem."""
    for _ in range(2):
        problems = _structure_problems(content) + language_violations(content)
        if not problems:
            break
        log.info("corrigindo %d problemas", len(problems))
        content = await _ask(prompts.LANGUAGE_FIX.format(
            violations="\n".join(problems), current=json.dumps(clean(content), ensure_ascii=False)), keep=content)
    return content


async def _render_checked(content: dict, pillar: str, photos, out_dir: Path) -> tuple[dict, dict]:
    """Renderiza; se algum slide estourar a área, pede versão mais curta (até 2 vezes)."""
    cache = out_dir.parent / "illustrations"
    for attempt in range(3):
        content = await illustration.ensure(content, cache)
        result = await renderer.render(content, pillar, photos, out_dir)
        if not result["overflow"] or attempt == 2:
            return content, result
        log.info("slides estourados: %s", result["overflow"])
        content = await _ask(prompts.SHORTEN.format(
            ids=", ".join(result["overflow"]), current=json.dumps(clean(content), ensure_ascii=False)), keep=content)
        content = await _polish(content)
    return content, result


async def _photos(content: dict, out_dir: Path):
    if cfg.photo_source == "generate":
        paths = []
        for name in ("cover", "cta"):
            p = out_dir / f"photo-{name}.png"
            await llm.generate_image(
                f"{brand['visual'].get('photo_mood', 'Fotografia editorial')}, sem texto, alta resolução. "
                f"Atmosfera: {content['photo_mood']}. "
                f"Variação para {'abertura' if name == 'cover' else 'encerramento'}.", p)
            paths.append(p)
        return tuple(paths)
    return renderer.pick_photos(db.recent_photos())


async def create(topic_id: int) -> int:
    t = db.topic(topic_id)
    topic = t["payload"]
    # Revisor, etapa 2: a ideia de como montar o post. O pedido de ajuste vira orientação obrigatória.
    plan_rv = await reviewer.review_plan(topic)
    brief = dict(topic)
    if plan_rv and plan_rv["verdict"] == "ajustar" and plan_rv["change_request"]:
        brief["orientacoes_obrigatorias_do_revisor"] = plan_rv["change_request"]
        plan_rv["applied"] = True
    topic_json = json.dumps(brief, ensure_ascii=False, indent=1)
    content = await _ask(prompts.CONTENT.format(topic=topic_json, design=prompts.design(), site=cfg.site_url))
    content = await _polish(content)
    content = await _editorial(content, brief)
    content = await _with_video(content, topic)
    content["_reviewer"] = {"plan": plan_rv} if plan_rv else {}
    post_id = await _save(topic_id, topic["pillar"], content, version=1)
    return await _review_post(post_id, topic)


async def _with_video(content: dict, topic: dict, feedback: str | None = None) -> dict:
    """Agent Vídeo: decide se o carrossel ganha um slide animado (e escreve a animação)."""
    vp = await video.plan(content, topic, feedback)
    if vp and vp.needed:
        return video.insert(content, vp)
    content = video.strip(content)
    if vp:  # decidiu que não precisa: a prévia mostra o motivo
        content["_video"] = {"needed": False, "reason": vp.reason}
    else:
        content.pop("_video", None)
    return content


async def _review_post(post_id: int, topic: dict) -> int:
    """Revisor, etapa 3: lê o post pronto (texto + peças). Se pedir ajuste, aplica UMA vez e segue
    para o humano sem nova rodada. Devolve o id do post que vai para aprovação."""
    p = db.post(post_id)
    c = p["content"]
    rv = await reviewer.review_post(c, topic, c["_render"]["feed"])
    if rv is None:
        return post_id
    info = c.get("_reviewer") or {}
    info["post"] = {"verdict": rv.verdict, "issues": rv.issues[:5], "applied": False,
                    "scores": {x.criterion: x.score for x in rv.scores}}
    if rv.verdict != "ajustar" or not rv.change_request:
        c["_reviewer"] = info
        db.update_post(post_id, content=c)
        return post_id
    new = await _ask(prompts.REVISION.format(
        feedback=f"Pedido do Revisor (obrigatório): {rv.change_request}", topic=json.dumps(topic, ensure_ascii=False),
        current=json.dumps(clean(c), ensure_ascii=False)), keep=c)
    new = await _polish(new)
    info["post"]["applied"] = True
    new["_reviewer"] = info
    if c.get("_review"):
        new["_review"] = c["_review"]
    new_id = await _save(p["topic_id"], c.get("_pillar") or topic["pillar"], new, p["version"],
                         photos=p["photos"], tag="revisor")
    db.update_post(post_id, status="superseded")
    return new_id


VIDEO_WORDS = re.compile(r"v[íi]deo|anima[çc]", re.I)


async def revise(post_id: int, feedback: str) -> int:
    old = db.post(post_id)
    t = db.topic(old["topic_id"])
    db.update_post(post_id, status="changes", feedback=feedback)
    about_video = bool(VIDEO_WORDS.search(feedback))
    content = await _ask(prompts.REVISION.format(
        feedback=feedback, topic=json.dumps(t["payload"], ensure_ascii=False),
        current=json.dumps(clean(old["content"]), ensure_ascii=False)), keep=None if about_video else old["content"])
    content = await _polish(content)
    # Revisão editorial também na nova versão, com as orientações do humano como obrigatórias.
    content = await _editorial(content, {**t["payload"], "orientacoes_do_humano_obrigatorias": feedback}, rounds=1)
    if about_video:  # o humano falou do vídeo: o Agent Vídeo refaz (ou tira) com a orientação
        content = await _with_video(content, t["payload"], feedback)
    keep_photos = "foto" not in feedback.lower() and "imagem" not in feedback.lower()
    new_id = await _save(old["topic_id"], t["payload"]["pillar"], content, old["version"] + 1,
                         photos=old["photos"] if keep_photos else None)
    return await _review_post(new_id, t["payload"])


def _caption_problems(c: dict) -> list[str]:
    p = [v for v in language_violations(c) if v.startswith("- caption")]
    for cap in ("caption_instagram", "caption_linkedin"):
        if re.search(r"\bfontes?\s*:", c.get(cap) or "", re.I):
            p.append(f"- {cap}: cita fonte (as fontes ficam só no Telegram)")
    return p


async def new_captions(post_id: int, direction: str | None = None) -> None:
    """Legendas novas para o mesmo post (botão "Outra legenda" ou "Direcionar legenda").
    Guarda de linguagem + Revisor (1 pedido de ajuste); slides e peças não mudam."""
    p = db.post(post_id)
    c = p["content"]
    t = db.topic(p["topic_id"])
    base = dict(topic=json.dumps({k: v for k, v in t["payload"].items() if not k.startswith("_")}, ensure_ascii=False),
                post=json.dumps({"slides": clean(c)["slides"]}, ensure_ascii=False),
                current=json.dumps({"caption_instagram": c["caption_instagram"],
                                    "caption_linkedin": c["caption_linkedin"]}, ensure_ascii=False),
                direction=direction or "Crie uma alternativa claramente diferente das atuais (outro gancho, outra abertura).")

    async def write(extra: str = "") -> Captions:
        return await llm.structured(prompts.base_instructions(), prompts.CAPTIONS.format(**base) + extra, Captions,
                                    brand_files=True, effort=cfg.content_reasoning_effort)

    caps = await write()
    probs = _caption_problems({**c, **caps.model_dump()})
    if probs:
        caps = await write("\n\n# Corrija também\n" + "\n".join(probs))
    rv = await reviewer.review_text("as legendas do post (Instagram e LinkedIn)",
                                    json.dumps(caps.model_dump(), ensure_ascii=False),
                                    f"Post: {base['post'][:6000]}\nOrientação do humano: {direction or '-'}",
                                    "legenda do Instagram curta e com abertura forte; LinkedIn mais analítica; "
                                    "chamada final clara; sem citar fontes.")
    note = None
    if rv:
        note = {"verdict": rv.verdict, "issues": rv.issues[:3], "applied": False}
        if rv.verdict == "ajustar" and rv.change_request:
            caps = await write(f"\n\n# Pedido do Revisor (obrigatório)\n{rv.change_request}")
            note["applied"] = True
    c["caption_instagram"], c["caption_linkedin"] = caps.caption_instagram, caps.caption_linkedin
    info = c.get("_reviewer") or {}
    if note:
        info["captions"] = note
    c["_reviewer"] = info
    db.update_post(post_id, content=c)


async def _save(topic_id: int, pillar: str, content: dict, version: int, photos: list[str] | None = None,
                tag: str = "") -> int:
    out_dir = cfg.runs_dir / f"{topic_id:04d}-{content['slug']}" / f"v{version}{'-' + tag if tag else ''}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if photos and len(photos) == 2 and all(Path(p).exists() for p in photos):
        pics = (Path(photos[0]), Path(photos[1]))
    else:
        pics = await _photos(content, out_dir)
    meta = {k: content[k] for k in META if k in content}
    content, result = await _render_checked(content, pillar, pics, out_dir)
    # Slide animado: grava o MP4 a partir da peça renderizada (Instagram); o PNG é o pôster.
    mi = video.motion_index(content)
    if mi is not None:
        mp4 = await video.record(out_dir / "pecas.html", f"slide-{mi + 1:02d}",
                                 out_dir / "feed" / f"carrossel-{mi + 1:02d}.mp4",
                                 int(content["slides"][mi].get("motion_seconds") or cfg.video_seconds))
        result["video"] = {"index": mi, "mp4": str(mp4)} if mp4 else None
    content["_render"] = result
    content["_pillar"] = pillar
    content.update(meta)
    return db.add_post(topic_id, content, str(out_dir), [str(p) for p in pics if p], version)
