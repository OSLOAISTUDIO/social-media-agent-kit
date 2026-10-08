"""Testa o pipeline sem APIs: guarda de linguagem, criação com modelo simulado, render e arquivamento.
Uso: python -m tests.test_pipeline"""
import asyncio
import json
import os
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ["DATA_DIR"] = "/tmp/social-media-agent-test"
os.environ["ARCHIVE_DIR"] = "/tmp/social-media-agent-test/_social"

from agent import content, db, llm, publisher  # noqa: E402
from agent.models import EditorialReview, PostContent, TopicProposal  # noqa: E402

here = Path(__file__).parent
sample = json.loads((here / "sample_post.json").read_text())
sample.pop("pillar")

TOPIC = TopicProposal(
    title="Agentes de IA em produção", headline="O piloto acabou", why_now="Teste.",
    angle="Novidade", practical_uses=["Resumir reuniões do comercial", "Triagem de chamados"],
    narrative="Gancho direto no lançamento, formato prático.",
    data_points=[{"fact": "Dado de teste", "source_url": "https://example.com"}],
    synopsis="Teste.", outline=["capa", "cta"], pillar="Integração de ambientes IA",
    scores={"momentum": 8, "novelty": 9, "relevance": 9, "ownability": 9},
    sources=[{"name": "Exemplo", "url": "https://example.com", "date": None}],
).model_dump()


async def fake_image(prompt, out_path, **kw):
    assert kw.get("model") == "gpt-image-2" and "ERP E CRM" in prompt, kw
    from agent.illustration import REFERENCE
    if REFERENCE.exists():  # marcas sem imagem de referência geram só pelo texto de estilo
        assert kw.get("reference") and Path(kw["reference"]).name == REFERENCE.name, "referência de estilo"
    out_path.write_bytes((here / "fixtures" / "diagram.png").read_bytes())


REVIEWS = []


async def fake_structured(instructions, prompt, schema, **kw):
    assert kw.get("effort") == "high", "escrita e revisão usam raciocínio alto"
    if schema is EditorialReview:
        # 1ª revisão reprova (nota 6 na capa) e reescreve; 2ª aprova
        approved = len(REVIEWS) > 0
        REVIEWS.append(approved)
        return EditorialReview(
            scores=[{"criterion": "Capa", "score": 9 if approved else 6, "note": "ok"},
                    {"criterion": "Precisão", "score": 9, "note": "ok"}],
            issues=[] if approved else ["Capa genérica"], approved=approved, content=PostContent(**sample))
    assert schema is PostContent
    return PostContent(**sample)


def test_language_guard():
    """Cada regra de linguagem configurada para a marca ativa precisa disparar."""
    bad = json.loads(json.dumps(sample))
    banned = content.BANNED[0] if content.BANNED else ""
    bad["caption_instagram"] = f"Isso não é hype, é operação — e é {banned}! Mais do que tecnologia, resultado."
    v = content.language_violations(bad)
    labels = " ".join(v)
    for _, label in content.CHECKS:
        if "nome da marca" not in label:
            assert label in labels, (label, v)
    if banned:
        assert banned in labels, v
    print("guarda de linguagem ok:", len(v), "violações detectadas", f"({len(content.CHECKS)} regras da marca)")


async def test_create_and_archive():
    db.init()
    llm.structured = fake_structured
    llm.generate_image = fake_image
    tid = db.add_topic(None, TOPIC, "approved", "request", "b-test", "teste")
    pid = await content.create(tid)
    p = db.post(pid)
    r = p["content"]["_render"]
    assert len(r["feed"]) == 9 and len(r["story"]) == 9, r
    assert list(p["content"]["_images"]) == ["3"], p["content"]["_images"]
    rv = p["content"]["_review"]
    assert REVIEWS == [False, True] and rv["approved"] and rv["min"] == 9, (REVIEWS, rv)
    print("revisão editorial ok: reprovou, reescreveu, aprovou com nota mínima", rv["min"])
    assert all(Path(f).exists() for f in r["feed"] + r["story"])
    dest = publisher.archive(pid)
    assert (dest / "feed" / "carrossel-01.png").exists() and (dest / "legendas.md").exists()
    print("criação + render + arquivo ok:", dest)


