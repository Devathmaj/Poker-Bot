# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so editing the code does not invalidate this
# layer. No compiler is needed: psycopg ships binary wheels and Pillow ships
# manylinux wheels.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pokerbot ./pokerbot
COPY assets ./assets

# The bot writes its slash-command sync fingerprint to data/.
RUN useradd --create-home --uid 10001 pokerbot \
    && mkdir -p /app/data \
    && chown -R pokerbot:pokerbot /app

USER pokerbot

# discord.py turns SIGTERM into a graceful shutdown, which cashes out any
# tables that are still open.
STOPSIGNAL SIGTERM

CMD ["python", "-m", "pokerbot"]
