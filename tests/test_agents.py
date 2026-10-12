"""Testa Revisor, Agent Vídeo, legendas novas e frente X sem chamar APIs (Claude e OpenAI simulados;
render, gravação do vídeo e payload do Buffer são reais). Uso: python -m tests.test_agents"""
import asyncio
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ["ANTHROPIC_API_KEY"] = "test"
os.environ["VIDEO_SECONDS"] = "3"
os.environ["DATA_DIR"] = "/tmp/social-media-agent-test-agents"
os.environ["ARCHIVE_DIR"] = "/tmp/social-media-agent-test-agents/_social"

import shutil  # noqa: E402

shutil.rmtree("/tmp/social-media-agent-test-agents", ignore_errors=True)

from agent import claude, content, costs, db, llm, publisher, reviewer, video, xarm  # noqa: E402
from agent.models import Captions, EditorialReview, PostContent, ResearchResult, TopicProposal  # noqa: E402

here = Path(__file__).parent
sample = json.loads((here / "sample_post.json").read_text())
sample.pop("pillar")
sample.pop("_nota", None)


def topic(title: str) -> TopicProposal:
    return TopicProposal(
        title=title, headline="Título", why_now="Teste.", angle="Novidade", practical_uses=["Uso 1", "Uso 2"],
        narrative="Gancho direto.", data_points=[{"fact": "Dado de teste", "source_url": "https://example.com"}],
        synopsis="Teste.", outline=["capa", "cta"], pillar="Pilar 2",
        scores={"momentum": 8, "novelty": 9, "relevance": 9, "ownability": 9},
        sources=[{"name": "Exemplo", "url": "https://example.com", "date": None}])


CALLS: list[tuple[str, object]] = []  # (agente/esquema, prompt)
USAGE = SimpleNamespace(input_tokens=1000, output_tokens=500, cache_read_input_tokens=0,
                        cache_creation_input_tokens=0, server_tool_use=None)
ANIM = ("<style>.mv-w{position:relative;width:100%;height:100%}.mv-b{position:absolute;bottom:20%;width:18%;"
        "background:var(--azure);transform-origin:bottom;animation:mv-g 1.5s ease-out both}"
        ".mv-1{left:10%;height:30%}.mv-2{left:40%;height:55%;animation-delay:.5s}.mv-3{left:70%;height:80%;"
        "animation-delay:1s}@keyframes mv-g{from{transform:scaleY(0)}to{transform:scaleY(1)}}"
        ".mv-l{position:absolute;bottom:8%;font-size:22px;color:var(--ink)}</style>"
        "<div class='mv-w'><div class='mv-b mv-1'></div><div class='mv-b mv-2'></div><div class='mv-b mv-3'></div>"
        "<span class='mv-l' style='left:10%' onclick='x()'>Hoje</span><script>alert(1)</script></div>")
PLAN_VERDICT = {"v": "ajustar"}


async def fake_claude(system, content_, schema, *, model, agent, effort="medium", fetch=0, max_tokens=16000):
    CALLS.append((schema.__name__, content_))
    costs.record_claude(model, USAGE, agent)
    if schema is reviewer.BriefOut:
        return schema(brief="Pedido afinado dentro do tema.", note="recorte mais claro")
    if schema is reviewer.TopicsCheck:
        assert fetch > 0, "sem referências, o revisor abre as fontes"
        return schema(summary="ok", checks=[
            {"index": 1, "theme_fit": 3, "sources_ok": False, "verdict": "descartar", "issues": ["fora do tema"],
             "change_request": ""},
            {"index": 2, "theme_fit": 9, "sources_ok": False, "verdict": "ajustar", "issues": ["dado não está na fonte"],
             "change_request": "Tire o dado sem fonte."},
            {"index": 3, "theme_fit": 7, "sources_ok": True, "verdict": "aprovar", "issues": [], "change_request": ""}])
    if schema is reviewer.StageCheck:
        v = PLAN_VERDICT["v"]
        return schema(verdict=v, issues=["capa fraca"] if v == "ajustar" else [],
                      change_request="Abra com o resultado concreto." if v == "ajustar" else "")
    if schema is reviewer.PostCheck:
        assert any(b.get("type") == "image" for b in content_), "o revisor vê as peças"
        return schema(verdict="ajustar", scores=[{"criterion": "Capa", "score": 6}], issues=["slide 2 genérico"],
                      change_request="Reescreva o slide 2 com o dado principal.")
    if schema is video.VideoPlan:
        assert max_tokens > 16000 and model == "claude-opus-5-5"
        return schema(needed=True, reason="crescimento fica claro em movimento", eyebrow="Em movimento",
                      title="O ganho em três passos", body="Cada barra é uma etapa.", html=ANIM)
    raise AssertionError(schema)


OPENAI: list[str] = []


