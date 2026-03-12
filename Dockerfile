# Nova Player — Docker image (headless subtitle generation mode)
# Runs the LookaheadScheduler without UI for batch processing.

FROM python:3.11-slim

ARG MODEL=small
ENV NOVA_MODEL=$MODEL \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install faster-whisper soundfile numpy psutil sortedcontainers

COPY nova_player/ ./nova_player/

RUN useradd -m -u 10001 appuser
USER appuser

VOLUME ["/media", "/models"]
ENTRYPOINT ["python", "-u", "-m", "nova_player.ai.chunk_worker"]
