import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Централизованная конфигурация: переменные из `.env` в корне проекта.
# override=True: на машине разработчика в системных переменных Windows (User/Machine)
# может висеть чужой OPENAI_API_KEY от другого проекта — без override он молча
# перебивает .env, и приложение шлёт запросы под чужим ключом без единой ошибки в логах.
load_dotenv(_PROJECT_ROOT / ".env", override=True)

MODELS_ROOT = Path(
    os.environ.get("MODELS_DIR", _PROJECT_ROOT / "models")
).resolve()

DATA_DIR = Path(os.environ.get("DATA_DIR", _PROJECT_ROOT / "data")).resolve()

# Где физически лежат файлы моделей: local (файловая система) или s3 (облако, S3-совместимое).
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local").strip().lower()

# Локальный кэш скачанных из S3 объектов (для рендера превью, распаковки zip и т.п.).
CACHE_DIR = Path(os.environ.get("CACHE_DIR", DATA_DIR / "cache")).resolve()

# --- S3 / S3-совместимое хранилище (MinIO, Yandex, Cloudflare R2, ...) ---
# Пустой endpoint = обычный AWS S3. Для совместимых укажите endpoint_url.
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "").strip() or None
S3_REGION = os.environ.get("S3_REGION", "").strip() or None
S3_BUCKET = os.environ.get("S3_BUCKET", "").strip() or None
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "").strip() or None
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "").strip() or None
# Префикс (папка) внутри бакета — каталог моделей. Пути в БД хранятся относительно него.
S3_PREFIX = os.environ.get("S3_PREFIX", "").strip().strip("/")
# path (для MinIO/большинства совместимых) или virtual (AWS).
S3_ADDRESSING_STYLE = os.environ.get("S3_ADDRESSING_STYLE", "path").strip().lower()

PREVIEWS_DIR = Path(
    os.environ.get("PREVIEWS_DIR", DATA_DIR / "previews")
).resolve()

SQLITE_PATH = Path(
    os.environ.get("SQLITE_PATH", DATA_DIR / "catalog.sqlite")
).resolve()

# --- База данных каталога: sqlite (по умолчанию), postgres или mysql ---
DB_CONNECTION = os.environ.get("DB_CONNECTION", "sqlite").strip().lower()
DB_HOST = os.environ.get("DB_HOST", "127.0.0.1").strip()
_default_db_port = {"postgres": 5432, "mysql": 3306}.get(DB_CONNECTION, 3306)
DB_PORT = int(os.environ.get("DB_PORT", str(_default_db_port)))
DB_DATABASE = os.environ.get("DB_DATABASE", "model_catalog").strip()
DB_USERNAME = os.environ.get("DB_USERNAME", "").strip()
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

SCAN_INTERVAL_SEC = float(os.environ.get("SCAN_INTERVAL_SEC", "60"))


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().casefold() in {"1", "true", "yes", "on"}


# --- Фоновый воркер обогащения БД (теги + AI-описания + эмбеддинги) ---
# Запускается отдельным процессом/контейнером: `python -m app.worker`.
WORKER_INTERVAL_SEC = float(os.environ.get("WORKER_INTERVAL_SEC", "30"))
WORKER_TAG_BATCH = int(os.environ.get("WORKER_TAG_BATCH", "20"))
WORKER_DESCRIBE_BATCH = int(os.environ.get("WORKER_DESCRIBE_BATCH", "20"))
WORKER_DO_TAGS = _env_bool("WORKER_DO_TAGS", True)
WORKER_DO_DESCRIBE = _env_bool("WORKER_DO_DESCRIBE", True)
# Сколько запросов к OpenAI держать в полёте одновременно. Вызовы полностью
# сетевые (~4 с каждый), поэтому последовательная обработка пачки — главный
# тормоз обогащения. Упирается в rate limit аккаунта, а не в CPU.
WORKER_CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "8"))
# В приложении можно отключить встроенный сканер, если сканирование делает воркер.
RUN_SCANNER = _env_bool("RUN_SCANNER", True)

# Сколько превью обработать за один вызов run_scan_cycle (синк + очередь).
PREVIEW_BATCH_PER_CYCLE = int(os.environ.get("PREVIEW_BATCH_PER_CYCLE", "500"))

# Таймаут subprocess для одного превью (тяжёлые GLB/FBX).
PREVIEW_SUBPROCESS_TIMEOUT_SEC = int(os.environ.get("PREVIEW_SUBPROCESS_TIMEOUT_SEC", "900"))

# Размер картинки превью (меньше — быстрее и меньше память).
PREVIEW_PIXEL_SIZE = int(os.environ.get("PREVIEW_PIXEL_SIZE", "384"))

