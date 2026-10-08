"""Medidor de custo da OpenAI por etapa (pesquisa, criação, ajustes), em dólar.

Cada chamada ao modelo soma os tokens reais devolvidos pela API (resp.usage) no medidor ativo da
tarefa (contextvar). Preços por 1M de tokens na tabela abaixo, conferidos na página de preços da
OpenAI em 2026-10-07; a busca na web é estimada por chamada (WEB_SEARCH_USD).
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
}
WEB_SEARCH_USD = float(os.getenv("WEB_SEARCH_USD", "0.01"))  # estimativa por busca

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

    def add(self, model: str, usage, searches: int = 0, image: bool = False) -> None:
        if usage is None:
            return
        tin = getattr(usage, "input_tokens", 0) or 0
        tout = getattr(usage, "output_tokens", 0) or 0
        det = getattr(usage, "input_tokens_details", None)
        cached = (getattr(det, "cached_tokens", 0) or 0) if det else 0
        pin, pcache, pout = _price(model)
        self.usd += ((tin - cached) * pin + cached * pcache + tout * pout) / 1_000_000 + searches * WEB_SEARCH_USD
        self.calls += 1
        self.tokens_in += tin
        self.tokens_cached += cached
        self.tokens_out += tout
        self.searches += searches
        self.images += int(image)

    def as_dict(self) -> dict:
        return {"usd": round(self.usd, 4), "calls": self.calls, "tokens_in": self.tokens_in,
                "tokens_cached": self.tokens_cached, "tokens_out": self.tokens_out,
                "searches": self.searches, "images": self.images}


def record(model: str, usage, searches: int = 0, image: bool = False) -> None:
    m = current.get()
    if m is not None:
        m.add(model, usage, searches, image)


def usd(v: float) -> str:
    return f"US$ {v:,.3f}".replace(",", "X").replace(".", ",").replace("X", ".")
