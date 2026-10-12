"""Cliente Claude (Anthropic) do Revisor e do Agent Vídeo: saída estruturada (pydantic), leitura de
páginas (web_fetch), custo por agente e fallback do servidor em caso de recusa.

Sem ANTHROPIC_API_KEY, `enabled()` é falso e os dois agentes ficam desligados sem derrubar o fluxo.
Sem crédito na conta (ou qualquer falha da API), o post segue sem eles e a prévia no Telegram avisa
(`problem()`): o processo nunca para por causa da Anthropic.
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from . import costs
from .config import cfg

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

# Recusa de um classificador de segurança: o servidor refaz no modelo recomendado (Claude API).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_FETCH = {"type": "web_fetch_20260209", "name": "web_fetch"}

_client: anthropic.AsyncAnthropic | None = None


class Unavailable(RuntimeError):
    """Claude não respondeu com o formato pedido (recusa, limite, rede). Quem chama segue sem o agente."""


# Último problema da API (para o aviso no Telegram). Some na primeira chamada que der certo.
_problem: dict = {"kind": None, "message": "", "at": 0.0}
CREDIT_HINTS = ("credit balance", "billing", "purchase credits")


def _set_problem(kind: str | None, message: str = "") -> None:
    _problem.update(kind=kind, message=message, at=time.time() if kind else 0.0)


def problem(max_age_s: int = 6 * 3600) -> dict | None:
    """{'kind': 'credit' | 'error', 'message'} se a API falhou recentemente; None se está tudo certo."""
    if _problem["kind"] and time.time() - _problem["at"] <= max_age_s:
        return dict(_problem)
    return None


def notice() -> str:
    """Aviso curto para o Telegram quando os agentes Claude não rodaram."""
    p = problem()
    if not p:
        return ""
    if p["kind"] == "credit":
        return ("⚠️ API Anthropic sem crédito: Revisor e Agent Vídeo não rodaram (o post seguiu normalmente, "
                "sem eles). Recarregue em platform.claude.com > Plans & Billing.")
    return ("⚠️ API Anthropic indisponível agora: Revisor e Agent Vídeo não rodaram (o post seguiu normalmente, "
            f"sem eles). Detalhe: {p['message'][:160]}")


def enabled() -> bool:
    return bool(cfg.anthropic_api_key)


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key, timeout=600, max_retries=3)
    return _client


def image_block(path: Path, width: int = 540) -> dict:
    """Peça renderizada como imagem para o Claude ver (JPEG reduzido: barato e suficiente para revisar)."""
    from io import BytesIO

    from PIL import Image

    img = Image.open(path).convert("RGB")
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


async def structured(system: str, content: str | list, schema: type[T], *, model: str, agent: str,
                     effort: str = "medium", fetch: int = 0, max_tokens: int = 16000) -> T:
    """Chama o Claude e devolve `schema` preenchido.

    content: texto ou lista de blocos (texto + imagens).
    fetch:   quantas páginas o modelo pode abrir com web_fetch (só URLs que já estão no prompt).
    """
    messages: list[dict] = [{"role": "user", "content": content}]
    kwargs: dict = dict(
        model=model, max_tokens=max_tokens, system=system, output_format=schema,
        output_config={"effort": effort},
        cache_control={"type": "ephemeral"},  # sistema + base da marca se repetem entre chamadas
        betas=[FALLBACK_BETA], fallbacks="default",
    )
    if fetch:
        kwargs["tools"] = [{**WEB_FETCH, "max_uses": fetch}]
    try:
        for _ in range(4):  # pause_turn: o servidor pausou um turno longo com ferramenta; continua
            if max_tokens > 16000:  # respostas longas (HTML do vídeo) vão por streaming
                async with client().beta.messages.stream(messages=messages, **kwargs) as stream:
                    resp = await stream.get_final_message()
            else:
                resp = await client().beta.messages.parse(messages=messages, **kwargs)
            costs.record_claude(resp.model or model, resp.usage, agent)
            if resp.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": resp.content})
    except anthropic.APIStatusError as ex:
        msg = str(getattr(ex, "message", ex))
        credit = any(h in msg.lower() for h in CREDIT_HINTS)
        _set_problem("credit" if credit else "error", msg)
        raise Unavailable(f"{model}: {'sem crédito na conta Anthropic' if credit else f'{ex.status_code} {msg}'}") from ex
    except anthropic.APIConnectionError as ex:
        _set_problem("error", f"sem conexão ({ex})")
        raise Unavailable(f"{model}: sem conexão ({ex})") from ex
    except Exception as ex:  # qualquer outra falha do SDK também não pode parar o post
        _set_problem("error", f"{type(ex).__name__}: {ex}")
        raise Unavailable(f"{model}: {type(ex).__name__}: {ex}") from ex
    _set_problem(None)

    if resp.stop_reason == "refusal":
        cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
        raise Unavailable(f"{model} recusou ({cat or 'sem categoria'})")
    if resp.stop_reason == "max_tokens":
        raise Unavailable(f"{model}: resposta cortada no limite de tokens")
    parsed = getattr(resp, "parsed_output", None)
    if parsed is None:
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            parsed = schema.model_validate_json(text)
        except Exception as ex:
            raise Unavailable(f"{model}: saída fora do formato ({ex})") from ex
    return parsed
