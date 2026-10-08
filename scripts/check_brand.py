"""Confere um pacote de marca e lista o que falta para ele ficar pronto.
Uso: python -m scripts.check_brand <slug>     (sem argumento: confere todas as marcas)
Não chama nenhuma API; só lê brands/<slug>/.
"""
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "brands"
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
REQUIRED = {
    "identity": ["name", "category", "city", "site", "instagram", "kicker", "footer_tagline", "cta_button", "editorial_standard"],
    "voice": ["language_label", "audience", "tone", "cover_example"],
    "research": ["subject", "priorities"],
    "visual": ["paper", "ink", "accent", "surface", "font_family", "logo_light", "logo_dark"],
    "illustration": ["style"],
    "knowledge": ["core"],
}


def check(slug: str) -> tuple[list[str], list[str]]:
    d = ROOT / slug
    errors, warnings = [], []
    try:
        b = tomllib.loads((d / "brand.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"brand.toml não existe em {d}"], []
    except tomllib.TOMLDecodeError as e:
        return [f"brand.toml inválido: {e}"], []

    for section, keys in REQUIRED.items():
        for k in keys:
            v = str(b.get(section, {}).get(k, "")).strip()
            if not v:
                errors.append(f"[{section}] {k} vazio")
            elif v.startswith("PENDENTE"):
                errors.append(f"[{section}] {k} ainda não preenchido")
    pillars = b.get("pillars", [])
    if not 2 <= len(pillars) <= 4:
        errors.append(f"pillars: precisa de 2 a 4 (tem {len(pillars)})")
    if any(str(p.get("name", "")).startswith("PENDENTE") for p in pillars):
        errors.append("pillars: nomes ainda não preenchidos")
    for k in ("paper", "ink", "accent", "surface"):
        v = b.get("visual", {}).get(k, "")
        if v and not HEX.match(v):
            errors.append(f"[visual] {k} deve ser cor #RRGGBB (está '{v}')")

    assets = d / "assets"
    vis = b.get("visual", {})
    if not vis.get("font_regular"):
        warnings.append("sem arquivo de fonte da marca: as peças usam Inter (ou a fonte do sistema)")
    for k in ("font_regular", "font_medium", "logo_light", "logo_dark"):
        if vis.get(k) and not (assets / vis[k]).exists():
            (errors if k in ("font_regular", "logo_light", "logo_dark") else warnings).append(
                f"arquivo {k} não encontrado: assets/{vis[k]}")
    photos = [p for p in (assets / "photos").glob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    if len(photos) < 2:
        warnings.append(f"assets/photos tem {len(photos)} foto(s): capa e CTA saem com fundo sólido (ideal: 6+, ≥ 2700 px de altura)")
    ref = b.get("illustration", {}).get("reference")
    if ref and not (assets / ref).exists():
        warnings.append(f"sem imagem de referência de ilustração (assets/{ref}): gera só pelo texto de estilo")

    kroot = d / b.get("knowledge", {}).get("root", "knowledge")
    for f in b.get("knowledge", {}).get("core", []):
        if not (kroot / f).exists():
            errors.append(f"documento da base não encontrado: {kroot.name}/{f}")
    for f, what in (("apify_sources.json", "fontes do Apify"), ("reference_sources.json", "referências editoriais")):
        if not (d / f).exists():
            warnings.append(f"{f} ausente: pesquisa sem {what}")
        elif f == "reference_sources.json":
            r = json.loads((d / f).read_text())
            if not (r.get("youtube") or r.get("instagram")):
                warnings.append("nenhuma referência editorial cadastrada (reference_sources.json)")

    text = (d / "brand.toml").read_text(encoding="utf-8")
    pend = [l.strip() for l in text.splitlines() if "PENDENTE" in l and not l.strip().startswith("#")]
    warnings += [f"pendente no brand.toml: {l}" for l in pend]
    return errors, warnings


def main():
    slugs = sys.argv[1:] or sorted(p.name for p in ROOT.iterdir()
                                   if (p / "brand.toml").exists() and not p.name.startswith("_"))
    bad = False
    for slug in slugs:
        errors, warnings = check(slug)
        status = "PRONTA" if not errors else "INCOMPLETA"
        print(f"\n== {slug}: {status} ({len(errors)} erro(s), {len(warnings)} aviso(s))")
        for e in errors:
            print("  ✗", e)
        for w in warnings:
            print("  !", w)
        bad |= bool(errors)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
