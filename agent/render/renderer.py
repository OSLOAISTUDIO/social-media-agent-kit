"""Renderiza o conteúdo do post em PNGs (feed 1080×1350 e stories 1080×1920) via Playwright."""
from __future__ import annotations

import random
from datetime import date
from pathlib import Path

from markupsafe import Markup, escape
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

from ..config import BRAND_NAME, brand, cfg

TEMPLATES = Path(__file__).parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
# Quebra de linha intencional nos títulos ("\n" vira <br>), sem abrir mão do escape.
env.filters["br"] = lambda t: Markup("<br>").join(escape(x) for x in str(t or "").split("\n"))

PILLARS = [(p["name"], p.get("desc", "")) for p in brand.get("pillars", [])]
VISUAL = brand["visual"]
IDENTITY = brand["identity"]


def _rgb(hex_color: str) -> str:
    """'#275279' → '39,82,121' para usar em rgba() no CSS."""
    h = hex_color.lstrip("#")
    return ",".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))
MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto",
         "Setembro", "Outubro", "Novembro", "Dezembro"]


def photo_library() -> list[Path]:
    d = cfg.assets_dir / "photos"
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})


def pick_photos(avoid: list[str]) -> tuple[Path | None, Path | None]:
    """Duas fotos diferentes para capa e CTA, evitando as usadas recentemente.
    Sem fotos no pacote da marca, capa e CTA saem com fundo sólido."""
    lib = photo_library()
    if not lib:
        return None, None
    used = {Path(a).name for a in avoid}
    fresh = [p for p in lib if p.name not in used] or lib
    random.shuffle(fresh)
    cover = fresh[0]
    cta = next((p for p in fresh[1:]), next(p for p in lib if p != cover) if len(lib) > 1 else cover)
    return cover, cta


def build_html(content: dict, pillar: str, photos: tuple[Path, Path], story: bool) -> str:
    today = date.today()
    return env.get_template("post.html.j2").render(
        slides=content["slides"],
        slug=content["slug"],
        images={k: Path(v).as_uri() for k, v in (content.get("_images") or {}).items()},
        story=story,
        assets=cfg.assets_dir.as_uri(),
        photo_cover=photos[0].as_uri() if photos[0] else "",
        photo_cta=photos[1].as_uri() if photos[1] else "",
        pillar=pillar,
        pillars=PILLARS,
        kicker=f"{IDENTITY.get('kicker', 'Insights')} · {MESES[today.month - 1]} {today.year}",
        brand_name=BRAND_NAME,
        v=VISUAL,
        ink_rgb=_rgb(VISUAL["ink"]),
        paper_rgb=_rgb(VISUAL["paper"]),
        cta_button=IDENTITY["cta_button"],
        footer_tagline=IDENTITY.get("footer_tagline", ""),
        site_url=cfg.site_url,
        handle=cfg.instagram_handle,
    )


# Originais em 2× (2160 × 2700 no feed): texto pequeno nítido em qualquer rede e em zoom.
SCALE = 2

# Versão de impressão do feed: uma peça por página, sem a moldura de visualização.
PRINT_CSS = """
@page { size: 1080px 1350px; margin: 0; }
html, body { margin: 0 !important; padding: 0 !important; background: none !important; display: block !important; }
.art { break-after: page; page-break-after: always; }
"""


async def render(content: dict, pillar: str, photos: tuple[Path, Path], out_dir: Path) -> dict:
    """Gera feed/ e storie/ (PNG em 2×) e carrossel.pdf vetorial em out_dir.
    Devolve caminhos e slides que estouraram a área."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {"feed": [], "story": [], "overflow": [], "pdf": None}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1300, "height": 2100}, device_scale_factor=SCALE)
        for story in (False, True):
            name = "stories" if story else "pecas"
            folder = out_dir / ("storie" if story else "feed")
            folder.mkdir(exist_ok=True)
            html_path = out_dir / f"{name}.html"
            html_path.write_text(build_html(content, pillar, photos, story), encoding="utf-8")
            await page.goto(html_path.as_uri())
            await page.wait_for_function("window.__ready === true", timeout=20000)
            await page.wait_for_timeout(300)
            if not story:
                result["overflow"] = await page.evaluate("window.__overflow")
            ids = await page.eval_on_selector_all(".art", "els => els.map(e => e.id)")
            for i, sid in enumerate(ids, 1):
                path = folder / f"{'storie' if story else 'carrossel'}-{i:02d}.png"
                await page.locator(f"#{sid}").screenshot(path=str(path))
                result["story" if story else "feed"].append(str(path))
            if not story:
                # PDF do LinkedIn direto do layout: texto e gráficos em vetor, fotos na resolução original.
                await page.add_style_tag(content=PRINT_CSS)
                pdf = out_dir / "carrossel.pdf"
                await page.pdf(path=str(pdf), width="1080px", height="1350px", print_background=True,
                               prefer_css_page_size=True)
                result["pdf"] = str(pdf)
        await browser.close()
    return result
