"""Esquemas de saída estruturada do modelo. Sem valores default (modo strict da OpenAI)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------- pesquisa ----------------


class Source(BaseModel):
    name: str = Field(description="Veículo ou instituição, ex.: 'The Verge', 'OpenAI Blog'")
    url: str
    date: str | None = Field(description="Data de publicação, AAAA-MM-DD, se conhecida")


class DataPoint(BaseModel):
    fact: str = Field(description="Dado concreto e verificável, com número quando houver")
    source_url: str


class Scores(BaseModel):
    momentum: int = Field(description="0-10. Potencial viral: quanto o tema está gerando conversa, compartilhamento e engajamento AGORA")
    novelty: int = Field(description="0-10. Quão nova é a novidade: lançamento ou recurso dos últimos dias = 9-10; dado/pesquisa sem novidade de produto = 3-5")
    relevance: int = Field(description="0-10. Relevância para o público da marca")
    ownability: int = Field(description="0-10. Quanto a marca tem autoridade para falar e conectar com sua oferta")


class TopicProposal(BaseModel):
    title: str = Field(description="Nome curto do tema")
    headline: str = Field(description="Título provisório da capa, no tom da marca")
    angle: Literal["Novidade", "Aplicação prática", "Dados e pesquisa", "Mercado"] = Field(
        description="Tipo de post: novidade (lançamento/recurso novo), aplicação prática (como usar no dia a dia), dados e pesquisa, ou mercado")
    practical_uses: list[str] = Field(description="2 a 4 aplicações práticas concretas no dia a dia de trabalho (o que alguém faz com isso amanhã)")
    narrative: str = Field(description="Pegada narrativa: gancho, formato e tom do post, inspirados nas referências editoriais, na voz da marca")
    why_now: str = Field(description="Justificativa: por que este tema está em alta nesta semana")
    data_points: list[DataPoint] = Field(description="2 a 4 dados que sustentam o post")
    synopsis: str = Field(description="Sinopse do que o post vai trabalhar, 3 a 5 frases")
    outline: list[str] = Field(description="Roteiro slide a slide, 5 a 8 linhas")
    pillar: str = Field(description="Exatamente um dos pilares da marca listados no prompt")
    scores: Scores
    sources: list[Source] = Field(description="Fontes primárias para o humano conferir")


class ResearchResult(BaseModel):
    landscape: str = Field(description="Leitura geral da semana em IA e tecnologia, 2 a 3 frases")
    candidates: list[TopicProposal]


# ---------------- post ----------------


class Metric(BaseModel):
    value: str = Field(description="Ex.: '−29%', '3×', 'US$ 4 bi'")
    label: str


class Item(BaseModel):
    title: str
    text: str


class Bar(BaseModel):
    label: str
    value: float
    display: str = Field(description="Valor formatado para exibir, ex.: '165,6 mil'")
    highlight: bool


class Series(BaseModel):
    name: str
    values: list[float]
    highlight: bool


Layout = Literal["cover", "big_number", "metrics", "list", "statement", "bars", "line", "illustration", "cta"]


class Slide(BaseModel):
    layout: Layout
    eyebrow: str | None = Field(description="Rótulo em caixa alta. Em conteúdo: 'Tema' (o número é inserido automaticamente)")
    title: str = Field(description="Display na capa/CTA, H2 nos demais. Uma ideia por slide")
    lead: str | None = Field(description="Frase de apoio, até ~140 caracteres")
    body: str | None = Field(description="Texto curto que acompanha o número/gráfico, até ~160 caracteres")
    big: str | None = Field(description="Número de destaque (layout big_number)")
    metrics: list[Metric] | None = Field(description="2 ou 3 métricas (layout metrics)")
    items: list[Item] | None = Field(description="3 ou 4 itens (layout list)")
    bars: list[Bar] | None = Field(description="3 a 6 barras (layout bars)")
    series: list[Series] | None = Field(description="1 ou 2 séries com os mesmos pontos (layout line)")
    x_labels: list[str] | None = Field(description="Rótulos do eixo X: primeiro e último (layout line)")
    visual_brief: str | None = Field(description="Layout illustration: o que o diagrama mostra (elementos, relações, fluxo), em 2 a 4 frases")
    visual_labels: list[str] | None = Field(description="Layout illustration: rótulos exatos dentro do diagrama, até 6, no máximo 3 palavras cada")
    tag: str | None = Field(description="Tag retangular da capa, ex.: 'Em alta', 'Lançamento'")
    source: str | None = Field(description="Fonte do dado exibida no rodapé, ex.: 'Fonte: OpenAI, 2026.'")


class PostContent(BaseModel):
    slug: str = Field(description="kebab-case, sem acento, ex.: 'agentes-de-ia-em-producao'")
    slides: list[Slide]
    caption_instagram: str
    caption_linkedin: str
    alt_text: str = Field(description="Texto alternativo da capa")
    photo_mood: str = Field(description="Descrição curta da atmosfera fotográfica nórdica desejada para capa e CTA")


class Fixes(BaseModel):
    content: PostContent


class Intent(BaseModel):
    kind: Literal["new_post", "direction", "other"]
    brief: str = Field(description="Briefing limpo extraído da mensagem")


class ScheduleParse(BaseModel):
    ok: bool = Field(description="false se a mensagem não indica um dia/horário compreensível")
    datetime_local: str | None = Field(description="AAAA-MM-DDTHH:MM no fuso informado")


class CriterionScore(BaseModel):
    criterion: str
    score: int = Field(description="0-10")
    note: str = Field(description="O que está bom ou o que falta, em uma frase")


class EditorialReview(BaseModel):
    scores: list[CriterionScore]
    issues: list[str] = Field(description="Problemas concretos encontrados, slide a slide")
    approved: bool = Field(description="true só se TODOS os critérios tiverem nota 8 ou mais")
    content: PostContent = Field(description="Versão revisada do post (igual à original se approved=true)")
