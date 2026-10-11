"""Agent Revisor (Claude Sonnet): confere cada etapa antes de ela chegar ao humano.

Etapas (em todas, no máximo 1 pedido de alteração; depois do ajuste segue para o humano):
  0. optimize_brief  melhora o pedido/direcionamento antes da pesquisa (otimizador de input)
  1. screen_topics   classifica os temas pesquisados: dentro do tema-macro? o que a pesquisa diz
                     está mesmo nas fontes? aprova, pede 1 ajuste ou descarta
  2. review_plan     revisa a ideia de como montar o post (ângulo, roteiro, narrativa)
  3. review_post     revisa o post pronto (texto + peças renderizadas)
  +  review_text     legendas novas e tweets

Se o Claude não estiver configurado ou falhar, o fluxo segue sem o revisor (nunca trava o post).
"""
from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, Field

from . import claude, llm, prompts
from .config import BRAND_NAME, MACRO_THEME, cfg
from .models import ResearchResult, TopicProposal

log = logging.getLogger(__name__)
AGENT = "reviewer"


def active() -> bool:
    return cfg.reviewer == "on" and claude.enabled()


def label() -> str:
    return f"Revisor ({cfg.reviewer_model.replace('claude-', 'Claude ').replace('-5-5', ' 5.5').replace('sonnet', 'Sonnet').replace('opus', 'Opus')})"


# ---------------- esquemas ----------------

class BriefOut(BaseModel):
    brief: str = Field(description="Pedido otimizado para a pesquisa, fiel à intenção do humano")
    note: str = Field(description="O que foi melhorado, em uma frase curta")


class CandidateCheck(BaseModel):
    index: int = Field(description="Posição do candidato na lista (1 = primeiro)")
    theme_fit: int = Field(description="0-10: quanto o tema está dentro do tema-macro da marca")
    sources_ok: bool = Field(description="true se os dados e fatos do candidato estão de fato nas fontes citadas")
    verdict: Literal["aprovar", "ajustar", "descartar"]
    issues: list[str] = Field(description="Problemas concretos (vazio se aprovar)")
    change_request: str = Field(description="Pedido único e objetivo de alteração (vazio se aprovar ou descartar)")


class TopicsCheck(BaseModel):
    checks: list[CandidateCheck]
    summary: str = Field(description="Leitura geral do lote em uma frase")


class StageCheck(BaseModel):
    verdict: Literal["aprovar", "ajustar"]
    issues: list[str] = Field(description="Problemas concretos (vazio se aprovar)")
    change_request: str = Field(description="Pedido único de alteração, completo e acionável (vazio se aprovar)")


class Score(BaseModel):
    criterion: str
    score: int = Field(description="0-10")


class PostCheck(BaseModel):
    verdict: Literal["aprovar", "ajustar"]
    scores: list[Score]
    issues: list[str] = Field(description="Problemas concretos, slide a slide (vazio se aprovar)")
    change_request: str = Field(description="Pedido único de alteração, completo e acionável (vazio se aprovar)")


# ---------------- prompts ----------------

def _system() -> str:
    return f"""Você é o Revisor da {BRAND_NAME}: o último filtro antes de qualquer conteúdo chegar ao humano
que aprova. Tema-macro da marca: {MACRO_THEME}. Você é exigente e objetivo: aprova o que está bom,
pede alteração só quando muda o resultado, e descarta o que não serve.

Regras do revisor:
- Você só tem UM pedido de alteração por etapa. Junte tudo o que precisa mudar num pedido único,
  claro e acionável (o que mudar, onde e como), porque não haverá segunda rodada.
- Não reprove por gosto pessoal; reprove por erro, imprecisão, fuga do tema, falta de clareza,
  fuga do tom da marca ou risco para a reputação.
- Precisão acima de tudo: dado que não está na fonte é erro grave.
- Responda em português do Brasil.

# Marca, tom e regras (referência para julgar)
{prompts.base_instructions()}"""


OPTIMIZE = """Etapa: otimizar o input antes da pesquisa.

O humano mandou este {kind}:
"{text}"

Reescreva como um pedido de pesquisa mais preciso: mantenha 100% da intenção do humano, deixe
explícito o recorte dentro do tema-macro ({theme}), o público e o tipo de notícia que interessa
(o que é novo, verificável e útil). Não invente assunto novo e não amplie o escopo. Se o pedido
já estiver ótimo, devolva igual. Até 3 frases."""

