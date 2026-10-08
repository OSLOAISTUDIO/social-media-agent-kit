"""Criação e revisão do post: texto (modelo) → guarda de linguagem → render → checagem de layout."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import db, illustration, llm, prompts
from .config import brand, cfg
from .models import EditorialReview, PostContent
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


def clean(content: dict) -> dict:
    """Remove metadados internos (_render, _pillar) antes de mandar ao modelo."""
    return {k: v for k, v in content.items() if not k.startswith("_")}


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
    s = content["slides"]
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
    # Regra fixa: todo post tem 1 slide de ilustração/diagrama no meio do carrossel.
    ill = [i for i, sl in enumerate(s) if sl["layout"] == "illustration"]
    if len(ill) != 1:
        p.append(f"o post precisa de exatamente 1 slide 'illustration' (tem {len(ill)})")
    elif not 2 <= ill[0] <= len(s) - 3:
        p.append("o slide 'illustration' precisa ficar no meio: depois do 2º slide e antes do penúltimo")
    return p


async def _ask(prompt: str) -> dict:
    res = await llm.structured(prompts.base_instructions(), prompt, PostContent, brand_files=True,
                               effort=cfg.content_reasoning_effort)
    return res.model_dump()


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
        content = await _polish(review.content.model_dump())
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
            violations="\n".join(problems), current=json.dumps(clean(content), ensure_ascii=False)))
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
            ids=", ".join(result["overflow"]), current=json.dumps(clean(content), ensure_ascii=False)))
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
    topic_json = json.dumps(t["payload"], ensure_ascii=False, indent=1)
    content = await _ask(prompts.CONTENT.format(topic=topic_json, design=prompts.design(), site=cfg.site_url))
    content = await _polish(content)
    content = await _editorial(content, t["payload"])
    return await _save(topic_id, t["payload"]["pillar"], content, version=1)


async def revise(post_id: int, feedback: str) -> int:
    old = db.post(post_id)
    t = db.topic(old["topic_id"])
    db.update_post(post_id, status="changes", feedback=feedback)
    content = await _ask(prompts.REVISION.format(
        feedback=feedback, topic=json.dumps(t["payload"], ensure_ascii=False),
        current=json.dumps(clean(old["content"]), ensure_ascii=False)))
    content = await _polish(content)
    # Revisão editorial também na nova versão, com as orientações do humano como obrigatórias.
    content = await _editorial(content, {**t["payload"], "orientacoes_do_humano_obrigatorias": feedback}, rounds=1)
    keep_photos = "foto" not in feedback.lower() and "imagem" not in feedback.lower()
    return await _save(old["topic_id"], t["payload"]["pillar"], content, old["version"] + 1,
                       photos=old["photos"] if keep_photos else None)


async def _save(topic_id: int, pillar: str, content: dict, version: int, photos: list[str] | None = None) -> int:
    out_dir = cfg.runs_dir / f"{topic_id:04d}-{content['slug']}" / f"v{version}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if photos and len(photos) == 2 and all(Path(p).exists() for p in photos):
        pics = (Path(photos[0]), Path(photos[1]))
    else:
        pics = await _photos(content, out_dir)
    review = content.get("_review")
    content, result = await _render_checked(content, pillar, pics, out_dir)
    content["_render"] = result
    content["_pillar"] = pillar
    if review:
        content["_review"] = review
    return db.add_post(topic_id, content, str(out_dir), [str(p) for p in pics if p], version)
