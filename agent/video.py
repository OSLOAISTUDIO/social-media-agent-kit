"""Agent Vídeo (Claude Opus): decide se o carrossel ganha um slide animado e, se sim, escreve a animação.

Fluxo:
  plan()    o Opus lê o post e decide (VIDEO_MODE=auto) se um vídeo explica melhor algum ponto
            (fluxo, processo, antes e depois, números crescendo, linha do tempo). Se sim, devolve a
            animação em HTML + CSS (só @keyframes, sem JavaScript), nas cores e fontes da marca.
  insert()  vira um slide "motion" logo depois da ilustração; o template desenha cabeçalho, título,
            numeração e rodapé como nos outros slides, e o PNG estático é o quadro final (pôster).
  record()  grava o slide quadro a quadro (Playwright, animações pausadas e posicionadas no tempo)
            e codifica em MP4 H.264 com o ffmpeg do pacote imageio-ffmpeg.
No Instagram o slide sai como vídeo dentro do carrossel; LinkedIn (PDF) e stories usam o pôster.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from . import claude, prompts
from .config import BRAND_NAME, cfg

log = logging.getLogger(__name__)
AGENT = "video"
FPS = 30
BOX = (888, 544)  # área da animação no feed (no stories ela ganha mais altura: use tamanhos relativos)


def active() -> bool:
    return cfg.video_mode != "off" and claude.enabled()


class VideoPlan(BaseModel):
    needed: bool = Field(description="true só se uma animação explica algo do post melhor do que imagem parada")
    reason: str = Field(description="Por que o vídeo ajuda (ou por que não precisa), em uma frase")
    eyebrow: str = Field(description="Rótulo curto do slide, ex.: 'Como funciona' (vazio se needed=false)")
    title: str = Field(description="Título H2 do slide animado, até ~55 caracteres (vazio se needed=false)")
    body: str = Field(description="Legenda curta abaixo da animação, até ~120 caracteres (pode ser vazia)")
    html: str = Field(description="Fragmento HTML com <style> e marcação da animação (vazio se needed=false)")


PLAN = """Você é o diretor de motion design da {brand}. Abaixo está um carrossel pronto para Instagram.
Decida se UM slide animado (vídeo curto, em loop) deixaria o conteúdo mais claro e mais visual.

{mode_rule}
Vale a pena quando há algo que se entende melhor em movimento: um fluxo ou processo em etapas, um
antes e depois, números ou barras crescendo, uma linha do tempo, um mecanismo (como X conversa com Y).
Não vale para opinião, lista simples ou quando a ilustração estática já resolve.

Se needed=true, escreva a animação (campo html):
- Um fragmento HTML com um bloco <style> e a marcação. Sem <script>, sem imagens externas, sem
  links, sem fontes externas. Pode usar SVG inline e divs.
- Ele é inserido dentro de uma caixa de {w}×{h}px (no stories a caixa fica mais alta): use
  width/height 100% e posições relativas/percentuais, centralizado, sem nada saindo da caixa.
- Animação só com CSS @keyframes (animation-fill-mode: both). Duração total {secs}s: os elementos
  entram em sequência e o último quadro (aos {secs}s) precisa ser a imagem completa e legível, porque
  ele vira o pôster do vídeo. Pode usar animation-iteration-count: infinite com duração que divida {secs}s.
- Visual da marca: fundo transparente (a peça já tem a cor de fundo), cores SÓ pelas variáveis CSS
  var(--ink) (texto/linhas), var(--azure) (acento, destaque), var(--surface) (blocos neutros),
  var(--line) (linhas finas) e var(--serene) (cor do fundo); fonte herdada (font-family: inherit).
  Estilo: {style}
- Texto dentro da animação: até 6 rótulos curtos em português (no máximo 4 palavras cada), tamanho
  mínimo 20px, em linha com os dados do post (não invente número).
- Prefixe todas as classes CSS com "mv-" para não colidir com o template.
- Títulos do slide (eyebrow/title/body) no tom da marca; o slide entra logo depois da ilustração.

# Tema
{topic}