# Если граней больше порога — quadric decimation до target (ускоряет pyrender).
PREVIEW_SIMPLIFY_MAX_FACES = int(os.environ.get("PREVIEW_SIMPLIFY_MAX_FACES", "80000"))
PREVIEW_SIMPLIFY_TARGET_FACES = int(os.environ.get("PREVIEW_SIMPLIFY_TARGET_FACES", "28000"))

# Превью: browser (Chromium + скриншот WebGL), pyrender (trimesh), auto — сначала browser, иначе pyrender.
PREVIEW_ENGINE = os.environ.get("PREVIEW_ENGINE", "auto").strip().lower()

# Таймаут ожидания загрузки модели в браузере (мс).
PREVIEW_BROWSER_TIMEOUT_MS = int(os.environ.get("PREVIEW_BROWSER_TIMEOUT_MS", "240000"))

# Потолок виртуальной памяти на один процесс preview_worker (и наследников), МиБ.
# 0 = не ограничивать. Unix: RLIMIT_AS/RLIMIT_DATA (на процесс; у Chromium несколько
# процессов — суммарно RAM может быть выше; тогда уменьшите значение или используйте
# systemd/cgroup MemoryMax для всего сервиса). Windows не поддерживается.
PREVIEW_MAX_MEMORY_MB = int(os.environ.get("PREVIEW_MAX_MEMORY_MB", "10240"))

# Авто-теги по превью (OpenAI Vision). Ключ обязателен для /api/tags/auto.
OPENAI_TAG_MODEL = os.environ.get("OPENAI_TAG_MODEL", "gpt-4o-mini").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

# Дополнительно для официального SDK (прокси / совместимые эндпоинты).
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "").strip() or None
OPENAI_ORG_ID = os.environ.get("OPENAI_ORG_ID", "").strip() or None
# SOCKS5/HTTP-прокси для запросов к OpenAI (например "socks5h://127.0.0.1:18081"),
# когда прямой маршрут с этой машины до api.openai.com нестабилен.
OPENAI_PROXY_URL = os.environ.get("OPENAI_PROXY_URL", "").strip() or None

# --- AI-описания моделей (для семантического поиска похожих) ---
# Модель для генерации текстового описания по превью (vision).
OPENAI_DESCRIBE_MODEL = os.environ.get("OPENAI_DESCRIBE_MODEL", "gpt-4o-mini").strip()
# Модель эмбеддингов OpenAI и её размерность.
OPENAI_EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small").strip()
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1536"))

# --- Qdrant (векторная база для поиска похожих моделей) ---
# Полный URL имеет приоритет; иначе собирается из host/port.
QDRANT_URL = os.environ.get("QDRANT_URL", "").strip() or None
QDRANT_HOST = os.environ.get("QDRANT_HOST", "127.0.0.1").strip()
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "").strip() or None
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "models").strip()

# --- Авторизация витрины ---
# Пустой логин или пароль = авторизация выключена (локальная разработка).
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "").strip()
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
# Секрет подписи cookie. Если не задан — выводится из пароля (сессии переживают
# рестарт, но инвалидируются при смене пароля, что и требуется).
AUTH_SECRET = os.environ.get("AUTH_SECRET", "").strip()
AUTH_SESSION_TTL_SEC = int(os.environ.get("AUTH_SESSION_TTL_SEC", str(30 * 24 * 3600)))

# Группы запуска для внешних клиентов API (JSON: id, filters, unit_main).
LAUNCH_GROUPS_PATH = Path(
    os.environ.get("LAUNCH_GROUPS_PATH", DATA_DIR / "launch_groups.json")
).resolve()

# Базовый URL для абсолютных ссылок в ответах API (например http://127.0.0.1:8000).
API_BASE_URL = os.environ.get("API_BASE_URL", "").strip().rstrip("/") or None

MODEL_EXTENSIONS = {".fbx", ".glb", ".gltf", ".usdz", ".flb", ".obj", ".stl"}

PREVIEW_EXTENSIONS = {".glb", ".gltf", ".fbx"}

# Картинки, которые считаются готовым превью рядом с моделью (ingest ищет пару
# «модель + одноимённая картинка»).
INGEST_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# --- Ingest: заливка каталога с локального диска в хранилище ---
# Корень, который обходит `python -m app.ingest` (папка = модель + превью).
INGEST_ROOT = Path(
    os.environ.get("INGEST_ROOT", MODELS_ROOT)
).resolve()
# Параллельные заливки файлов в S3 (IO-bound, не CPU).
INGEST_WORKERS = int(os.environ.get("INGEST_WORKERS", "4"))
# Файлы крупнее порога заливаются multipart'ом самим boto3; здесь — защита от
# случайного затаскивания гигантских архивов, 0 = без ограничения.
INGEST_MAX_FILE_MB = int(os.environ.get("INGEST_MAX_FILE_MB", "0"))
