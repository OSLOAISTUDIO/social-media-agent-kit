"""Cliente OpenAI (Responses API) com saída estruturada, web search e ferramentas locais."""
from __future__ import annotations

import json
import logging
from typing import TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from . import costs, knowledge
from .config import cfg

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    """Criado no primeiro uso, para o processo subir mesmo antes de a chave estar configurada."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=cfg.openai_api_key)
    return _client


async def structured(
    instructions: str,
    prompt: str,
    schema: type[T],
    *,
    web: bool = False,
    brand_files: bool = False,
    fast: bool = False,
    effort: str | None = None,
    max_tool_rounds: int = 8,
) -> T:
    """Chama o modelo e devolve `schema` preenchido.

    web:        habilita a ferramenta nativa de busca na web (pesquisa de temas e fontes)
    brand_files: habilita busca/leitura dos arquivos da base da marca
    """
    tools: list[dict] = []
    if web:
        tools.append({"type": "web_search"})
    if brand_files:
        tools += knowledge.TOOLS

    kwargs: dict = dict(
        model=cfg.model_fast if fast else cfg.model,
        instructions=instructions,
        text_format=schema,
    )
    if tools:
        kwargs["tools"] = tools
    if (effort or cfg.reasoning_effort) and not fast:
        kwargs["reasoning"] = {"effort": effort or cfg.reasoning_effort}

    def meter(r) -> None:
        costs.record(kwargs["model"], r.usage, searches=sum(1 for o in r.output if o.type == "web_search_call"))

    resp = await client().responses.parse(input=prompt, **kwargs)
    meter(resp)
    for _ in range(max_tool_rounds):
        calls = [o for o in resp.output if o.type == "function_call"]
        if not calls:
            break
        outputs = []
        for call in calls:
            handler = knowledge.HANDLERS.get(call.name)
            try:
                result = handler(json.loads(call.arguments)) if handler else "Ferramenta desconhecida."
            except Exception as e:  # a ferramenta nunca derruba a geração
                result = f"Erro: {e}"
            log.info("tool %s(%s)", call.name, call.arguments[:120])
            outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": result})
        resp = await client().responses.parse(input=outputs, previous_response_id=resp.id, **kwargs)
        meter(resp)

    if resp.output_parsed is None:
        raise RuntimeError(f"Modelo não devolveu saída estruturada: {resp.output_text[:500]}")
    return resp.output_parsed


async def generate_image(prompt: str, out_path, *, model: str | None = None, size: str = "1024x1536",
                         quality: str | None = None, reference=None) -> None:
    """Gera imagem com a Images API (fotos com PHOTO_SOURCE=generate; ilustrações com gpt-image-2).

    reference: imagem de estilo enviada junto (endpoint de edição), para o resultado seguir o mesmo
    nível visual; se a API recusar, gera só pelo texto.
    """
    import base64

    kwargs = {"quality": quality} if quality else {}
    model = model or cfg.image_model
    r = None
    if reference is not None:
        try:
            with open(reference, "rb") as ref:
                r = await client().images.edit(model=model, image=ref, prompt=prompt, size=size, **kwargs)
        except Exception as e:
            log.warning("geração com referência de estilo falhou (%s); gerando só pelo texto", e)
            r = None
    if r is None:
        r = await client().images.generate(model=model, prompt=prompt, size=size, **kwargs)
    costs.record(model, getattr(r, "usage", None), image=True)
    out_path.write_bytes(base64.b64decode(r.data[0].b64_json))
