# Agent de social media no Railway (worker, sem porta HTTP: o bot usa long polling do Telegram).
# A marca vem da variável BRAND (pacote em brands/<slug>/); cada empresa é um serviço separado.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    ARCHIVE_DIR=/data/_social \
    TZ=America/Sao_Paulo

WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends fonts-inter \
 && pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .
CMD ["python", "-m", "agent.main"]
