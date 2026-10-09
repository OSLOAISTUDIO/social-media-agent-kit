"""Estado persistente em SQLite: semanas, temas, posts e o que o bot espera do humano."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from .config import cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS weeks (
  id INTEGER PRIMARY KEY,
  week_start TEXT UNIQUE NOT NULL,      -- data da segunda-feira, ISO
  n_posts INTEGER,                      -- definido pelo humano
  direction TEXT,                       -- direcionamento acumulado da semana
  status TEXT NOT NULL DEFAULT 'asking' -- asking | researching | proposing | done
);
CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY,
  week_id INTEGER,                      -- NULL quando é pedido avulso
  origin TEXT NOT NULL DEFAULT 'weekly',-- weekly | request
  batch TEXT,                           -- linhagem de propostas (rodada de pesquisa + substitutas)
  brief TEXT,                           -- pedido/direcionamento que gerou a rodada
  status TEXT NOT NULL,                 -- backlog | proposed | rejected | approved
  title TEXT NOT NULL,
  payload TEXT NOT NULL,                -- JSON completo da proposta
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  topic_id INTEGER NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,                 -- review | changes | approved | published | failed
  content TEXT NOT NULL,                -- JSON: slides + legendas
  render_dir TEXT,
  feedback TEXT,
  photos TEXT,                          -- JSON: fotos usadas
  metricool TEXT,                       -- JSON: ids/retornos da Metricool
  created_at TEXT NOT NULL,
  published_at TEXT
);
CREATE TABLE IF NOT EXISTS chat_state (
  chat_id INTEGER PRIMARY KEY,
  awaiting TEXT,                        -- theme | direction | request | revision | schedule | NULL
  ref_id INTEGER
);
CREATE TABLE IF NOT EXISTS costs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                   -- research (ref = batch) | post (ref = post id)
  ref TEXT NOT NULL,
  usd REAL NOT NULL,
  detail TEXT,                          -- JSON: chamadas, tokens, buscas, imagens
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_alias (
  old_id INTEGER PRIMARY KEY,           -- grupo comum autorizado no .env
  new_id INTEGER NOT NULL               -- supergrupo em que ele foi convertido pelo Telegram
);
"""


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def row(sql: str, *args: Any) -> dict | None:
    with conn() as c:
        r = c.execute(sql, args).fetchone()
        return dict(r) if r else None


def rows(sql: str, *args: Any) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def execute(sql: str, *args: Any) -> int:
    with conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


# ---------- semanas ----------

def get_or_create_week(week_start: str) -> dict:
    w = row("SELECT * FROM weeks WHERE week_start = ?", week_start)
    if w:
        return w
    execute("INSERT INTO weeks (week_start) VALUES (?)", week_start)
    return row("SELECT * FROM weeks WHERE week_start = ?", week_start)


