"""Renderiza tests/sample_post.json sem chamar nenhuma API. Uso: python -m tests.render_sample"""
import asyncio
import json
from pathlib import Path

from agent.config import BRAND_SLUG, cfg
from agent.render.renderer import pick_photos, render

here = Path(__file__).parent


async def main():
    sample = here / f"sample_post_{BRAND_SLUG}.json"
    content = json.loads((sample if sample.exists() else here / "sample_post.json").read_text())
    # Diagrama de exemplo no lugar do que o gpt-image-2 geraria.
    fixture = here / "fixtures" / "diagram.png"
    content["_images"] = {str(i): str(fixture)
                          for i, s in enumerate(content["slides"]) if s["layout"] == "illustration"}
    out = cfg.runs_dir / "_sample"
    res = await render(content, content["pillar"], pick_photos([]), out)
    print("feed:", len(res["feed"]), "stories:", len(res["story"]), "overflow:", res["overflow"])
    print(out)


asyncio.run(main())
