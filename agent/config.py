"""Configuração central. Tudo vem de variáveis de ambiente (.env)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _list(name: str) -> list[str]:
    return [x.strip() for x in _env(name).split(",") if x.strip()]


# ---------- marca ----------
# Cada empresa é um pacote em brands/<slug>/ (brand.toml, knowledge/, assets/, *_sources.json).
# BRAND escolhe qual pacote esta instância usa. Ver ONBOARDING.md.
BRAND_SLUG = _env("BRAND", "exemplo").lower()
BRAND_DIR = PROJECT_DIR / "brands" / BRAND_SLUG
if not (BRAND_DIR / "brand.toml").exists():
    raise SystemExit(f"Pacote de marca não encontrado: {BRAND_DIR}/brand.toml (BRAND={BRAND_SLUG})")
brand: dict = tomllib.loads((BRAND_DIR / "brand.toml").read_text(encoding="utf-8"))
BRAND_NAME: str = brand["identity"]["name"]


@dataclass(frozen=True)
class Config:
    # Telegram
    telegram_token: str = _env("TELEGRAM_BOT_TOKEN")
    allowed_chat_ids: list[int] = field(default_factory=lambda: [int(x) for x in _list("TELEGRAM_ALLOWED_CHAT_IDS")])

    # OpenAI
    openai_api_key: str = _env("OPENAI_API_KEY")
    model: str = _env("OPENAI_MODEL", "gpt-6-luna")
    model_fast: str = _env("OPENAI_MODEL_FAST", "gpt-6-luna")
    reasoning_effort: str = _env("OPENAI_REASONING_EFFORT", "medium")
    # Escrita e revisão editorial do post: mais raciocínio para entregar em alta qualidade
    content_reasoning_effort: str = _env("OPENAI_CONTENT_REASONING_EFFORT", "high")
    image_model: str = _env("OPENAI_IMAGE_MODEL", "gpt-image-2")
    photo_source: str = _env("PHOTO_SOURCE", "library")  # library | generate
    # ChatGPT Images 2: usado no slide de ilustração/diagrama de todo post
    illustration_model: str = _env("OPENAI_ILLUSTRATION_MODEL", "gpt-image-2")
    illustration_quality: str = _env("OPENAI_ILLUSTRATION_QUALITY", "high")

    # Apify (sinais de tendência: Google Trends, Reddit, X; ver apify_sources.json)
    apify_token: str = _env("APIFY_TOKEN")

    # Publicação: buffer (padrão, API grátis) | metricool (plano pago)
    publisher: str = _env("PUBLISHER", "buffer").lower()
    buffer_api_key: str = _env("BUFFER_API_KEY")
    buffer_instagram_channel: str = _env("BUFFER_INSTAGRAM_CHANNEL_ID")  # opcional: senão, descobre sozinho
    buffer_linkedin_channel: str = _env("BUFFER_LINKEDIN_CHANNEL_ID")
    story_frames: int = int(_env("STORY_FRAMES", "0"))  # quadros de stories por post (0 = todos, espelha o feed)
    buffer_queue_limit: int = int(_env("BUFFER_QUEUE_LIMIT", "10"))  # posts agendados por canal (Free = 10; 0 = sem checagem)
    linkedin_format: str = _env("LINKEDIN_FORMAT", "document").lower()  # document (PDF) | images

    # Metricool
    metricool_token: str = _env("METRICOOL_USER_TOKEN")
    metricool_user_id: str = _env("METRICOOL_USER_ID")
    metricool_blog_id: str = _env("METRICOOL_BLOG_ID")
    networks: list[str] = field(default_factory=lambda: _list("PUBLISH_NETWORKS") or ["instagram", "linkedin"])
    publish_stories: bool = _env("PUBLISH_STORIES", "true").lower() == "true"
    slots: list[str] = field(default_factory=lambda: _list("POST_SLOTS") or ["TUE 10:00", "THU 10:00"])

    # Storage público para as imagens (a Metricool baixa a partir de uma URL)
    supabase_url: str = _env("SUPABASE_URL").rstrip("/")
    supabase_key: str = _env("SUPABASE_SERVICE_KEY")
    supabase_bucket: str = _env("SUPABASE_BUCKET", "social-agent")

    # Agenda
    timezone: str = _env("TIMEZONE", "America/Sao_Paulo")
    weekly_day: int = int(_env("WEEKLY_DAY", "0"))  # 0 = segunda
    weekly_time: str = _env("WEEKLY_TIME", "09:00")

    # Marca e caminhos
    brand_slug: str = BRAND_SLUG
    brand_dir: Path = BRAND_DIR
    # Documentos da marca: KNOWLEDGE_ROOT sobrescreve a pasta knowledge/ do pacote
    knowledge_root: Path = Path(_env("KNOWLEDGE_ROOT")
                                or str(BRAND_DIR / brand["knowledge"].get("root", "knowledge"))).expanduser()
    data_dir: Path = Path(_env("DATA_DIR", f"~/.agent-socialmedia/{BRAND_SLUG}")).expanduser()
    archive_dir: Path = Path(_env("ARCHIVE_DIR") or str(Path(_env("DATA_DIR", f"~/.agent-socialmedia/{BRAND_SLUG}")) / "_social")).expanduser()

    site_url: str = _env("SITE_URL") or brand["identity"]["site"]
    instagram_handle: str = _env("INSTAGRAM_HANDLE") or brand["identity"]["instagram"]

    @property
    def assets_dir(self) -> Path:
        return BRAND_DIR / "assets"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    def missing(self) -> list[str]:
        req = {
            "TELEGRAM_BOT_TOKEN": self.telegram_token,
            "TELEGRAM_ALLOWED_CHAT_IDS": self.allowed_chat_ids,
            "OPENAI_API_KEY": self.openai_api_key,
        }
        return [k for k, v in req.items() if not v]

    def can_publish(self) -> list[str]:
        req = {"SUPABASE_URL": self.supabase_url, "SUPABASE_SERVICE_KEY": self.supabase_key}
        if self.publisher == "metricool":
            req |= {
                "METRICOOL_USER_TOKEN": self.metricool_token,
                "METRICOOL_USER_ID": self.metricool_user_id,
                "METRICOOL_BLOG_ID": self.metricool_blog_id,
            }
        else:
            req["BUFFER_API_KEY"] = self.buffer_api_key
        return [k for k, v in req.items() if not v]


cfg = Config()
cfg.data_dir.mkdir(parents=True, exist_ok=True)
cfg.runs_dir.mkdir(parents=True, exist_ok=True)