TOPICS = """Etapa: classificar os temas que a pesquisa trouxe, antes de irem para o humano.

Para cada candidato, confira:
1. Tema-macro: está dentro de "{theme}" e faz sentido para o público da marca? (theme_fit 0-10;
   abaixo de 6 = descartar)
2. Fidelidade às fontes: os data_points, why_now e a sinopse dizem o que as fontes dizem? Números,
   datas e nomes batem? {fetch_rule}
3. Qualidade do ângulo: é notícia de verdade (novo, relevante, com consequência prática) e não
   repetição genérica?
Veredito: "aprovar" (pronto), "ajustar" (bom tema com problema corrigível: escreva o pedido único)
ou "descartar" (fora do tema, sem sustentação nas fontes ou fraco).

{refs_block}# Candidatos
{candidates}"""

TOPIC_FIX = """O Revisor pediu um ajuste neste tema antes de ele ir para aprovação.

# Pedido do Revisor (obrigatório)
{change_request}

# Problemas apontados
{issues}

# Tema atual (JSON)
{candidate}

Reescreva o candidato corrigindo o que foi pedido. Mantenha o assunto, a estrutura e os campos.
Todo dado precisa estar na fonte citada: se não for possível sustentar um dado, retire-o.
{refs_rule}"""

PLAN = """Etapa: revisar a ideia de como o post vai ser montado, antes da escrita.

Confira: o ângulo e a capa prometida prendem o público da marca; o roteiro tem progressão lógica
(capa, desenvolvimento com 1 ilustração no meio, CTA), uma ideia por slide, sem repetição; a
narrativa está no tom da marca; os dados do roteiro existem nas fontes do tema; o tema segue
dentro de "{theme}". Se algo precisar mudar, o pedido único vira orientação obrigatória para
quem escreve o post.

# Tema aprovado pelo humano (JSON)
{topic}"""

POST = """Etapa: revisar o post pronto (texto e peças renderizadas em anexo), antes da aprovação humana.

Notas de 0 a 10 para: Capa (gancho e benefício em 2 segundos), Clareza e progressão, Utilidade
prática, Precisão (todo número e fato bate com as fontes do tema), Tom e linguagem da marca,
Visual (legibilidade, hierarquia, nada cortado ou apertado nas imagens), Legendas (abertura forte,
chamada clara, sem citar fontes).
Veredito "ajustar" se alguma nota ficar abaixo de 8 ou houver erro de precisão; nesse caso escreva o
pedido único com tudo o que precisa mudar (slide a slide).

# Tema (com fontes)
{topic}

# Post (JSON)
{post}"""

TEXT = """Etapa: revisar {what} antes de ir para o humano.

Confira: segue o tom e as regras da marca; está dentro de "{theme}"; nenhum fato fora das fontes;
{extra}

# Contexto
{context}

# Texto
{text}"""


# ---------------- etapas ----------------

async def _call(prompt, schema, *, fetch: int = 0, effort: str | None = None):
    try:
        return await claude.structured(_system(), prompt, schema, model=cfg.reviewer_model, agent=AGENT,
                                       effort=effort or cfg.reviewer_effort, fetch=fetch)
    except claude.Unavailable as ex:
        log.warning("revisor indisponível: %s", ex)
        return None


async def optimize_brief(text: str | None, kind: str) -> str | None:
    """Etapa 0. Devolve o pedido otimizado (ou o original, se o revisor estiver desligado)."""
    if not text or not active():
        return text
    out = await _call(OPTIMIZE.format(kind=kind, text=text, theme=MACRO_THEME), BriefOut, effort="low")
    if not out or not out.brief.strip():
        return text
    log.info("revisor otimizou o pedido: %s", out.note)
    return out.brief.strip()


def _public(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if not k.startswith("_")}


async def screen_topics(res: ResearchResult, refs: str | None = None) -> tuple[ResearchResult, list[dict | None]]:
    """Etapa 1. Classifica, ajusta (1 vez) e reordena os candidatos; descartados vão para o fim.
    Devolve também a nota do revisor de cada candidato, na mesma ordem (None sem revisor)."""
    if not active() or not res.candidates:
        return res, [None] * len(res.candidates)
    cands = [_public(c.model_dump()) for c in res.candidates]
    refs_block = f"# Referências enviadas pelo humano (única base válida)\n{refs[:12000]}\n\n" if refs else ""
    fetch_rule = ("Use só as referências abaixo para conferir." if refs else
                  "Abra com web_fetch as fontes mais importantes (os links estão nos candidatos) para conferir.")
    check = await _call(TOPICS.format(theme=MACRO_THEME, fetch_rule=fetch_rule, refs_block=refs_block,
                                      candidates=json.dumps(cands, ensure_ascii=False, indent=1)),
                        TopicsCheck, fetch=0 if refs else min(2 * len(cands), 8))
    if not check:
        return res, [None] * len(res.candidates)
    by_idx = {c.index: c for c in check.checks}
    kept, dropped = [], []
    for i, cand in enumerate(res.candidates, 1):
        c = by_idx.get(i)
        if not c:
            kept.append((5, cand, None))
            continue
        note = {"verdict": c.verdict, "theme_fit": c.theme_fit, "sources_ok": c.sources_ok,
                "issues": c.issues[:4], "applied": False}
        if c.verdict == "ajustar" and c.change_request:
            fixed = await _fix_topic(cand, c, refs)
            if fixed:
                cand, note["applied"] = fixed, True
        (dropped if c.verdict == "descartar" else kept).append((c.theme_fit, cand, note))
    # aprovados/ajustados primeiro (mais alinhados ao tema na frente); descartados só se faltar opção
    kept.sort(key=lambda x: -x[0])
    ordered = kept + dropped
    res.candidates = [c for _, c, _ in ordered]
    log.info("revisor: %s", check.summary)
    return res, [n for _, _, n in ordered]


