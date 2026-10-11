"""Medidor de custo por etapa (pesquisa, criação, ajustes), em dólar, separado por agente.

Cada chamada a um modelo soma os tokens reais devolvidos pela API no medidor ativo da tarefa
(contextvar). Agentes: "openai" (pesquisa, escrita, imagens), "reviewer" (Revisor, Claude Sonnet) e
"video" (Agent Vídeo, Claude Opus). Preços por 1M de tokens na tabela abaixo: OpenAI conferida em
2026-10-07, Anthropic em 2026-10-11. A busca na web é estimada por chamada.
"""
from __future__ import annotations

import contextvars
import os

# modelo: (entrada, entrada em cache, saída) em US$ por 1M de tokens
PRICES = {
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-image-2": (8.00, 2.00, 30.00),
    "claude-sonnet-5-5": (2.00, 0.20, 10.00),
    "claude-opus-5-5": (4.00, 0.20, 20.00),
    "claude-haiku-5-5": (0.10, 0.01, 0.50),
}
WEB_SEARCH_USD = float(os.getenv("WEB_SEARCH_USD", "0.01"))  # estimativa por busca
AGENTS = {"openai": "OpenAI", "reviewer": "Revisor", "video": "Agent Vídeo"}

current: contextvars.ContextVar["Meter | None"] = contextvars.ContextVar("cost_meter", default=None)


def _price(model: str) -> tuple[float, float, float]:
    for name, p in PRICES.items():
        if model.startswith(name):
            return p
    return PRICES["gpt-6-luna"]


class Meter:
    def __init__(self) -> None:
        self.calls = 0
        self.tokens_in = self.tokens_cached = self.tokens_out = 0
        self.searches = 0
        self.images = 0
        self.usd = 0.0
        self.agents: dict[str, float] = {}

    def _add(self, agent: str, usd: float, tin: int, cached: int, tout: int, searches: int, image: bool) -> None:
        self.usd += usd
        self.agents[agent] = self.agents.get(agent, 0.0) + usd
        self.calls += 1
        self.tokens_in += tin
        self.tokens_cached += cached
        self.tokens_out += tout
        self.searches += searches
        self.images += int(image)

    def add(self, model: str, usage, searches: int = 0, image: bool = False) -> None:
        """Uso da OpenAI (Responses/Images API)."""
        if usage is None:
            return
        tin = getattr(usage, "input_tokens", 0) or 0
        tout = getattr(usage, "output_tokens", 0) or 0
        det = getattr(usage, "input_tokens_details", None)
        cached = (getattr(det, "cached_tokens", 0) or 0) if det else 0
        pin, pcache, pout = _price(model)
        usd = ((tin - cached) * pin + cached * pcache + tout * pout) / 1_000_000 + searches * WEB_SEARCH_USD
        self._add("openai", usd, tin, cached, tout, searches, image)

    def add_claude(self, model: str, usage, agent: str) -> None:
        """Uso da Anthropic: input_tokens já exclui o cache; escrita em cache custa 1,25× a entrada."""
        if usage is None:
            return
        tin = getattr(usage, "input_tokens", 0) or 0
        cread = getattr(usage, "cache_read_input_tokens", 0) or 0
        cwrite = getattr(usage, "cache_creation_input_tokens", 0) or 0
        tout = getattr(usage, "output_tokens", 0) or 0
        stu = getattr(usage, "server_tool_use", None)
        searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        pin, pcache, pout = _price(model)
        usd = (tin * pin + cwrite * pin * 1.25 + cread * pcache + tout * pout) / 1_000_000 + searches * WEB_SEARCH_USD
        self._add(agent, usd, tin + cread + cwrite, cread, tout, searches, False)

    def as_dict(self) -> dict:
        return {"usd": round(self.usd, 4), "calls": self.calls, "tokens_in": self.tokens_in,
                "tokens_cached": self.tokens_cached, "tokens_out": self.tokens_out,
                "searches": self.searches, "images": self.images,
                "agents": {k: round(v, 4) for k, v in self.agents.items()}}


def record(model: str, usage, searches: int = 0, image: bool = False) -> None:
    m = current.get()
    if m is not None:
        m.add(model, usage, searches, image)


def record_claude(model: str, usage, agent: str) -> None:
    m = current.get()
    if m is not None:
        m.add_claude(model, usage, agent)


def usd(v: float) -> str:
    return f"US$ {v:,.3f}".replace(",", "X").replace(".", ",").replace("X", ".")


def split_line(split: dict[str, float]) -> str:
    """'OpenAI US$ 0,041 · Revisor US$ 0,032 · <b>total US$ 0,073</b>' (agentes sem gasto ficam de fora)."""
    parts = [f"{AGENTS.get(k, k)} {usd(v)}" for k, v in split.items() if v > 0 and k in AGENTS]
    return " · ".join(parts + [f"<b>total {usd(sum(split.values()))}</b>"])
