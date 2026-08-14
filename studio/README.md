# Облачная студия разработки игр — отдельное приложение по адресу `/app`.

Стек: React + TypeScript + Vite + Tailwind + shadcn-подобные UI, Three.js и Babylon.js,
FastAPI (Python), PostgreSQL, Docker.

Оркестратор (Claude / Grok / GPT, в том числе через [Kie.ai](https://kie.ai/claude-opus-5))
разбивает запрос на задачи: поиск 3D в каталоге ModelFolder, концепт-арт, трейлер,
озвучка, музыка, меш (Meshy), сборка веб/мобильного/десктоп превью.

## Запуск

```bash
cd studio
cp .env.example .env   # впишите KIE_API_KEY / OPENAI / ANTHROPIC / GROK / MESHY
docker compose up --build
```

Открыть: **http://127.0.0.1:18081/app**

Локально без Docker:

```bash
# postgres: создайте БД studio (порт 15433 в compose)
cd backend && python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 18082 --reload
cd web && npm install && npm run dev
```

Vite dev: http://127.0.0.1:5173/app  (проксирует `/app/api` на FastAPI `:18082`)

## API

- `GET /api/v1/health`
- `GET /api/v1/models` — реестр агентов/моделей
- `POST /api/v1/projects` `{ "prompt", "platform": "web|mobile|desktop" }`
- `GET /api/v1/projects/{id}`
- `GET /api/v1/projects/{id}/events` — SSE пайплайна
- `POST /api/v1/projects/{id}/chat`

Миграции: SQL в `backend/migrations/`, накатываются при старте API.

## Провайдеры

| Роль | Модели |
|------|--------|
| Оркестратор | Claude Opus 5 / Sonnet 5 / Fable 5 / Opus 4.8, GPT-5.6, Grok 4.6, Gemini Flash, Codex |
| Картинки | GPT Image 1.5, 4o Image, Nano Banana 2 Lite, FLUX 3 |
| Видео | Grok Imagine 1.5, Kling O3, Gemini Omni, Runway, Hailuo |
| Голос | ElevenLabs TTS, Dialogue V3 |
| Музыка | Suno |
| 3D | Meshy + каталог ModelFolder |