def update_week(week_id: int, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE weeks SET {sets} WHERE id = ?", *fields.values(), week_id)


# ---------- temas ----------

def add_topic(week_id: int | None, payload: dict, status: str, origin: str = "weekly",
              batch: str | None = None, brief: str | None = None) -> int:
    return execute(
        "INSERT INTO topics (week_id, origin, batch, brief, status, title, payload, created_at) VALUES (?,?,?,?,?,?,?,?)",
        week_id, origin, batch, brief, status, payload["title"], json.dumps(payload, ensure_ascii=False), now(),
    )


def next_backlog(batch: str) -> dict | None:
    r = row("SELECT id FROM topics WHERE batch = ? AND status = 'backlog' ORDER BY id LIMIT 1", batch)
    return topic(r["id"]) if r else None


def topic(topic_id: int) -> dict:
    t = row("SELECT * FROM topics WHERE id = ?", topic_id)
    t["payload"] = json.loads(t["payload"])
    return t


def set_topic_status(topic_id: int, status: str) -> None:
    execute("UPDATE topics SET status = ? WHERE id = ?", status, topic_id)


def week_topics(week_id: int, status: str | None = None) -> list[dict]:
    if status:
        rs = rows("SELECT * FROM topics WHERE week_id = ? AND status = ? ORDER BY id", week_id, status)
    else:
        rs = rows("SELECT * FROM topics WHERE week_id = ? ORDER BY id", week_id)
    for r in rs:
        r["payload"] = json.loads(r["payload"])
    return rs


def recent_titles(limit: int = 40) -> list[str]:
    """Temas já aprovados ou propostos recentemente, para evitar repetição."""
    rs = rows("SELECT title, status FROM topics ORDER BY id DESC LIMIT ?", limit)
    return [f"{r['title']} ({r['status']})" for r in rs]


# ---------- posts ----------

def add_post(topic_id: int, content: dict, render_dir: str, photos: list[str], version: int = 1) -> int:
    return execute(
        "INSERT INTO posts (topic_id, version, status, content, render_dir, photos, created_at) VALUES (?,?,?,?,?,?,?)",
        topic_id, version, "review", json.dumps(content, ensure_ascii=False), render_dir, json.dumps(photos), now(),
    )


def post(post_id: int) -> dict:
    p = row("SELECT * FROM posts WHERE id = ?", post_id)
    p["content"] = json.loads(p["content"])
    p["photos"] = json.loads(p["photos"] or "[]")
    p["metricool"] = json.loads(p["metricool"]) if p.get("metricool") else {}  # registro da publicação
    return p


def update_post(post_id: int, **fields: Any) -> None:
    for k, v in fields.items():
        if isinstance(v, (dict, list)):
            fields[k] = json.dumps(v, ensure_ascii=False)
    sets = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE posts SET {sets} WHERE id = ?", *fields.values(), post_id)


def recent_photos(limit: int = 6) -> list[str]:
    out: list[str] = []
    for r in rows("SELECT photos FROM posts ORDER BY id DESC LIMIT ?", limit):
        out += json.loads(r["photos"] or "[]")
    return out


def week_published_count(week_id: int) -> int:
    r = row(
        "SELECT COUNT(*) AS n FROM posts p JOIN topics t ON t.id = p.topic_id "
        "WHERE t.week_id = ? AND p.status IN ('approved','published')", week_id,
    )
    return r["n"] if r else 0


# ---------- conversa ----------

def set_awaiting(chat_id: int, awaiting: str | None, ref_id: int | None = None) -> None:
    execute(
        "INSERT INTO chat_state (chat_id, awaiting, ref_id) VALUES (?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET awaiting = excluded.awaiting, ref_id = excluded.ref_id",
        chat_id, awaiting, ref_id,
    )


def awaiting(chat_id: int) -> tuple[str | None, int | None]:
    r = row("SELECT awaiting, ref_id FROM chat_state WHERE chat_id = ?", chat_id)
    return (r["awaiting"], r["ref_id"]) if r else (None, None)


# ---------- grupo convertido em supergrupo ----------

def add_alias(old_id: int, new_id: int) -> None:
    execute("INSERT OR REPLACE INTO chat_alias (old_id, new_id) VALUES (?, ?)", old_id, new_id)


def resolve_chat(chat_id: int) -> int:
    """ID atual de um chat do .env (segue a conversão de grupo em supergrupo)."""
    r = row("SELECT new_id FROM chat_alias WHERE old_id = ?", chat_id)
    return r["new_id"] if r else chat_id


def aliases_of(chat_ids: list[int]) -> set[int]:
    if not chat_ids:
        return set()
    marks = ",".join("?" * len(chat_ids))
    return {r["new_id"] for r in rows(f"SELECT new_id FROM chat_alias WHERE old_id IN ({marks})", *chat_ids)}


# ---------- custos da OpenAI ----------

def add_cost(kind: str, ref, detail: dict) -> None:
    execute("INSERT INTO costs (kind, ref, usd, detail, created_at) VALUES (?,?,?,?,?)",
            kind, str(ref), detail["usd"], json.dumps(detail), now())


def cost(kind: str, ref) -> float:
    r = row("SELECT COALESCE(SUM(usd), 0) AS v FROM costs WHERE kind = ? AND ref = ?", kind, str(ref))
    return r["v"] if r else 0.0


def topic_posts_cost(topic_id: int) -> float:
    r = row("SELECT COALESCE(SUM(c.usd), 0) AS v FROM costs c JOIN posts p ON c.kind = 'post' AND c.ref = CAST(p.id AS TEXT) "
            "WHERE p.topic_id = ?", topic_id)
    return r["v"] if r else 0.0


def cost_since(iso_date: str) -> float:
    r = row("SELECT COALESCE(SUM(usd), 0) AS v FROM costs WHERE created_at >= ?", iso_date)
    return r["v"] if r else 0.0