def topic_line(note: dict | None) -> str:
    """Linha do revisor no card do tema."""
    if not note:
        return ""
    fit = f"tema-macro {note['theme_fit']}/10"
    src = "fontes conferidas" if note.get("sources_ok") else "fontes com ressalva"
    if note["verdict"] == "aprovar":
        return f"✓ {label()}: aprovado · {fit} · {src}"
    if note["verdict"] == "ajustar":
        return f"✓ {label()}: ajustado ({'; '.join(note['issues'][:2])}) · {fit}" if note.get("applied") else \
            f"⚠️ {label()}: pediu ajuste não aplicado ({'; '.join(note['issues'][:2])}) · {fit}"
    return f"⚠️ {label()}: recomendou descartar ({'; '.join(note['issues'][:2])}) · {fit}"


async def _fix_topic(cand: TopicProposal, c: CandidateCheck, refs: str | None) -> TopicProposal | None:
    refs_rule = ("Use SOMENTE as referências enviadas pelo humano como base e fonte:\n" + refs[:12000]) if refs else \
        "Se precisar, confira na web; fontes primárias com data."
    try:
        fixed = await llm.structured(prompts.ROLE, TOPIC_FIX.format(
            change_request=c.change_request, issues="\n".join(f"- {x}" for x in c.issues) or "-",
            candidate=json.dumps(_public(cand.model_dump()), ensure_ascii=False, indent=1), refs_rule=refs_rule),
            TopicProposal, web=not refs)
    except Exception as ex:  # o ajuste é melhoria; se falhar, segue o original
        log.warning("ajuste do tema pelo revisor falhou: %s", ex)
        return None
    if fixed.pillar not in prompts.PILLAR_NAMES and prompts.PILLAR_NAMES:
        fixed.pillar = cand.pillar
    return fixed


async def review_plan(topic: dict) -> dict | None:
    """Etapa 2. Devolve {'verdict', 'issues', 'change_request'} ou None (revisor desligado/indisponível)."""
    if not active():
        return None
    out = await _call(PLAN.format(theme=MACRO_THEME, topic=json.dumps(_public(topic), ensure_ascii=False, indent=1)),
                      StageCheck)
    return out.model_dump() if out else None


async def review_post(content: dict, topic: dict, images: list) -> PostCheck | None:
    """Etapa 3. Lê o JSON do post e as peças do feed (imagens reduzidas)."""
    if not active():
        return None
    text = POST.format(topic=json.dumps(_public(topic), ensure_ascii=False, indent=1),
                       post=json.dumps(_public_post(content), ensure_ascii=False, indent=1))
    blocks: list = [{"type": "text", "text": text}]
    for img in images[:10]:
        try:
            blocks.append(claude.image_block(img))
        except Exception as ex:
            log.warning("imagem para o revisor falhou (%s): %s", img, ex)
    return await _call(blocks, PostCheck)


def _public_post(content: dict) -> dict:
    out = _public(content)
    out["slides"] = [{k: v for k, v in s.items() if k != "motion_html"} for s in content.get("slides", [])]
    return out


async def review_text(what: str, text: str, context: str, extra: str = "") -> StageCheck | None:
    """Legendas novas e tweets."""
    if not active():
        return None
    return await _call(TEXT.format(what=what, theme=MACRO_THEME, extra=extra or "clareza e força.",
                                   context=context, text=text), StageCheck)


def summary(rv: dict | None) -> str:
    """Linha do revisor para a prévia no Telegram."""
    if not rv:
        return ""
    if rv.get("verdict") == "aprovar":
        return f"{label()}: aprovado de primeira."
    issues = "; ".join(rv.get("issues", [])[:3])
    done = "ajuste aplicado" if rv.get("applied") else "ajuste não aplicado"
    return f"{label()}: pediu 1 ajuste ({issues}). {done.capitalize()}."
