"""Slide de ilustração/diagrama: gerado com ChatGPT Images 2 (gpt-image-2) no estilo da marca
(brand.toml → [illustration]: texto de estilo e imagem de referência opcional).

O modelo de texto descreve o diagrama (visual_brief) e os rótulos exatos (visual_labels);
aqui isso vira um prompt com as regras visuais da marca. Cada imagem é guardada por hash do
briefing, então re-renderizações e revisões que não mexem no diagrama reaproveitam o arquivo.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from . import llm
from .config import brand, cfg

ILL = brand.get("illustration", {})
REFERENCE = cfg.assets_dir / ILL.get("reference", "style/illustration-reference.png")

log = logging.getLogger(__name__)

SIZE = "1536x1024"  # 3:2, ocupa as 6 colunas do grid (888px)

STYLE = ILL["style"]



def _key(slide: dict) -> str:
    # "v2" = estilo isométrico com referência; muda a chave para não reaproveitar imagens do estilo antigo
    raw = "v2|" + (slide.get("visual_brief") or "") + "|" + "|".join(slide.get("visual_labels") or [])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def build_prompt(slide: dict) -> str:
    labels = [l.strip() for l in (slide.get("visual_labels") or []) if l.strip()][:6]
    label_text = (
        "Side labels to render, spelled EXACTLY as written (Brazilian Portuguese, keep accents), each used once, "
        "numbered in order as \"01 · \", \"02 · \"...: " + "; ".join(f'"{l.upper()}"' for l in labels)
        if labels else "Do not render any text at all."
    )
    return f"{STYLE}\n\nWhat the illustration shows: {slide.get('visual_brief')}\n\n{label_text}"


def neutralize_background(path: Path) -> None:
    """Leva o fundo gerado (quase branco, nunca exato) para branco puro, canal a canal.

    No slide a imagem é mesclada com multiply: branco vira exatamente a cor do artboard e não sobra
    retângulo em volta; traços, blocos e sombras suaves continuam iguais.
    """
    from PIL import Image

    img = Image.open(path).convert("RGB")
    w, h = img.size
    border = [img.getpixel((x, y)) for x in range(0, w, 8) for y in (2, h - 3)] + \
             [img.getpixel((x, y)) for y in range(0, h, 8) for x in (2, w - 3)]
    bg = [sorted(c[k] for c in border)[len(border) // 2] for k in range(3)]  # mediana da borda
    luts = [[min(255, round(v * 255 / max(b, 1))) for v in range(256)] for b in bg]
    img.point(luts[0] + luts[1] + luts[2]).save(path)


async def ensure(content: dict, cache_dir: Path) -> dict:
    """Gera (ou reaproveita) a imagem de cada slide 'illustration'. Grava em content['_images']."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    images: dict[str, str] = {}
    for i, s in enumerate(content["slides"]):
        if s["layout"] != "illustration":
            continue
        path = cache_dir / f"{_key(s)}.png"
        if not path.exists():
            try:
                log.info("gerando ilustração do slide %d com %s", i + 1, cfg.illustration_model)
                await llm.generate_image(build_prompt(s), path, model=cfg.illustration_model, size=SIZE,
                                         quality=cfg.illustration_quality or None,
                                         reference=REFERENCE if REFERENCE.exists() else None)
                neutralize_background(path)
            except Exception as e:
                # Sem imagem, o slide vira 'statement' para não travar o post; o humano vê no preview.
                log.error("ilustração falhou: %s", e)
                s["layout"] = "statement"
                s["lead"] = s.get("lead") or s.get("body") or s.get("visual_brief")
                continue
        images[str(i)] = str(path)
    content["_images"] = images
    return content
