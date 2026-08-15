FROM node:22-bookworm-slim AS studio-ui
WORKDIR /web
COPY studio/web/package.json studio/web/package-lock.json ./
RUN npm ci
COPY studio/web/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    # pyrender headless-рендер через EGL (без X-сервера).
    PYOPENGL_PLATFORM=egl

# Системные зависимости: OpenGL/EGL для pyrender/trimesh + утилиты.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libegl1 \
        libgles2 \
        libosmesa6 \
        libxext6 \
        libxrender1 \
        libsm6 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости — лучше кешируется.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Chromium для браузерных превью (playwright) + его системные библиотеки.
RUN python -m playwright install --with-deps chromium

COPY app ./app
COPY static ./static
COPY scripts ./scripts
COPY --from=studio-ui /web/dist /app/static/studio

# Каталог моделей и данные монтируются как volume (см. docker-compose.yml).
ENV MODELS_DIR=/data/models \
    DATA_DIR=/data/app \
    QDRANT_HOST=qdrant \
    QDRANT_PORT=6333

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