test_language_guard()
asyncio.run(test_create_and_archive())


async def test_publish_payload():
    """Simula Supabase e Buffer (e depois Metricool) e confere o que seria enviado."""
    import httpx

    sent = []
    queued = 0  # posts já agendados no canal (simulado)
    created, failing = [], {}

    def handler(req: httpx.Request):
        sent.append(req)
        if "/storage/v1/object/" in req.url.path:
            return httpx.Response(200, json={"Key": "ok"})
        if req.url.host == "api.buffer.com":
            q = json.loads(req.content)["query"]
            if "organizations" in q:
                return httpx.Response(200, json={"data": {"account": {"organizations": [{"id": "org1", "name": "Exemplo"}]}}})
            if "externalLink" in q:  # conferência: carrossel do Instagram com erro, resto publicado
                return httpx.Response(200, json={"data": {"posts": {"edges": [
                    {"node": {"id": pid_, "status": "error" if pid_ == failing.get("id") else "sent",
                              "externalLink": None if pid_ == failing.get("id") else f"https://x/{pid_}",
                              "error": {"message": "Failed to create media container"} if pid_ == failing.get("id") else None}}
                    for pid_ in created]}}})
            if "posts(" in q:  # fila de agendados do canal
                return httpx.Response(200, json={"data": {"posts": {"edges": [{"node": {"id": f"q{i}"}} for i in range(queued)]}}})
            if "channels" in q:
                return httpx.Response(200, json={"data": {"channels": [
                    {"id": "ig1", "service": "instagram", "name": "exemplo", "isLocked": False, "isDisconnected": False},
                    {"id": "li1", "service": "linkedin", "name": "Exemplo", "isLocked": False, "isDisconnected": False}]}})
            created.append(f"p{len(sent)}")
            return httpx.Response(200, json={"data": {"createPost": {"post": {"id": created[-1], "dueAt": "x"}}}})
        if "normalize" in req.url.path:
            return httpx.Response(200, text='"https://static.metricool.com/x.png"')
        return httpx.Response(200, json={"data": {"id": len(sent)}})

    real = httpx.AsyncClient
    publisher.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    for k, v in {"publisher": "buffer", "buffer_api_key": "bk", "supabase_url": "https://x.supabase.co",
                 "supabase_key": "k", "story_frames": 4, "linkedin_format": "document"}.items():
        object.__setattr__(publisher.cfg, k, v)
    pid = db.rows("SELECT id FROM posts ORDER BY id DESC LIMIT 1")[0]["id"]
    out = await publisher.publish(pid, publisher.next_slot())

    creates = [json.loads(r.content)["variables"]["input"] for r in sent
               if r.url.host == "api.buffer.com" and "createPost" in json.loads(r.content)["query"]]
    ig = [c for c in creates if c["channelId"] == "ig1" and c["metadata"]["instagram"]["type"] == "post"]
    stories = [c for c in creates if c["channelId"] == "ig1" and c["metadata"]["instagram"]["type"] == "story"]
    li = [c for c in creates if c["channelId"] == "li1"]
    assert len(ig) == 1 and len(ig[0]["assets"]) == 9, ig
    assert len(stories) == 4 and all(len(c["assets"]) == 1 for c in stories), len(stories)
    assert [Path(c["assets"][0]["image"]["url"]).name for c in stories] == \
        ["storie-01-1080.jpg", "storie-02-1080.jpg", "storie-04-1080.jpg", "storie-09-1080.jpg"], "capa, 1º dado, ilustração, CTA"
    assert len(li) == 1 and li[0]["assets"][0]["document"]["url"].endswith(".pdf"), li
    assert ig[0]["dueAt"].endswith(".000Z") and ig[0]["mode"] == "customScheduled"
    assert next(r for r in sent if r.url.host == "api.buffer.com").headers["Authorization"] == "Bearer bk"
    pdf = Path(db.post(pid)["render_dir"]) / "carrossel.pdf"  # PDF vetorial gerado no render
    assert pdf.exists() and pdf.read_bytes()[:4] == b"%PDF"
    print("payload Buffer ok:", len(out["posts"]), "agendamentos (1 carrossel, 4 stories, 1 LinkedIn em PDF)")

    def creates_of(reqs):
        return [json.loads(r.content)["variables"]["input"] for r in reqs
                if r.url.host == "api.buffer.com" and "createPost" in json.loads(r.content)["query"]]

    # Padrão: stories espelham o feed inteiro (9 quadros, mesma ordem)
    object.__setattr__(publisher.cfg, "story_frames", 0)
    sent.clear()
    out = await publisher.publish(pid, publisher.next_slot())
    st = [c for c in creates_of(sent) if c.get("metadata", {}).get("instagram", {}).get("type") == "story"]
    assert [Path(c["assets"][0]["image"]["url"]).name for c in st] == [f"storie-{i:02d}-1080.jpg" for i in range(1, 10)]
    print("stories espelhando o feed ok:", len(st), "quadros")

    # Fila cheia: 5 já agendados + 10 necessários > 10 → nada é criado
    queued = 5
    sent.clear()
    try:
        await publisher.publish(pid, publisher.next_slot())
        raise AssertionError("deveria ter recusado por fila cheia")
    except publisher.QueueFull as ex:
        assert not creates_of(sent), "não pode criar nada com a fila cheia"
        print("fila cheia ok:", str(ex)[:90], "…")

    # Postar agora: shareNow, sem dueAt e sem checar a fila
    sent.clear()
    await publisher.publish(pid, publisher.asap(), now=True)
    cs = creates_of(sent)
    assert cs and all(c["mode"] == "shareNow" and "dueAt" not in c for c in cs)
    print("postar agora ok:", len(cs), "publicações imediatas")
    queued = 0

    # Imagens vão em JPEG (o feed do Instagram recusa PNG)
    ups = [r for r in sent if "/storage/v1/object/" in r.url.path and "/public/" not in r.url.path]
    imgs = [r for r in ups if not r.url.path.endswith(".pdf")]
    assert imgs and all(r.url.path.endswith(".jpg") and r.headers["Content-Type"] == "image/jpeg" for r in imgs)
    print("upload em JPEG ok:", len(imgs), "imagens")

    # Conferência: o carrossel do Instagram falhou no Buffer; nova tentativa recria só ele, em JPEG
    ig_feed = next(x for x in db.post(pid)["metricool"]["posts"] if x["label"] == "Instagram feed")
    failing["id"] = ig_feed["id"]
    res = await publisher.verify(pid)
    st = {r["label"]: r["status"] for r in res}
    assert st["Instagram feed"] == "error" and st["LinkedIn"] == "sent" and st["Story 1"] == "sent", st
    sent.clear()
    new = await publisher.retry_failed(pid, ["Instagram feed"])
    cs = creates_of(sent)
    assert len(new) == 1 and len(cs) == 1 and cs[0]["mode"] == "shareNow" and len(cs[0]["assets"]) == 9
    assert all(a["image"]["url"].endswith(".jpg") for a in cs[0]["assets"])
    labels = [x["label"] for x in db.post(pid)["metricool"]["posts"]]
    assert labels.count("Instagram feed") == 1 and new[0]["id"] in [x["id"] for x in db.post(pid)["metricool"]["posts"]]
    print("conferência + nova tentativa ok: carrossel recriado em JPEG, resto mantido")

    # Metricool continua disponível como alternativa
    sent.clear()
    for k, v in {"publisher": "metricool", "metricool_token": "t", "metricool_user_id": "1", "metricool_blog_id": "2",
                 "publish_stories": True, "story_frames": 0}.items():
        object.__setattr__(publisher.cfg, k, v)
    out = await publisher.publish(pid, publisher.next_slot())
    assert len(out["posts"]) == 11 and sent[-1].headers["X-Mc-Auth"] == "t"
    print("payload Metricool ok:", len(out["posts"]), "agendamentos")


asyncio.run(test_publish_payload())
