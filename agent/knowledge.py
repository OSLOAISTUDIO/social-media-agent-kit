"""Base de conhecimento da marca ativa (brands/<slug>/brand.toml → [knowledge]): documentos
principais sempre no contexto, e busca/leitura de qualquer arquivo de texto da pasta como
ferramenta do modelo."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .config import BRAND_NAME, brand, cfg

K = brand["knowledge"]
# Documentos da marca carregados em todo prompt de criação.
CORE_FILES: list[str] = K.get("core", [])
DESIGN_FILE: str | None = K.get("design")

TEXT_EXT = {".md", ".txt", ".html", ".csv", ".json"}
SKIP_DIRS = {"node_modules", ".git", ".output", ".wrangler", ".tanstack", ".lovable", "__pycache__", "_erp", "dist", "build"}
# Material confidencial de clientes (NDAs, propostas, reuniões) nunca entra em post público.
# Cases publicados no site continuam acessíveis via _site/.
SKIP_DIRS |= set(K.get("skip_dirs", []))
SKIP_NAMES = re.compile(r"^\.env|secret|credential|token", re.I)
MAX_BYTES = 2_000_000


@lru_cache
def core_knowledge() -> str:
    parts = []
    for rel in CORE_FILES:
        p = cfg.knowledge_root / rel
        if p.exists():
            parts.append(f"\n\n<arquivo path=\"{rel}\">\n{p.read_text(encoding='utf-8')}\n</arquivo>")
    return "".join(parts)


@lru_cache
def design_rules() -> str:
    """Trecho do design system usado nos prompts (o resto já está no template)."""
    if not DESIGN_FILE or not (p := cfg.knowledge_root / DESIGN_FILE).exists():
        return ""
    t = p.read_text(encoding="utf-8")
    section = K.get("design_section") or []
    start = section[0] if section else None
    end = section[1] if len(section) > 1 else None
    if not start:
        return t
    i = t.find(start)
    j = t.find(end) if end else -1
    return t[i:j if j > i else None] if i >= 0 else ""


def _iter_files():
    root = cfg.knowledge_root
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.is_file() and p.suffix.lower() in TEXT_EXT and not SKIP_NAMES.search(p.name):
            try:
                if p.stat().st_size <= MAX_BYTES:
                    yield p
            except OSError:
                continue


def _strip_html(t: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t)


def search_files(query: str, limit: int = 8) -> str:
    """Busca por palavras em todos os arquivos de texto da base da marca."""
    terms = [w.lower() for w in re.findall(r"\w{3,}", query)]
    if not terms:
        return "Consulta vazia."
    scored = []
    for p in _iter_files():
        rel = str(p.relative_to(cfg.knowledge_root))
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if p.suffix == ".html":
            text = _strip_html(text)
        low = text.lower()
        score = sum(low.count(t) for t in terms) + 5 * sum(t in rel.lower() for t in terms)
        if score:
            i = min((low.find(t) for t in terms if t in low), default=0)
            snippet = text[max(0, i - 160): i + 240].replace("\n", " ")
            scored.append((score, rel, snippet))
    scored.sort(reverse=True)
    if not scored:
        return "Nenhum arquivo encontrado."
    return "\n".join(f"- {rel} (relevância {s}): …{snip}…" for s, rel, snip in scored[:limit])


def read_file(path: str, max_chars: int = 20000) -> str:
    p = (cfg.knowledge_root / path).resolve()
    if cfg.knowledge_root.resolve() not in p.parents or not p.is_file():
        return "Arquivo não encontrado na base da marca."
    if SKIP_NAMES.search(p.name) or p.suffix.lower() not in TEXT_EXT:
        return "Tipo de arquivo não permitido."
    text = p.read_text(encoding="utf-8", errors="ignore")
    if p.suffix == ".html":
        text = _strip_html(text)
    return text[:max_chars] + ("\n[…truncado]" if len(text) > max_chars else "")


# Definições no formato de function tools da Responses API.
TOOLS = [
    {
        "type": "function",
        "name": "search_brand_files",
        "description": f"Busca nos arquivos da base da {BRAND_NAME} ({K.get('files_description', 'documentos da marca')}). "
                       f"Use para trazer dados reais da {BRAND_NAME} ao post.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Palavras-chave"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "read_brand_file",
        "description": f"Lê um arquivo da base da {BRAND_NAME} pelo caminho relativo devolvido em search_brand_files.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

HANDLERS = {
    "search_brand_files": lambda a: search_files(a["query"]),
    "read_brand_file": lambda a: read_file(a["path"]),
}