async def fake_llm(instructions, prompt, schema, **kw):
    OPENAI.append(schema.__name__)
    if schema is PostContent:
        return PostContent(**sample)
    if schema is EditorialReview:
        return EditorialReview(scores=[{"criterion": "Capa", "score": 9, "note": "ok"}], issues=[], approved=True,
                               content=PostContent(**sample))
    if schema is TopicProposal:
        assert "Tire o dado sem fonte." in prompt
        return topic("Tema ajustado")
    if schema is Captions:
        return Captions(caption_instagram=f"Legenda nova {len(OPENAI)}", caption_linkedin="LinkedIn novo")
    if schema is xarm.Tweet:
        n = OPENAI.count("Tweet")
        text = "x" * 300 if n == 1 else f"Opinião forte número {n} sobre o tema."
        return xarm.Tweet(text=text, angle="opinião", subject="tema", source_name="Exemplo",
                          source_url="https://example.com")
    raise AssertionError(schema)


async def fake_image(prompt, out_path, **kw):
    out_path.write_bytes((here / "fixtures" / "diagram.png").read_bytes())


REAL_CLAUDE = claude.structured
claude.structured = fake_claude
llm.structured = fake_llm
llm.generate_image = fake_image
db.init()


async def test_screen_topics():
    res = ResearchResult(landscape="x", candidates=[topic("A"), topic("B"), topic("C")])
    out, notes = await reviewer.screen_topics(res)
    titles = [c.title for c in out.candidates]
    assert titles == ["Tema ajustado", "C", "A"], titles  # ajustado (9) > aprovado (7) > descartado no fim
    assert notes[0]["verdict"] == "ajustar" and notes[0]["applied"] and notes[2]["verdict"] == "descartar"
    assert OPENAI.count("TopicProposal") == 1, "1 ajuste por tema, uma vez"
    assert "ajustado" in reviewer.topic_line(notes[0]) and "descartar" in reviewer.topic_line(notes[2])
    brief = await reviewer.optimize_brief("agentes", "pedido de post")
    assert brief == "Pedido afinado dentro do tema."
    print("revisor etapas 0 e 1 ok: otimiza o pedido, ajusta 1 tema, descarta o fora do tema e reordena")


async def test_create_with_reviewer_and_video():
    CALLS.clear()
    meter = costs.Meter()
    costs.current.set(meter)
    tid = db.add_topic(None, topic("Tema do post").model_dump(), "approved", "request", "b-agents", "teste")
    pid = await content.create(tid)
    p = db.post(pid)
    c = p["content"]
    names = [n for n, _ in CALLS]
    assert names.count("StageCheck") == 1 and names.count("PostCheck") == 1, names  # 1 revisão por etapa
    assert names.count("VideoPlan") == 1
    first = db.row("SELECT id, status FROM posts WHERE topic_id = ? ORDER BY id LIMIT 1", tid)
    assert first["id"] != pid and first["status"] == "superseded", "ajuste do revisor gera a versão que vai ao humano"
    assert Path(p["render_dir"]).name == "v1-revisor"
    rv = c["_reviewer"]
    assert rv["plan"]["applied"] and rv["post"]["applied"] and rv["post"]["verdict"] == "ajustar"
    # vídeo: slide motion logo depois da ilustração, sem script/handlers, MP4 gravado
    layouts = [s["layout"] for s in c["slides"]]
    assert layouts[layouts.index("illustration") + 1] == "motion", layouts
    html = c["slides"][layouts.index("motion")]["motion_html"]
    assert "<script" not in html and "onclick" not in html
    assert len(c["_render"]["feed"]) == len(c["slides"]) == 10
    mp4 = Path(c["_render"]["video"]["mp4"])
    assert mp4.exists() and mp4.stat().st_size > 5000
    info = subprocess.run([video._ffmpeg(), "-i", str(mp4)], capture_output=True, text=True).stderr
    assert "h264" in info and "1080x1350" in info and "Duration: 00:00:03" in info, info[-400:]
    assert c["_video"]["needed"] and c["_render"]["video"]["index"] == layouts.index("motion")
    # custo separado por agente
    assert meter.agents.get("reviewer", 0) > 0 and meter.agents.get("video", 0) > 0 and meter.agents.get("openai", 0) >= 0
    line = costs.split_line(meter.agents)
    assert "Revisor" in line and "Agent Vídeo" in line and "total" in line
    # publicação: o slide animado vai como vídeo no carrossel do Instagram
    urls = [f"https://x/{i}.jpg" for i in range(10)]
    assets = publisher.feed_assets(urls, {"index": 5, "url": "https://x/v.mp4"})
    assert assets[5] == {"video": {"url": "https://x/v.mp4", "thumbnailUrl": urls[5]}} and "image" in assets[4]
    print(f"criação ok: plano e post revisados 1 vez cada, slide animado {mp4.name} (H.264 1080×1350), "
          f"custo {line}")
    return pid


async def test_human_revision_keeps_video(pid: int):
    CALLS.clear()
    PLAN_VERDICT["v"] = "aprovar"
    new_id = await content.revise(pid, "Troque o título da capa.")
    c = db.post(new_id)["content"]
    assert any(s["layout"] == "motion" for s in c["slides"]), "o slide animado volta depois da reescrita"
    assert [n for n, _ in CALLS].count("VideoPlan") == 0, "sem falar de vídeo, o Agent Vídeo não refaz"
    assert [n for n, _ in CALLS].count("PostCheck") == 1, "nova versão também passa pelo revisor (1 vez)"
    print("ajuste humano ok: texto reescrito, slide animado mantido, revisor conferiu a nova versão")
    return new_id