# Carrossel (JSON)
{post}"""

STYLE = ("minimalista e editorial: linhas finas, blocos retos (sem cantos arredondados, sem sombra), "
         "movimentos suaves (ease-out), nada girando ou quicando, alinhado a um grid.")


def _system() -> str:
    return f"Você cria animações para os carrosséis da {BRAND_NAME}.\n\n{prompts.base_instructions()}"


async def plan(content: dict, topic: dict, feedback: str | None = None) -> VideoPlan | None:
    if not active():
        return None
    mode_rule = ("Neste post o vídeo é obrigatório (needed=true)." if cfg.video_mode == "always" else
                 "Seja seletivo: a maioria dos posts NÃO precisa de vídeo.")
    post = {k: v for k, v in strip(content).items() if not k.startswith("_")}
    if feedback:  # pedido do humano sobre o vídeo vale mais que o critério padrão (inclusive para tirar)
        mode_rule += f"\nOrientação do humano para esta versão (obrigatória): {feedback}"
    prompt = PLAN.format(brand=BRAND_NAME, mode_rule=mode_rule, w=BOX[0], h=BOX[1], secs=cfg.video_seconds,
                         style=prompts.C.get("motion_style", STYLE) if hasattr(prompts, "C") else STYLE,
                         topic=json.dumps({k: v for k, v in topic.items() if not k.startswith("_")},
                                          ensure_ascii=False, indent=1),
                         post=json.dumps(post, ensure_ascii=False, indent=1))
    try:
        vp = await claude.structured(_system(), prompt, VideoPlan, model=cfg.video_model, agent=AGENT,
                                     effort=cfg.video_effort, max_tokens=32000)
    except claude.Unavailable as ex:
        log.warning("agent vídeo indisponível: %s", ex)
        return None
    log.info("agent vídeo: needed=%s · %s", vp.needed, vp.reason)
    if vp.needed and cfg.video_mode != "always" and not vp.html.strip():
        return None
    return vp


_BAD = [re.compile(p, re.I | re.S) for p in (
    r"<script\b.*?</script\s*>", r"<script\b[^>]*/?>", r"<iframe\b.*?</iframe\s*>", r"<link\b[^>]*>",
    r"<img\b[^>]*>", r"<object\b.*?</object\s*>", r"<embed\b[^>]*>", r"@import[^;]*;",
    r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", r"url\(\s*['\"]?(https?:|//)[^)]*\)")]


def sanitize(html: str) -> str:
    """Só HTML/CSS/SVG local: sem script, sem recursos externos, sem handlers de evento."""
    for rx in _BAD:
        html = rx.sub("", html)
    return html.strip()


def insert(content: dict, vp: VideoPlan) -> dict:
    """Coloca o slide animado logo depois da ilustração (ou antes do CTA, se não houver ilustração)."""
    slides = [s for s in content["slides"] if s.get("layout") != "motion"]
    ill = next((i for i, s in enumerate(slides) if s.get("layout") == "illustration"), len(slides) - 2)
    slide = {"layout": "motion", "eyebrow": vp.eyebrow or None, "title": vp.title, "lead": None,
             "body": vp.body or None, "source": None, "motion_html": sanitize(vp.html),
             "motion_seconds": cfg.video_seconds}
    slides.insert(min(ill + 1, len(slides) - 1), slide)
    content["slides"] = slides
    content["_video"] = {"needed": True, "reason": vp.reason, "seconds": cfg.video_seconds, "model": cfg.video_model}
    return content


def strip(content: dict) -> dict:
    """Post sem o slide animado (para o modelo de texto, que não conhece esse layout)."""
    return {**content, "slides": [s for s in content.get("slides", []) if s.get("layout") != "motion"]}


def carry(new: dict, old: dict) -> dict:
    """Depois de uma reescrita de texto, devolve o slide animado da versão anterior ao lugar."""
    motion = next((s for s in old.get("slides", []) if s.get("layout") == "motion"), None)
    if not motion or any(s.get("layout") == "motion" for s in new.get("slides", [])):
        return new
    slides = list(new["slides"])
    ill = next((i for i, s in enumerate(slides) if s.get("layout") == "illustration"), len(slides) - 2)
    slides.insert(min(ill + 1, len(slides) - 1), motion)
    new["slides"] = slides
    if old.get("_video"):
        new["_video"] = old["_video"]
    return new


def motion_index(content: dict) -> int | None:
    return next((i for i, s in enumerate(content.get("slides", [])) if s.get("layout") == "motion"), None)


def _ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


async def record(html_path: Path, slide_id: str, out: Path, seconds: int) -> Path | None:
    """Grava o slide `slide_id` de `html_path` em MP4 (1080 × 1350, H.264, FPS quadros por segundo)."""
    from playwright.async_api import async_playwright

    ff = _ffmpeg()
    if not ff:
        log.warning("ffmpeg não encontrado: o slide animado sai só como imagem")
        return None
    frames = out.parent / f"{out.stem}-frames"
    shutil.rmtree(frames, ignore_errors=True)
    frames.mkdir(parents=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1300, "height": 2100}, device_scale_factor=1)
        await page.goto(html_path.as_uri())
        await page.wait_for_function("window.__ready === true", timeout=20000)
        el = page.locator(f"#{slide_id}")
        n = await page.evaluate(f"document.getElementById('{slide_id}').getAnimations({{subtree: true}}).length")
        if not n:
            await browser.close()
            log.warning("slide animado sem animação CSS: sai só como imagem")
            return None
        total = seconds * FPS
        for k in range(total + 1):
            ms = min(k * 1000 / FPS, seconds * 1000 - 1)
            await page.evaluate("([id, ms]) => document.getElementById(id).getAnimations({subtree: true})"
                                ".forEach(a => { a.pause(); a.currentTime = ms; })", [slide_id, ms])
            await el.screenshot(path=str(frames / f"f{k:04d}.png"))
        await browser.close()
    cmd = [ff, "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(frames / "f%04d.png"),
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium",
           # BT.709 explícito: sem isso o fundo do vídeo sai um tom diferente do da peça estática
           "-vf", "scale=1080:1350:flags=lanczos:out_color_matrix=bt709:out_range=tv",
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
           "-movflags", "+faststart", str(out)]
    proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
    shutil.rmtree(frames, ignore_errors=True)
    if proc.returncode != 0 or not out.exists():
        log.warning("ffmpeg falhou: %s", proc.stderr[-400:])
        return None
    return out
