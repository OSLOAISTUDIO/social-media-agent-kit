"""Mostra os últimos posts no Buffer (status, tipo, erro de publicação). Uso:
railway run --service agent python -m scripts.buffer_status"""
import asyncio

import httpx

from agent import publisher
from agent.config import cfg

Q = """query($i: PostsInput!) { posts(first: 20, input: $i) { edges { node {
  id status channelService dueAt sentAt shareMode externalLink text
  metadata { ... on InstagramPostMetadata { type } }
  assets { id mimeType source }
  error { message rawError supportUrl } } } } }"""


async def main():
    async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {cfg.buffer_api_key}"}) as bf:
        orgs = (await publisher._gql(bf, "{ account { organizations { id } } }"))["account"]["organizations"]
        for org in orgs:
            try:
                data = await publisher._gql(bf, Q, {"i": {"organizationId": org["id"],
                                                          "sort": [{"field": "createdAt", "direction": "desc"}]}})
            except RuntimeError:
                data = await publisher._gql(bf, Q.replace("metadata { ... on InstagramPostMetadata { type } }", ""),
                                            {"i": {"organizationId": org["id"]}})
            for e in data["posts"]["edges"]:
                n = e["node"]
                kind = (n.get("metadata") or {}).get("type", "")
                err = n.get("error") or {}
                print(f"{n['channelService']:9} {kind or '-':6} {n['status']:9} imgs={len(n['assets'])} "
                      f"due={str(n.get('dueAt'))[:16]} sent={str(n.get('sentAt'))[:16]} {n.get('externalLink') or ''}")
                if err:
                    print("   ERRO:", err.get("message"), "|", (err.get("rawError") or "")[:300])


asyncio.run(main())