async def test_captions(pid: int):
    CALLS.clear()
    PLAN_VERDICT["v"] = "ajustar"
    before = OPENAI.count("Captions")
    await content.new_captions(pid, "mais curta")
    c = db.post(pid)["content"]
    assert OPENAI.count("Captions") - before == 2, "1 escrita + 1 ajuste pedido pelo revisor"
    assert c["caption_instagram"].startswith("Legenda nova") and c["_reviewer"]["captions"]["applied"]
    print("legendas ok: outra legenda com direcionamento, revisada com 1 pedido de ajuste")


async def test_x():
    xarm.set_value("mode", "on")
    xarm.set_value("tweets_per_week", 5)
    assert xarm.mode() == "on" and xarm.settings()["per_week"] == 5
    monday = datetime(2026, 10, 12, 11, 0, tzinfo=xarm.TZ)
    assert xarm.due_now(monday) == 1 and xarm.due_now(monday.replace(day=14)) == 3  # 5/semana: seg 1, qua 3
    PLAN_VERDICT["v"] = "ajustar"
    meter = costs.Meter()
    costs.current.set(meter)
    tid = await xarm.write(xarm.seed_request("agentes de IA no atendimento"))
    t = db.tweet(tid)
    assert len(t["text"]) <= 280 and t["payload"]["review"]["applied"], t
    assert t["week_start"] == xarm.week_key()
    seed = xarm.seed_auto()
    assert seed["kind"] in ("topic", "web")

    import httpx
    sent = []

    def handler(req: httpx.Request):
        q = json.loads(req.content)["query"]
        sent.append(json.loads(req.content))
        if "organizations" in q and "posts" not in q:
            return httpx.Response(200, json={"data": {"account": {"organizations": [{"id": "org1"}]}}})
        if "channels" in q:
            return httpx.Response(200, json={"data": {"channels": [
                {"id": "tw1", "service": "twitter", "name": "x", "isLocked": False, "isDisconnected": False}]}})
        if "posts(" in q:
            return httpx.Response(200, json={"data": {"posts": {"edges": []}}})
        return httpx.Response(200, json={"data": {"createPost": {"post": {"id": "p1", "dueAt": "x"}}}})

    real = httpx.AsyncClient
    publisher.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    object.__setattr__(publisher.cfg, "buffer_api_key", "bk")
    await publisher.publish_tweet(tid, publisher.next_x_slot())
    create = next(x["variables"]["input"] for x in sent if "createPost" in x["query"])
    assert create["channelId"] == "tw1" and create["text"] == t["text"] and create["mode"] == "customScheduled"
    assert db.tweet(tid)["status"] == "published"
    publisher.httpx.AsyncClient = real
    print(f"frente X ok: cota 5/semana, tweet {len(t['text'])} caracteres revisado, agendado no canal X do Buffer")


async def test_no_credit():
    """API Anthropic sem crédito: o post sai inteiro, sem Revisor e sem vídeo, com aviso para o Telegram."""
    import anthropic
    import httpx2

    def no_credit():
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        return anthropic.BadRequestError(
            "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to "
            "upgrade or purchase credits.", response=httpx2.Response(400, request=req), body=None)

    class Stream:
        async def __aenter__(self):
            raise no_credit()

        async def __aexit__(self, *a):
            return False

    class Broke:  # parse (Revisor) e stream (Agent Vídeo) recusam por falta de crédito
        async def parse(self, **kw):
            raise no_credit()

        def stream(self, **kw):
            return Stream()

    claude.structured = REAL_CLAUDE
    claude._client = SimpleNamespace(beta=SimpleNamespace(messages=Broke()))
    res, notes = await reviewer.screen_topics(ResearchResult(landscape="x", candidates=[topic("A")]))
    assert notes == [None] and claude.problem()["kind"] == "credit"
    tid = db.add_topic(None, topic("Sem crédito").model_dump(), "approved", "request", "b-credit", "teste")
    pid = await content.create(tid)
    c = db.post(pid)["content"]
    assert db.post(pid)["status"] == "review" and len(c["_render"]["feed"]) == len(c["slides"])
    assert not any(s["layout"] == "motion" for s in c["slides"]) and not c.get("_reviewer")
    assert "sem crédito" in claude.notice() and "seguiu normalmente" in claude.notice()
    claude.structured, claude._client = fake_claude, None
    claude._set_problem(None)
    print("sem crédito na Anthropic ok: post criado sem Revisor e sem vídeo, com aviso no Telegram")


async def main():
    await test_screen_topics()
    pid = await test_create_with_reviewer_and_video()
    pid = await test_human_revision_keeps_video(pid)
    await test_captions(pid)
    await test_x()
    await test_no_credit()


asyncio.run(main())
