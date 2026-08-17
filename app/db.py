import sqlite3
import threading
import time
from contextlib import contextmanager
from app.paths import (
    DB_CONNECTION,
    DB_DATABASE,
    DB_HOST,
    DB_PASSWORD,
    DB_PORT,
    DB_USERNAME,
    PREVIEWS_DIR,
    SQLITE_PATH,
)

IS_MYSQL = DB_CONNECTION == "mysql"
IS_POSTGRES = DB_CONNECTION == "postgres"
IS_SERVER_DB = IS_MYSQL or IS_POSTGRES


class _NoLock:
    """Заглушка для MySQL: сервер сам управляет конкурентным доступом, а глобальный
    Python-lock только сериализует API-чтения за медленными записями сканера."""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


# SQLite — единственный писатель, нужен глобальный лок; серверные БД — параллелизм на сервере.
_lock = threading.Lock() if not IS_SERVER_DB else _NoLock()
# COLLATE для регистронезависимой сортировки: в MySQL/PostgreSQL коллация CI или отдельная логика.
_CI = "" if IS_SERVER_DB else "COLLATE NOCASE"

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    path TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    preview_file TEXT,
    preview_source TEXT,
    preview_status TEXT NOT NULL DEFAULT 'none',
    preview_error TEXT,
    content_adult INTEGER,
    content_nudity INTEGER,
    content_violence INTEGER,
    content_horror INTEGER,
    content_gore INTEGER,
    content_sensitive_tags TEXT,
    safety_checked_at REAL,
    first_seen REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_preview_status ON assets(preview_status);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_tags (
    path TEXT NOT NULL,
    tag TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at REAL NOT NULL,
    PRIMARY KEY (path, tag)
);
CREATE INDEX IF NOT EXISTS idx_asset_tags_tag ON asset_tags(tag);
CREATE INDEX IF NOT EXISTS idx_asset_tags_path ON asset_tags(path);
"""

# MySQL: полная схема (все колонки сразу). path/tag ограничены по длине под лимит
# индекса InnoDB (utf8mb4, PK ≤ 3072 байт). `key` — зарезервированное слово → бэктики.
MYSQL_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS assets (
        path VARCHAR(600) NOT NULL PRIMARY KEY,
        name TEXT NOT NULL,
        ext VARCHAR(32) NOT NULL,
        size BIGINT NOT NULL,
        mtime DOUBLE NOT NULL,
        preview_file TEXT,
        preview_source VARCHAR(32),
        preview_status VARCHAR(16) NOT NULL DEFAULT 'none',
        preview_error TEXT,
        blend_path TEXT,
        content_adult TINYINT,
        content_nudity TINYINT,
        content_violence TINYINT,
        content_horror TINYINT,
        content_gore TINYINT,
        content_sensitive_tags TEXT,
        safety_checked_at DOUBLE,
        description TEXT,
        description_source VARCHAR(32),
        described_at DOUBLE,
        embedded_at DOUBLE,
        first_seen DOUBLE NOT NULL,
        updated_at DOUBLE NOT NULL,
        INDEX idx_assets_preview_status (preview_status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        `key` VARCHAR(190) NOT NULL PRIMARY KEY,
        `value` TEXT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_tags (
        path VARCHAR(600) NOT NULL,
        tag VARCHAR(150) NOT NULL,
        source VARCHAR(32) NOT NULL DEFAULT 'manual',
        created_at DOUBLE NOT NULL,
        PRIMARY KEY (path, tag),
        INDEX idx_asset_tags_tag (tag),
        INDEX idx_asset_tags_path (path)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
    """,
]
# PostgreSQL: полная схема (аналог MySQL, отдельные индексы).
POSTGRES_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS assets (
        path VARCHAR(600) NOT NULL PRIMARY KEY,
        name TEXT NOT NULL,
        ext VARCHAR(32) NOT NULL,
        size BIGINT NOT NULL,
        mtime DOUBLE PRECISION NOT NULL,
        preview_file TEXT,
        preview_source VARCHAR(32),
        preview_status VARCHAR(16) NOT NULL DEFAULT 'none',
        preview_error TEXT,
        blend_path TEXT,
        content_adult SMALLINT,
        content_nudity SMALLINT,
        content_violence SMALLINT,
        content_horror SMALLINT,
        content_gore SMALLINT,
        content_sensitive_tags TEXT,
        safety_checked_at DOUBLE PRECISION,
        description TEXT,
        description_source VARCHAR(32),
        described_at DOUBLE PRECISION,
        embedded_at DOUBLE PRECISION,
        first_seen DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_assets_preview_status ON assets(preview_status)",
    """
    CREATE TABLE IF NOT EXISTS meta (
        "key" VARCHAR(190) NOT NULL PRIMARY KEY,
        "value" TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS asset_tags (
        path VARCHAR(600) NOT NULL,
        tag VARCHAR(150) NOT NULL,
        source VARCHAR(32) NOT NULL DEFAULT 'manual',
        created_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (path, tag)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_asset_tags_tag ON asset_tags(tag)",
    "CREATE INDEX IF NOT EXISTS idx_asset_tags_path ON asset_tags(path)",
]
# utf8mb4_bin / PostgreSQL text: побайтовое сравнение — пути и теги сопоставляются точно (иначе
# 'Model' == 'model' и NFC/NFD-варианты акцентов схлопываются в один PK).


class _Row:
    """Строка, поддерживающая доступ и по индексу (r[0]), и по имени (r["path"]),
    и dict(r) — как sqlite3.Row, чтобы код работал без изменений на обоих движках."""

    __slots__ = ("_v", "_c", "_map")

    def __init__(self, values, cols):
        self._v = values
        self._c = cols
        self._map = None

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._v[k]
        if self._map is None:
            self._map = dict(zip(self._c, self._v))
        return self._map[k]

    def keys(self):
        return list(self._c)

    def __iter__(self):
        # как sqlite3.Row — поддержка распаковки `a, b = row`
        return iter(self._v)

    def __len__(self):
        return len(self._v)


class _ServerConn:
    """Обёртка над pymysql/psycopg2 с интерфейсом как у sqlite3.Connection (execute/commit/close)
    и трансляцией плейсхолдеров ? → %s."""

    def __init__(self, raw, *, postgres: bool = False):
        self._raw = raw
        self._postgres = postgres

    def _adapt_sql(self, sql: str) -> str:
        sql = sql.replace("?", "%s")
        if self._postgres:
            sql = sql.replace("`", '"')
        return sql

    def execute(self, sql, params=()):
        cur = self._raw.cursor()
        cur.execute(self._adapt_sql(sql), params or None)
        return _ServerCursor(cur)

    def executemany(self, sql, seq):
        seq = list(seq)
        if not seq:
            return None
        cur = self._raw.cursor()
        cur.executemany(self._adapt_sql(sql), seq)
        return _ServerCursor(cur)

    def executescript(self, sql):
        for stmt in filter(str.strip, sql.split(";")):
            self._raw.cursor().execute(stmt)

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._raw.commit()
        else:
            self._raw.rollback()


class _ServerCursor:
    def __init__(self, cur):
        self._cur = cur
        self._cols = [d[0] for d in cur.description] if cur.description else []

    def fetchone(self):
        row = self._cur.fetchone()
        return _Row(row, self._cols) if row is not None else None

    def fetchall(self):
        return [_Row(r, self._cols) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


# Совместимость имён (если где-то ссылались).
_MySQLConn = _ServerConn
_MySQLCursor = _ServerCursor


def _connect_server():
    if IS_MYSQL:
        import pymysql

        raw = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USERNAME,
            password=DB_PASSWORD,
            database=DB_DATABASE,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=10,
            # Симметрично PostgreSQL: не висеть вечно на оборвавшемся соединении.
            read_timeout=120,
            write_timeout=120,
        )
        return _ServerConn(raw, postgres=False)
    import psycopg2

    raw = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USERNAME,
        password=DB_PASSWORD,
        dbname=DB_DATABASE,
        connect_timeout=10,
        # БД ходит через SSH-туннель, который периодически рвётся. Без keepalive
        # сокет умирает молча, и процесс висит на нём десятками минут, пока
        # туннель не поднимется. С этими параметрами обрыв виден примерно за минуту.
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
        # Страховка от запроса, зависшего уже на стороне сервера.
        options="-c statement_timeout=120000",
    )
    raw.autocommit = False
    return _ServerConn(raw, postgres=True)


def _connect():
    if IS_SERVER_DB:
        return _connect_server()
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SQLITE_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Несколько процессов (app + worker) пишут в одну БД — ждём снятия блокировки.
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# Колонки, которые дообавляются к `assets` миграцией: имя -> (sqlite, mysql, postgres).
# Держим их здесь, а не только в CREATE TABLE: боевые БД уже созданы, а
# `CREATE TABLE IF NOT EXISTS` новые колонки не добавляет.
_ASSET_COLUMNS: dict[str, tuple[str, str, str]] = {
    "blend_path": ("TEXT", "TEXT", "TEXT"),
    "preview_source": ("TEXT", "VARCHAR(32)", "VARCHAR(32)"),
    "content_adult": ("INTEGER", "TINYINT", "SMALLINT"),
    "content_nudity": ("INTEGER", "TINYINT", "SMALLINT"),
    "content_violence": ("INTEGER", "TINYINT", "SMALLINT"),
    "content_horror": ("INTEGER", "TINYINT", "SMALLINT"),
    "content_gore": ("INTEGER", "TINYINT", "SMALLINT"),
    "content_sensitive_tags": ("TEXT", "TEXT", "TEXT"),
    "safety_checked_at": ("REAL", "DOUBLE", "DOUBLE PRECISION"),
    "description": ("TEXT", "TEXT", "TEXT"),
    "description_source": ("TEXT", "VARCHAR(32)", "VARCHAR(32)"),
    "described_at": ("REAL", "DOUBLE", "DOUBLE PRECISION"),
    "embedded_at": ("REAL", "DOUBLE", "DOUBLE PRECISION"),
    # --- ingest: происхождение и дедупликация ---
    # sha256 файла модели: единственный надёжный признак дубля, когда одна и та же
    # модель лежит в разных папках/категориях под разными именами.
    "content_hash": ("TEXT", "VARCHAR(64)", "VARCHAR(64)"),
    "dir_key": ("TEXT", "TEXT", "TEXT"),
    "local_dir": ("TEXT", "TEXT", "TEXT"),
    "source_url": ("TEXT", "TEXT", "TEXT"),
    "preview_key": ("TEXT", "TEXT", "TEXT"),
    "ingested_at": ("REAL", "DOUBLE", "DOUBLE PRECISION"),
    # --- классификация ---
    "category": ("TEXT", "VARCHAR(32)", "VARCHAR(32)"),
    "collection": ("TEXT", "TEXT", "TEXT"),
    "age_rating": ("TEXT", "VARCHAR(16)", "VARCHAR(16)"),
    "kid_friendly": ("INTEGER", "TINYINT", "SMALLINT"),
    "nsfw": ("INTEGER", "TINYINT", "SMALLINT"),
    "classified_at": ("REAL", "DOUBLE", "DOUBLE PRECISION"),
    # --- метаданные геометрии ---
    "vertex_count": ("INTEGER", "BIGINT", "BIGINT"),
    "face_count": ("INTEGER", "BIGINT", "BIGINT"),
    "mesh_count": ("INTEGER", "INT", "INTEGER"),
    "material_count": ("INTEGER", "INT", "INTEGER"),
    "texture_count": ("INTEGER", "INT", "INTEGER"),
    "animation_count": ("INTEGER", "INT", "INTEGER"),
    "has_rig": ("INTEGER", "TINYINT", "SMALLINT"),
    "bbox_x": ("REAL", "DOUBLE", "DOUBLE PRECISION"),
    "bbox_y": ("REAL", "DOUBLE", "DOUBLE PRECISION"),
    "bbox_z": ("REAL", "DOUBLE", "DOUBLE PRECISION"),
    "meta_json": ("TEXT", "TEXT", "TEXT"),
    # --- пользовательская оценка ---
    # Ручной рейтинг 1–5 звёзд; NULL = не оценивали (это не то же самое, что 0).
    "rating": ("INTEGER", "TINYINT", "SMALLINT"),
    "rated_at": ("REAL", "DOUBLE", "DOUBLE PRECISION"),
}

_EXTRA_INDEXES = (
    ("idx_assets_content_hash", "assets(content_hash)"),
    ("idx_assets_category", "assets(category)"),
    ("idx_assets_age_rating", "assets(age_rating)"),
    ("idx_assets_nsfw", "assets(nsfw)"),
    ("idx_assets_kid_friendly", "assets(kid_friendly)"),
    ("idx_assets_ext", "assets(ext)"),
    ("idx_assets_animation_count", "assets(animation_count)"),
    ("idx_assets_rating", "assets(rating)"),
)


def _assets_table_exists(conn) -> bool:
    if IS_MYSQL:
        row = conn.execute(
            "SELECT 1 FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'assets'"
        ).fetchone()
    elif IS_POSTGRES:
        row = conn.execute("SELECT to_regclass('assets')").fetchone()
        return bool(row and row[0])
    else:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'assets'"
        ).fetchone()
    return bool(row)


def _existing_table_columns(conn, table: str) -> set[str]:
    if IS_MYSQL:
        cur = conn.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?",
            (table,),
        )
    elif IS_POSTGRES:
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        )
    else:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in cur.fetchall()}
    return {str(r[0]) for r in cur.fetchall()}


def _existing_asset_columns(conn) -> set[str]:
    return _existing_table_columns(conn, "assets")


def _ensure_asset_columns(conn) -> None:
    cols = _existing_asset_columns(conn)
    idx = 1 if IS_MYSQL else (2 if IS_POSTGRES else 0)
    for name, types in _ASSET_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE assets ADD COLUMN {name} {types[idx]}")


def _existing_index_names(conn) -> set[str]:
    if IS_MYSQL:
        cur = conn.execute(
            "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'assets'"
        )
    elif IS_POSTGRES:
        cur = conn.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = 'assets'"
        )
    else:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'assets'"
        )
    return {str(r[0]) for r in cur.fetchall()}


def _ensure_extra_indexes(conn) -> None:
    # Сверяемся со словарём БД до DDL: даже `CREATE INDEX IF NOT EXISTS` берёт
    # блокировку таблицы и на живой заливке ловит deadlock со вставками.
    existing = _existing_index_names(conn)
    for name, target in _EXTRA_INDEXES:
        if name in existing:
            continue
        conn.execute(f"CREATE INDEX {name} ON {target}")


def init_db() -> None:
    with _lock:
        conn = _connect()
        try:
            # Даже `CREATE TABLE/INDEX IF NOT EXISTS` берёт блокировку таблицы и на
            # работающих воркерах ловит deadlock со вставками. Поэтому на уже
            # созданной схеме DDL не выполняем вовсе — только досоздаём недостающее.
            if not _assets_table_exists(conn):
                if IS_MYSQL:
                    for stmt in MYSQL_SCHEMA:
                        conn.execute(stmt)
                elif IS_POSTGRES:
                    for stmt in POSTGRES_SCHEMA:
                        conn.execute(stmt)
                else:
                    conn.executescript(SCHEMA)
            _ensure_asset_columns(conn)
            _ensure_extra_indexes(conn)
            _ensure_collection_tables(conn)
            _ensure_api_key_tables(conn)
            _ensure_api_key_columns(conn)
            _ensure_billing_tables(conn)
            _seed_billing_plans(conn)
            conn.commit()
        finally:
            conn.close()


@contextmanager
def write_transaction():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def list_assets() -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                f"SELECT * FROM assets ORDER BY path {_CI}"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def get_catalog_meta() -> dict:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT `value` FROM meta WHERE `key` = 'last_full_scan_at'"
            ).fetchone()
            last = float(row[0]) if row else None
            pending = conn.execute(
                "SELECT COUNT(*) FROM assets WHERE preview_status = 'pending'"
            ).fetchone()[0]
            cur = conn.execute(
                "SELECT preview_status, COUNT(*) FROM assets GROUP BY preview_status"
            )
            by_status = {r[0]: int(r[1]) for r in cur.fetchall()}
        finally:
            conn.close()
    return {
        "last_full_scan_at": last,
        "pending_previews": int(pending),
        "preview_by_status": by_status,
    }


def all_paths(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT path FROM assets")
    return {r[0] for r in cur.fetchall()}


def load_existing_index(conn) -> dict[str, dict]:
    """Все существующие модели одним запросом (для быстрой сверки при скане).

    Возвращает path -> {size, mtime, preview_source, blend_path, preview_file}.
    Заменяет per-row get_row в цикле синхронизации (критично для remote MySQL).
    """
    cur = conn.execute(
        "SELECT path, size, mtime, preview_source, blend_path, preview_file FROM assets"
    )
    out: dict[str, dict] = {}
    for r in cur.fetchall():
        out[r[0]] = {
            "size": r[1],
            "mtime": r[2],
            "preview_source": r[3],
            "blend_path": r[4],
            "preview_file": r[5],
        }
    return out


def insert_assets_bulk(conn, rows: list[tuple]) -> None:
    """Пакетная вставка новых моделей (executemany, чанками).

    rows: (path, name, ext, size, mtime, preview_status, first_seen, updated_at, blend_path).
    """
    if not rows:
        return
    # Защита от точных дублей пути в одной пачке (например если источник отдал
    # файл дважды) — иначе нарушение PRIMARY KEY роняет весь батч.
    seen: set = set()
    deduped: list[tuple] = []
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        deduped.append(r)
    rows = deduped
    # Все значения — плейсхолдеры (без литеральных NULL): иначе pymysql.executemany
    # не распознаёт INSERT...VALUES и шлёт по одной строке (сотни round-trip'ов).
    # preview_file и preview_error передаются как None прямо в rows.
    sql = (
        "INSERT INTO assets "
        "(path, name, ext, size, mtime, preview_file, preview_status, preview_error, first_seen, updated_at, blend_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    chunk = 1000
    for i in range(0, len(rows), chunk):
        conn.executemany(sql, rows[i : i + chunk])


def get_row(conn: sqlite3.Connection, path: str) -> dict | None:
    row = conn.execute("SELECT * FROM assets WHERE path = ?", (path,)).fetchone()
    return dict(row) if row else None


def insert_asset(
    conn: sqlite3.Connection,
    *,
    path: str,
    name: str,
    ext: str,
    size: int,
    mtime: float,
    preview_status: str,
    now: float,
    blend_path: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO assets (path, name, ext, size, mtime, preview_file, preview_status, preview_error, first_seen, updated_at, blend_path)
        VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?)
        """,
        (path, name, ext, size, mtime, preview_status, now, now, blend_path),
    )


def update_asset_meta(
    conn: sqlite3.Connection,
    *,
    path: str,
    name: str,
    ext: str,
    size: int,
    mtime: float,
    preview_status: str,
    clear_preview: bool,
    now: float,
    blend_path: str | None = None,
) -> None:
    if clear_preview:
        conn.execute(
            """
            UPDATE assets SET
                name = ?, ext = ?, size = ?, mtime = ?,
                preview_file = NULL, preview_source = NULL,
                preview_status = ?, preview_error = NULL,
                content_adult = NULL, content_nudity = NULL,
                content_violence = NULL, content_horror = NULL,
                content_gore = NULL, content_sensitive_tags = NULL,
                safety_checked_at = NULL,
                updated_at = ?, blend_path = ?
            WHERE path = ?
            """,
            (name, ext, size, mtime, preview_status, now, blend_path, path),
        )
    else:
        conn.execute(
            """
            UPDATE assets SET name = ?, ext = ?, size = ?, mtime = ?, updated_at = ?, blend_path = ?
            WHERE path = ?
            """,
            (name, ext, size, mtime, now, blend_path, path),
        )


def update_blend_path_only(conn: sqlite3.Connection, path: str, blend_path: str | None, now: float) -> None:
    conn.execute(
        "UPDATE assets SET blend_path = ?, updated_at = ? WHERE path = ?",
        (blend_path, now, path),
    )


def delete_asset(conn: sqlite3.Connection, path: str) -> str | None:
    row = conn.execute(
        "SELECT preview_file FROM assets WHERE path = ?", (path,)
    ).fetchone()
    preview_file = row[0] if row else None
    conn.execute("DELETE FROM asset_tags WHERE path = ?", (path,))
    conn.execute("DELETE FROM assets WHERE path = ?", (path,))
    return preview_file


def set_preview_result(
    conn: sqlite3.Connection,
    path: str,
    *,
    preview_file: str | None,
    status: str,
    error: str | None,
    now: float,
    preview_source: str | None = None,
) -> None:
    source = preview_source if status == "ok" and preview_file else None
    conn.execute(
        """
        UPDATE assets SET
            preview_file = ?,
            preview_source = ?,
            preview_status = ?,
            preview_error = ?,
            content_adult = NULL,
            content_nudity = NULL,
            content_violence = NULL,
            content_horror = NULL,
            content_gore = NULL,
            content_sensitive_tags = NULL,
            safety_checked_at = NULL,
            updated_at = ?
        WHERE path = ?
        """,
        (preview_file, source, status, error, now, path),
    )


def fetch_pending_preview_paths(limit: int = 2) -> list[str]:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT path FROM assets WHERE preview_status = 'pending' ORDER BY updated_at LIMIT ?",
                (limit,),
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()


def fetch_asset_for_preview(path: str) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT path, name, ext, size, mtime FROM assets WHERE path = ?",
                (path,),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def set_last_scan_time(conn, ts: float) -> None:
    if IS_MYSQL:
        conn.execute(
            "INSERT INTO meta (`key`, `value`) VALUES ('last_full_scan_at', ?) AS new "
            "ON DUPLICATE KEY UPDATE `value` = new.`value`",
            (str(ts),),
        )
    elif IS_POSTGRES:
        conn.execute(
            """
            INSERT INTO meta ("key", "value") VALUES ('last_full_scan_at', ?)
            ON CONFLICT ("key") DO UPDATE SET "value" = EXCLUDED."value"
            """,
            (str(ts),),
        )
    else:
        conn.execute(
            """
            INSERT INTO meta (`key`, `value`) VALUES ('last_full_scan_at', ?)
            ON CONFLICT(`key`) DO UPDATE SET `value` = excluded.`value`
            """,
            (str(ts),),
        )


def delete_tags_for_source(conn: sqlite3.Connection, path: str, source: str) -> None:
    conn.execute(
        "DELETE FROM asset_tags WHERE path = ? AND source = ?", (path, source)
    )


def add_tags(
    conn: sqlite3.Connection,
    path: str,
    tags: list[str],
    source: str,
    now: float,
) -> None:
    for t in tags:
        if not t:
            continue
        if source == "openai":
            # Не перетираем существующий тег (в т.ч. ручной).
            if IS_MYSQL:
                conn.execute(
                    "INSERT IGNORE INTO asset_tags (path, tag, source, created_at) VALUES (?, ?, ?, ?)",
                    (path, t, source, now),
                )
            elif IS_POSTGRES:
                conn.execute(
                    """
                    INSERT INTO asset_tags (path, tag, source, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (path, tag) DO NOTHING
                    """,
                    (path, t, source, now),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO asset_tags (path, tag, source, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(path, tag) DO NOTHING
                    """,
                    (path, t, source, now),
                )
        else:
            if IS_MYSQL:
                conn.execute(
                    "INSERT INTO asset_tags (path, tag, source, created_at) VALUES (?, ?, ?, ?) AS new "
                    "ON DUPLICATE KEY UPDATE source = new.source, created_at = new.created_at",
                    (path, t, source, now),
                )
            elif IS_POSTGRES:
                conn.execute(
                    """
                    INSERT INTO asset_tags (path, tag, source, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (path, tag) DO UPDATE SET
                        source = EXCLUDED.source,
                        created_at = EXCLUDED.created_at
                    """,
                    (path, t, source, now),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO asset_tags (path, tag, source, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(path, tag) DO UPDATE SET source = excluded.source, created_at = excluded.created_at
                    """,
                    (path, t, source, now),
                )


def set_content_safety(
    conn: sqlite3.Connection,
    path: str,
    *,
    adult: bool | None,
    nudity: bool | None,
    violence: bool | None,
    horror: bool | None,
    gore: bool | None,
    sensitive_tags_json: str,
    checked_at: float,
) -> None:
    def val(x: bool | None) -> int | None:
        if x is None:
            return None
        return 1 if x else 0

    conn.execute(
        """
        UPDATE assets SET
            content_adult = ?,
            content_nudity = ?,
            content_violence = ?,
            content_horror = ?,
            content_gore = ?,
            content_sensitive_tags = ?,
            safety_checked_at = ?,
            updated_at = ?
        WHERE path = ?
        """,
        (
            val(adult),
            val(nudity),
            val(violence),
            val(horror),
            val(gore),
            sensitive_tags_json,
            checked_at,
            checked_at,
            path,
        ),
    )


def get_tags_bulk(paths: list[str]) -> dict[str, list[dict]]:
    if not paths:
        return {}
    with _lock:
        conn = _connect()
        try:
            chunk = 400
            out: dict[str, list[dict]] = {p: [] for p in paths}
            for i in range(0, len(paths), chunk):
                part = paths[i : i + chunk]
                q = ",".join("?" * len(part))
                cur = conn.execute(
                    f"SELECT path, tag, source FROM asset_tags WHERE path IN ({q}) ORDER BY tag",
                    part,
                )
                for row in cur.fetchall():
                    p = row[0]
                    if p in out:
                        out[p].append({"tag": row[1], "source": row[2]})
            return out
        finally:
            conn.close()


def paths_matching_tags(
    any_tags: list[str] | None,
    all_tags: list[str] | None,
) -> set[str] | None:
    if not any_tags and not all_tags:
        return None
    with _lock:
        conn = _connect()
        try:
            result: set[str] | None = None
            if any_tags:
                low = list(dict.fromkeys([t.lower() for t in any_tags if t]))
                if not low:
                    return set()
                ph = ",".join("?" * len(low))
                cur = conn.execute(
                    f"SELECT DISTINCT path FROM asset_tags WHERE tag IN ({ph})",
                    low,
                )
                result = {r[0] for r in cur.fetchall()}
            if all_tags:
                low = list(dict.fromkeys([t.lower() for t in all_tags if t]))
                if not low:
                    return set()
                ph = ",".join("?" * len(low))
                n = len(low)
                cur = conn.execute(
                    f"""
                    SELECT path FROM asset_tags WHERE tag IN ({ph})
                    GROUP BY path
                    HAVING COUNT(DISTINCT tag) = ?
                    """,
                    (*low, n),
                )
                s_all = {r[0] for r in cur.fetchall()}
                result = s_all if result is None else result & s_all
            return result
        finally:
            conn.close()


def list_tag_counts() -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                SELECT tag, COUNT(*) AS c FROM asset_tags
                GROUP BY tag ORDER BY c DESC, tag {ci}
                """.format(ci=_CI)
            )
            return [{"tag": r[0], "count": int(r[1])} for r in cur.fetchall()]
        finally:
            conn.close()


def fetch_assets_for_vision_tagging(
    conn: sqlite3.Connection,
    *,
    limit: int,
    only_missing_openai: bool,
) -> list[dict]:
    if only_missing_openai:
        cur = conn.execute(
            """
            SELECT a.path, a.preview_file
            FROM assets a
            WHERE a.preview_status = 'ok'
              AND a.preview_file IS NOT NULL
              AND (
                a.safety_checked_at IS NULL
                OR NOT EXISTS (
                  SELECT 1 FROM asset_tags t
                  WHERE t.path = a.path AND t.source = 'openai'
                )
              )
            ORDER BY a.path
            LIMIT ?
            """,
            (limit,),
        )
    else:
        cur = conn.execute(
            """
            SELECT path, preview_file FROM assets
            WHERE preview_status = 'ok' AND preview_file IS NOT NULL
            ORDER BY path
            LIMIT ?
            """,
            (limit,),
        )
    return [{"path": r[0], "preview_file": r[1]} for r in cur.fetchall()]


def list_preview_assets_for_tagging(limit: int, only_missing_openai: bool) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            return fetch_assets_for_vision_tagging(
                conn, limit=limit, only_missing_openai=only_missing_openai
            )
        finally:
            conn.close()


def set_description(
    conn: sqlite3.Connection,
    path: str,
    *,
    description: str,
    source: str,
    now: float,
) -> None:
    conn.execute(
        """
        UPDATE assets SET
            description = ?,
            description_source = ?,
            described_at = ?,
            updated_at = ?
        WHERE path = ?
        """,
        (description, source, now, now, path),
    )


def set_embedded_at(conn: sqlite3.Connection, path: str, ts: float) -> None:
    conn.execute("UPDATE assets SET embedded_at = ? WHERE path = ?", (ts, path))


def fetch_assets_for_describe(limit: int, only_missing: bool) -> list[dict]:
    """Модели с готовым превью, которым нужно AI-описание."""
    with _lock:
        conn = _connect()
        try:
            if only_missing:
                cur = conn.execute(
                    """
                    SELECT path, name, preview_file, preview_key FROM assets
                    WHERE preview_status = 'ok' AND preview_file IS NOT NULL
                      AND (description IS NULL OR description = '')
                    ORDER BY path
                    LIMIT ?
                    """,
                    (limit,),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT path, name, preview_file, preview_key FROM assets
                    WHERE preview_status = 'ok' AND preview_file IS NOT NULL
                    ORDER BY path
                    LIMIT ?
                    """,
                    (limit,),
                )
            return [
                {"path": r[0], "name": r[1], "preview_file": r[2], "preview_key": r[3]}
                for r in cur.fetchall()
            ]
        finally:
            conn.close()


def fetch_assets_for_embedding(limit: int) -> list[dict]:
    """Модели с описанием, но ещё не отправленные (или устаревшие) в Qdrant."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                SELECT path, name FROM assets
                WHERE description IS NOT NULL AND description != ''
                  AND (embedded_at IS NULL OR embedded_at < described_at)
                ORDER BY path
                LIMIT ?
                """,
                (limit,),
            )
            return [{"path": r[0], "name": r[1]} for r in cur.fetchall()]
        finally:
            conn.close()


def get_asset_with_description(path: str) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT path, name, description FROM assets WHERE path = ?",
                (path,),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Ingest: дедупликация по контрольной сумме и регистрация залитых моделей
# --------------------------------------------------------------------------- #
def find_by_content_hash(conn, content_hash: str) -> dict | None:
    """Уже залитая модель с такой же sha256 (защита от дублей)."""
    row = conn.execute(
        "SELECT path, name, category, size FROM assets WHERE content_hash = ? LIMIT 1",
        (content_hash,),
    ).fetchone()
    return dict(row) if row else None


def content_hashes_present(hashes: list[str]) -> set[str]:
    """Какие из переданных sha256 уже есть в каталоге (пакетно, для ingest)."""
    if not hashes:
        return set()
    out: set[str] = set()
    with _lock:
        conn = _connect()
        try:
            chunk = 400
            for i in range(0, len(hashes), chunk):
                part = hashes[i : i + chunk]
                ph = ",".join("?" * len(part))
                cur = conn.execute(
                    f"SELECT content_hash FROM assets WHERE content_hash IN ({ph})", part
                )
                out.update(str(r[0]) for r in cur.fetchall() if r[0])
        finally:
            conn.close()
    return out


_INGEST_FIELDS = (
    "name",
    "ext",
    "size",
    "mtime",
    "content_hash",
    "category",
    "collection",
    "dir_key",
    "local_dir",
    "source_url",
    "preview_file",
    "preview_key",
    "preview_source",
    "preview_status",
    "vertex_count",
    "face_count",
    "mesh_count",
    "material_count",
    "texture_count",
    "animation_count",
    "has_rig",
    "bbox_x",
    "bbox_y",
    "bbox_z",
    "meta_json",
    "ingested_at",
    "updated_at",
)


def upsert_ingested_asset(conn, path: str, values: dict, now: float) -> None:
    """Вставляет или обновляет модель, залитую ingest'ом.

    Пишем ровно поля из _INGEST_FIELDS, чтобы повторный ingest не затирал уже
    полученные от AI описание, теги и классификацию.
    """
    payload = {k: values.get(k) for k in _INGEST_FIELDS}
    payload["updated_at"] = now
    payload["ingested_at"] = values.get("ingested_at", now)

    exists = conn.execute("SELECT 1 FROM assets WHERE path = ?", (path,)).fetchone()
    if exists:
        sets = ", ".join(f"{k} = ?" for k in _INGEST_FIELDS)
        conn.execute(
            f"UPDATE assets SET {sets} WHERE path = ?",
            (*[payload[k] for k in _INGEST_FIELDS], path),
        )
        return
    cols = ("path", "first_seen", *_INGEST_FIELDS)
    ph = ", ".join("?" * len(cols))
    conn.execute(
        f"INSERT INTO assets ({', '.join(cols)}) VALUES ({ph})",
        (path, now, *[payload[k] for k in _INGEST_FIELDS]),
    )


def set_classification(
    conn,
    path: str,
    *,
    category: str | None,
    age_rating: str | None,
    kid_friendly: bool | None,
    nsfw: bool | None,
    now: float,
) -> None:
    def flag(x: bool | None) -> int | None:
        return None if x is None else (1 if x else 0)

    conn.execute(
        """
        UPDATE assets SET
            category = ?, age_rating = ?, kid_friendly = ?, nsfw = ?,
            classified_at = ?, updated_at = ?
        WHERE path = ?
        """,
        (category, age_rating, flag(kid_friendly), flag(nsfw), now, now, path),
    )


def fetch_assets_for_classification(limit: int, only_missing: bool) -> list[dict]:
    """Модели с превью, которым ещё не проставлена категория/рейтинг."""
    with _lock:
        conn = _connect()
        try:
            where = "preview_status = 'ok' AND preview_file IS NOT NULL"
            if only_missing:
                where += " AND (classified_at IS NULL OR category IS NULL)"
            cur = conn.execute(
                f"""
                SELECT path, name, preview_file, collection, category
                FROM assets WHERE {where} ORDER BY path LIMIT ?
                """,
                (limit,),
            )
            return [
                {
                    "path": r[0],
                    "name": r[1],
                    "preview_file": r[2],
                    "collection": r[3],
                    "category": r[4],
                }
                for r in cur.fetchall()
            ]
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# Поиск с фильтрами на стороне БД (вместо выгрузки всего каталога в память)
# --------------------------------------------------------------------------- #
def _tag_subquery(tags: list[str], *, require_all: bool) -> tuple[str, list]:
    ph = ",".join("?" * len(tags))
    if require_all:
        return (
            f"SELECT path FROM asset_tags WHERE tag IN ({ph}) "
            f"GROUP BY path HAVING COUNT(DISTINCT tag) = ?",
            [*tags, len(tags)],
        )
    return (f"SELECT DISTINCT path FROM asset_tags WHERE tag IN ({ph})", list(tags))


def build_asset_filter(
    *,
    tags_any: list[str] | None = None,
    tags_all: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    categories: list[str] | None = None,
    ext: list[str] | None = None,
    name_contains: str | None = None,
    path_prefix: str | None = None,
    age_ratings: list[str] | None = None,
    kid_only: bool = False,
    exclude_nsfw: bool = False,
    only_with_preview: bool = False,
    animated: bool | None = None,
    rigged: bool | None = None,
    collection_id: int | None = None,
    min_rating: int | None = None,
    unrated_only: bool = False,
    paths_subset: list[str] | None = None,
) -> tuple[str, list]:
    """Собирает WHERE для таблицы assets (алиас `a`) и список параметров."""
    where: list[str] = ["1=1"]
    params: list = []

    if tags_any:
        sub, p = _tag_subquery(tags_any, require_all=False)
        where.append(f"a.path IN ({sub})")
        params += p
    if tags_all:
        sub, p = _tag_subquery(tags_all, require_all=True)
        where.append(f"a.path IN ({sub})")
        params += p
    if exclude_tags:
        ph = ",".join("?" * len(exclude_tags))
        where.append(
            f"NOT EXISTS (SELECT 1 FROM asset_tags t WHERE t.path = a.path AND t.tag IN ({ph}))"
        )
        params += exclude_tags
    if categories:
        ph = ",".join("?" * len(categories))
        where.append(f"a.category IN ({ph})")
        params += categories
    if ext:
        ph = ",".join("?" * len(ext))
        where.append(f"a.ext IN ({ph})")
        params += ext
    if name_contains:
        where.append("(LOWER(a.name) LIKE ? OR LOWER(a.path) LIKE ?)")
        needle = f"%{name_contains.lower()}%"
        params += [needle, needle]
    if path_prefix:
        where.append("a.path LIKE ?")
        params.append(f"{path_prefix}%")
    if age_ratings:
        ph = ",".join("?" * len(age_ratings))
        where.append(f"(a.age_rating IN ({ph}) OR a.age_rating IS NULL)")
        params += age_ratings
    if kid_only:
        where.append("a.kid_friendly = 1")
    if exclude_nsfw:
        # NULL = ещё не классифицировано: в безопасном режиме такие не показываем.
        where.append("a.nsfw = 0 AND (a.content_adult IS NULL OR a.content_adult = 0)")
    if only_with_preview:
        # Должно совпадать с _preview_url(): на S3-бэкенде PNG лежит не файлом
        # рядом с ingest, а ключом в хранилище. Проверка только по preview_file
        # выкидывала из выдачи вообще все модели, у которых превью есть.
        where.append(
            "((a.preview_status = 'ok' AND a.preview_file IS NOT NULL)"
            " OR a.preview_key IS NOT NULL)"
        )
    if animated is not None:
        # Признак берём из метаданных файла (клипы анимации), а не из тегов AI:
        # он точный и есть у модели сразу после заливки.
        where.append(
            "a.animation_count > 0" if animated
            else "(a.animation_count IS NULL OR a.animation_count = 0)"
        )
    if rigged is not None:
        where.append("a.has_rig = 1" if rigged else "(a.has_rig IS NULL OR a.has_rig = 0)")
    if collection_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM collection_items ci "
            "WHERE ci.path = a.path AND ci.collection_id = ?)"
        )
        params.append(collection_id)
    if min_rating is not None:
        where.append("a.rating >= ?")
        params.append(min_rating)
    if unrated_only:
        where.append("a.rating IS NULL")
    if paths_subset is not None:
        if not paths_subset:
            return "1=0", []
        ph = ",".join("?" * len(paths_subset))
        where.append(f"a.path IN ({ph})")
        params += paths_subset

    return " AND ".join(where), params


_SORTS = {
    "name": "LOWER(a.name) ASC",
    "path": "a.path ASC",
    "newest": "a.ingested_at DESC NULLS LAST, a.first_seen DESC",
    "oldest": "a.ingested_at ASC NULLS LAST, a.first_seen ASC",
    "size": "a.size DESC",
    "size_asc": "a.size ASC",
    "complexity": "a.face_count DESC NULLS LAST",
    "simplicity": "a.face_count ASC NULLS LAST",
    "rating": "a.rating DESC NULLS LAST, LOWER(a.name) ASC",
}


def set_rating(path: str, rating: int | None, now: float) -> None:
    """Ставит оценку 1–5; None снимает её."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE assets SET rating = ?, rated_at = ?, updated_at = ? WHERE path = ?",
                (rating, now if rating is not None else None, now, path),
            )
            conn.commit()
        finally:
            conn.close()


def _order_by(sort: str | None) -> str:
    clause = _SORTS.get((sort or "name").lower(), _SORTS["name"])
    if not IS_POSTGRES:
        # NULLS LAST есть только в PostgreSQL (и SQLite ≥3.30) — для MySQL убираем.
        if IS_MYSQL:
            clause = clause.replace(" NULLS LAST", "")
    return clause


def search_assets(
    *,
    limit: int = 60,
    offset: int = 0,
    sort: str | None = None,
    **filters,
) -> tuple[list[dict], int]:
    """Отфильтрованная страница каталога + общее число совпадений."""
    where, params = build_asset_filter(**filters)
    with _lock:
        conn = _connect()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM assets a WHERE {where}", params
            ).fetchone()[0]
            cur = conn.execute(
                f"SELECT a.* FROM assets a WHERE {where} "
                f"ORDER BY {_order_by(sort)} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    return rows, int(total)


def facet_counts(**filters) -> dict:
    """Счётчики по категориям, расширениям и тегам для текущего фильтра."""
    where, params = build_asset_filter(**filters)
    with _lock:
        conn = _connect()
        try:
            cats = conn.execute(
                f"SELECT COALESCE(a.category, 'unclassified') AS c, COUNT(*) "
                f"FROM assets a WHERE {where} GROUP BY c ORDER BY COUNT(*) DESC",
                params,
            ).fetchall()
            exts = conn.execute(
                f"SELECT a.ext, COUNT(*) FROM assets a WHERE {where} "
                f"GROUP BY a.ext ORDER BY COUNT(*) DESC",
                params,
            ).fetchall()
            tags = conn.execute(
                f"SELECT t.tag, COUNT(*) AS n FROM asset_tags t "
                f"JOIN assets a ON a.path = t.path WHERE {where} "
                f"GROUP BY t.tag ORDER BY n DESC LIMIT 200",
                params,
            ).fetchall()
            ratings = conn.execute(
                f"SELECT COALESCE(a.age_rating, 'unrated') AS r, COUNT(*) "
                f"FROM assets a WHERE {where} GROUP BY r ORDER BY COUNT(*) DESC",
                params,
            ).fetchall()
            flags = conn.execute(
                f"SELECT "
                f"SUM(CASE WHEN a.animation_count > 0 THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN a.has_rig = 1 THEN 1 ELSE 0 END), "
                f"COUNT(*) "
                f"FROM assets a WHERE {where}",
                params,
            ).fetchone()
        finally:
            conn.close()
    return {
        "categories": [{"category": r[0], "count": int(r[1])} for r in cats],
        "ext": [{"ext": r[0], "count": int(r[1])} for r in exts],
        "tags": [{"tag": r[0], "count": int(r[1])} for r in tags],
        "age_ratings": [{"age_rating": r[0], "count": int(r[1])} for r in ratings],
        "animated": {
            "yes": int(flags[0] or 0),
            "no": int(flags[2] or 0) - int(flags[0] or 0),
        },
        "rigged": {
            "yes": int(flags[1] or 0),
            "no": int(flags[2] or 0) - int(flags[1] or 0),
        },
    }


def list_category_counts() -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT COALESCE(category, 'unclassified') AS c, COUNT(*) AS n "
                "FROM assets GROUP BY c ORDER BY n DESC"
            )
            return [{"category": r[0], "count": int(r[1])} for r in cur.fetchall()]
        finally:
            conn.close()


def get_assets_bulk(paths: list[str]) -> dict[str, dict]:
    """Полные строки моделей по списку путей (для гидратации результатов Qdrant)."""
    if not paths:
        return {}
    out: dict[str, dict] = {}
    with _lock:
        conn = _connect()
        try:
            chunk = 300
            for i in range(0, len(paths), chunk):
                part = paths[i : i + chunk]
                ph = ",".join("?" * len(part))
                cur = conn.execute(f"SELECT * FROM assets WHERE path IN ({ph})", part)
                for r in cur.fetchall():
                    row = dict(r)
                    out[row["path"]] = row
        finally:
            conn.close()
    return out


# --------------------------------------------------------------------------- #
# Пользовательские подборки («избранное» под разные задачи)
# --------------------------------------------------------------------------- #
# Одна модель может лежать в любом числе подборок, поэтому это связь многие-ко-многим,
# а не поле у модели. Подборки живут отдельно от категорий: категория описывает,
# что это за объект, подборка — зачем он лично вам.
_COLLECTION_SCHEMA = {
    "sqlite": [
        """
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            color TEXT,
            created_at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS collection_items (
            collection_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            added_at REAL NOT NULL,
            PRIMARY KEY (collection_id, path)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_collection_items_path ON collection_items(path)",
    ],
    "postgres": [
        """
        CREATE TABLE IF NOT EXISTS collections (
            id SERIAL PRIMARY KEY,
            name VARCHAR(120) NOT NULL UNIQUE,
            color VARCHAR(16),
            created_at DOUBLE PRECISION NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS collection_items (
            collection_id INTEGER NOT NULL,
            path VARCHAR(600) NOT NULL,
            added_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (collection_id, path)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_collection_items_path ON collection_items(path)",
    ],
    "mysql": [
        """
        CREATE TABLE IF NOT EXISTS collections (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(120) NOT NULL UNIQUE,
            color VARCHAR(16),
            created_at DOUBLE NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS collection_items (
            collection_id INT NOT NULL,
            path VARCHAR(600) NOT NULL,
            added_at DOUBLE NOT NULL,
            PRIMARY KEY (collection_id, path),
            INDEX idx_collection_items_path (path)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    ],
}


def _ensure_collection_tables(conn) -> None:
    engine = "mysql" if IS_MYSQL else ("postgres" if IS_POSTGRES else "sqlite")
    for stmt in _COLLECTION_SCHEMA[engine]:
        conn.execute(stmt)


def list_collections() -> list[dict]:
    """Подборки со счётчиком моделей в каждой."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                SELECT c.id, c.name, c.color, c.created_at,
                       (SELECT COUNT(*) FROM collection_items i WHERE i.collection_id = c.id)
                FROM collections c
                ORDER BY c.name
                """
            )
            return [
                {
                    "id": int(r[0]),
                    "name": r[1],
                    "color": r[2],
                    "created_at": r[3],
                    "count": int(r[4]),
                }
                for r in cur.fetchall()
            ]
        finally:
            conn.close()


def create_collection(name: str, color: str | None, now: float) -> dict:
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT id, name, color FROM collections WHERE name = ?", (name,)
            ).fetchone()
            if existing:
                return {"id": int(existing[0]), "name": existing[1], "color": existing[2]}
            conn.execute(
                "INSERT INTO collections (name, color, created_at) VALUES (?, ?, ?)",
                (name, color, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id, name, color FROM collections WHERE name = ?", (name,)
            ).fetchone()
            return {"id": int(row[0]), "name": row[1], "color": row[2]}
        finally:
            conn.close()


def rename_collection(collection_id: int, name: str, color: str | None) -> bool:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE collections SET name = ?, color = ? WHERE id = ?",
                (name, color, collection_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()


def delete_collection(collection_id: int) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM collection_items WHERE collection_id = ?", (collection_id,)
            )
            conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
            conn.commit()
        finally:
            conn.close()


def set_collection_membership(collection_id: int, path: str, member: bool, now: float) -> None:
    with _lock:
        conn = _connect()
        try:
            if not member:
                conn.execute(
                    "DELETE FROM collection_items WHERE collection_id = ? AND path = ?",
                    (collection_id, path),
                )
            elif IS_MYSQL:
                conn.execute(
                    "INSERT IGNORE INTO collection_items (collection_id, path, added_at) "
                    "VALUES (?, ?, ?)",
                    (collection_id, path, now),
                )
            else:
                conn.execute(
                    "INSERT INTO collection_items (collection_id, path, added_at) "
                    "VALUES (?, ?, ?) ON CONFLICT (collection_id, path) DO NOTHING",
                    (collection_id, path, now),
                )
            conn.commit()
        finally:
            conn.close()


def collections_for_paths(paths: list[str]) -> dict[str, list[int]]:
    """path -> id подборок, в которых он состоит (для отметок в галерее)."""
    if not paths:
        return {}
    out: dict[str, list[int]] = {p: [] for p in paths}
    with _lock:
        conn = _connect()
        try:
            chunk = 400
            for i in range(0, len(paths), chunk):
                part = paths[i : i + chunk]
                ph = ",".join("?" * len(part))
                cur = conn.execute(
                    f"SELECT path, collection_id FROM collection_items WHERE path IN ({ph})",
                    part,
                )
                for r in cur.fetchall():
                    if r[0] in out:
                        out[r[0]].append(int(r[1]))
        finally:
            conn.close()
    return out


def paths_in_collection(collection_id: int) -> list[str]:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "SELECT path FROM collection_items WHERE collection_id = ?", (collection_id,)
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()


def unlink_preview_file(basename: str | None) -> None:
    if not basename or "/" in basename or "\\" in basename or basename.startswith("."):
        return
    p = (PREVIEWS_DIR / basename).resolve()
    try:
        p.relative_to(PREVIEWS_DIR.resolve())
    except ValueError:
        return
    if p.is_file():
        p.unlink()


# --------------------------------------------------------------------------- #
# Ключи сервисного API (/v1)
# --------------------------------------------------------------------------- #
_API_KEY_SCHEMA = {
    "sqlite": [
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            scopes TEXT NOT NULL,
            rate_limit_per_min INTEGER NOT NULL DEFAULT 120,
            created_at REAL NOT NULL,
            last_used_at REAL,
            request_count INTEGER NOT NULL DEFAULT 0,
            revoked_at REAL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)",
    ],
    "postgres": [
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id SERIAL PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            key_prefix VARCHAR(16) NOT NULL,
            key_hash VARCHAR(64) NOT NULL UNIQUE,
            scopes TEXT NOT NULL,
            rate_limit_per_min INTEGER NOT NULL DEFAULT 120,
            created_at DOUBLE PRECISION NOT NULL,
            last_used_at DOUBLE PRECISION,
            request_count BIGINT NOT NULL DEFAULT 0,
            revoked_at DOUBLE PRECISION
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)",
    ],
    "mysql": [
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            key_prefix VARCHAR(16) NOT NULL,
            key_hash VARCHAR(64) NOT NULL,
            scopes TEXT NOT NULL,
            rate_limit_per_min INT NOT NULL DEFAULT 120,
            created_at DOUBLE NOT NULL,
            last_used_at DOUBLE,
            request_count BIGINT NOT NULL DEFAULT 0,
            revoked_at DOUBLE,
            UNIQUE KEY uq_api_keys_hash (key_hash)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    ],
}


def _ensure_api_key_tables(conn) -> None:
    engine = "mysql" if IS_MYSQL else ("postgres" if IS_POSTGRES else "sqlite")
    for stmt in _API_KEY_SCHEMA[engine]:
        conn.execute(stmt)


def _ensure_api_key_columns(conn) -> None:
    cols = _existing_table_columns(conn, "api_keys")
    if "customer_id" not in cols:
        if IS_MYSQL:
            conn.execute("ALTER TABLE api_keys ADD COLUMN customer_id INT NULL")
        elif IS_POSTGRES:
            conn.execute("ALTER TABLE api_keys ADD COLUMN customer_id INTEGER")
        else:
            conn.execute("ALTER TABLE api_keys ADD COLUMN customer_id INTEGER")


def _api_key_row(r) -> dict:
    import json

    scopes_raw = r[4]
    try:
        scopes = json.loads(scopes_raw) if isinstance(scopes_raw, str) else list(scopes_raw or [])
    except (TypeError, ValueError):
        scopes = []
    customer_id = r[10] if len(r) > 10 else None
    return {
        "id": int(r[0]),
        "name": r[1],
        "key_prefix": r[2],
        "key_hash": r[3],
        "scopes": [str(s) for s in scopes],
        "rate_limit_per_min": int(r[5] or 0),
        "created_at": r[6],
        "last_used_at": r[7],
        "request_count": int(r[8] or 0),
        "revoked_at": r[9],
        "customer_id": int(customer_id) if customer_id is not None else None,
    }


_API_KEY_SELECT = (
    "SELECT id, name, key_prefix, key_hash, scopes, rate_limit_per_min, "
    "created_at, last_used_at, request_count, revoked_at, customer_id FROM api_keys"
)


def list_api_keys(*, include_revoked: bool = True) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            sql = _API_KEY_SELECT
            if not include_revoked:
                sql += " WHERE revoked_at IS NULL"
            sql += " ORDER BY created_at DESC"
            return [_api_key_row(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()


def get_api_key_by_hash(key_hash: str) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                _API_KEY_SELECT + " WHERE key_hash = ?", (key_hash,)
            ).fetchone()
        finally:
            conn.close()
    return _api_key_row(row) if row else None


def get_api_key(key_id: int) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(_API_KEY_SELECT + " WHERE id = ?", (key_id,)).fetchone()
        finally:
            conn.close()
    return _api_key_row(row) if row else None


def create_api_key(
    *,
    name: str,
    key_prefix: str,
    key_hash: str,
    scopes: list[str],
    rate_limit_per_min: int,
    now: float,
    customer_id: int | None = None,
) -> dict:
    import json

    scopes_json = json.dumps(list(scopes), ensure_ascii=False)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO api_keys
                    (name, key_prefix, key_hash, scopes, rate_limit_per_min, created_at, request_count, customer_id)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (name, key_prefix, key_hash, scopes_json, rate_limit_per_min, now, customer_id),
            )
            row = conn.execute(
                _API_KEY_SELECT + " WHERE key_hash = ?", (key_hash,)
            ).fetchone()
            conn.commit()
        finally:
            conn.close()
    if row is None:
        raise RuntimeError("api_keys insert did not return a row")
    return _api_key_row(row)


def count_active_api_keys(customer_id: int) -> int:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM api_keys WHERE customer_id = ? AND revoked_at IS NULL",
                (customer_id,),
            ).fetchone()
        finally:
            conn.close()
    return int(row[0] if row else 0)


def revoke_api_key(key_id: int, now: float) -> bool:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id FROM api_keys WHERE id = ? AND revoked_at IS NULL", (key_id,)
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ?", (now, key_id)
            )
            conn.commit()
        finally:
            conn.close()
    return True


def touch_api_key(key_id: int, now: float) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE api_keys SET last_used_at = ?, request_count = request_count + 1 WHERE id = ?",
                (now, key_id),
            )
            conn.commit()
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# Биллинг: тарифы, клиенты, подписки, счета
# --------------------------------------------------------------------------- #
def _billing_plan_sql(engine: str) -> list[str]:
    if engine == "postgres":
        return [
            """
            CREATE TABLE IF NOT EXISTS billing_plans (
                id SERIAL PRIMARY KEY,
                slug VARCHAR(64) NOT NULL UNIQUE,
                name VARCHAR(120) NOT NULL,
                description TEXT,
                price_cents INTEGER NOT NULL DEFAULT 0,
                currency VARCHAR(8) NOT NULL DEFAULT 'RUB',
                period VARCHAR(16) NOT NULL DEFAULT 'month',
                requests_per_period INTEGER NOT NULL DEFAULT 0,
                downloads_per_period INTEGER NOT NULL DEFAULT 0,
                bytes_per_period BIGINT NOT NULL DEFAULT 0,
                searches_per_period INTEGER NOT NULL DEFAULT 0,
                rate_limit_per_min INTEGER NOT NULL DEFAULT 60,
                max_api_keys INTEGER NOT NULL DEFAULT 1,
                max_page_size INTEGER NOT NULL DEFAULT 24,
                scopes TEXT NOT NULL,
                trial_days INTEGER NOT NULL DEFAULT 0,
                is_active SMALLINT NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS billing_customers (
                id SERIAL PRIMARY KEY,
                name VARCHAR(160) NOT NULL,
                email VARCHAR(190),
                notes TEXT,
                created_at DOUBLE PRECISION NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS billing_subscriptions (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                plan_id INTEGER NOT NULL,
                status VARCHAR(16) NOT NULL,
                auto_renew SMALLINT NOT NULL DEFAULT 1,
                period_start DOUBLE PRECISION NOT NULL,
                period_end DOUBLE PRECISION NOT NULL,
                usage_requests INTEGER NOT NULL DEFAULT 0,
                usage_downloads INTEGER NOT NULL DEFAULT 0,
                usage_bytes BIGINT NOT NULL DEFAULT 0,
                usage_searches INTEGER NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL,
                canceled_at DOUBLE PRECISION
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_billing_subs_customer ON billing_subscriptions(customer_id)",
            """
            CREATE TABLE IF NOT EXISTS billing_invoices (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                subscription_id INTEGER,
                plan_id INTEGER,
                amount_cents INTEGER NOT NULL,
                currency VARCHAR(8) NOT NULL DEFAULT 'RUB',
                status VARCHAR(16) NOT NULL,
                period_start DOUBLE PRECISION,
                period_end DOUBLE PRECISION,
                description TEXT,
                issued_at DOUBLE PRECISION NOT NULL,
                paid_at DOUBLE PRECISION,
                due_at DOUBLE PRECISION,
                yookassa_payment_id VARCHAR(64),
                yookassa_status VARCHAR(32),
                yookassa_confirmation_url TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_billing_invoices_customer ON billing_invoices(customer_id)",
        ]
    if engine == "mysql":
        return [
            """
            CREATE TABLE IF NOT EXISTS billing_plans (
                id INT AUTO_INCREMENT PRIMARY KEY,
                slug VARCHAR(64) NOT NULL,
                name VARCHAR(120) NOT NULL,
                description TEXT,
                price_cents INT NOT NULL DEFAULT 0,
                currency VARCHAR(8) NOT NULL DEFAULT 'RUB',
                period VARCHAR(16) NOT NULL DEFAULT 'month',
                requests_per_period INT NOT NULL DEFAULT 0,
                downloads_per_period INT NOT NULL DEFAULT 0,
                bytes_per_period BIGINT NOT NULL DEFAULT 0,
                searches_per_period INT NOT NULL DEFAULT 0,
                rate_limit_per_min INT NOT NULL DEFAULT 60,
                max_api_keys INT NOT NULL DEFAULT 1,
                max_page_size INT NOT NULL DEFAULT 24,
                scopes TEXT NOT NULL,
                trial_days INT NOT NULL DEFAULT 0,
                is_active TINYINT NOT NULL DEFAULT 1,
                sort_order INT NOT NULL DEFAULT 0,
                created_at DOUBLE NOT NULL,
                UNIQUE KEY uq_billing_plans_slug (slug)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS billing_customers (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(160) NOT NULL,
                email VARCHAR(190),
                notes TEXT,
                created_at DOUBLE NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS billing_subscriptions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                customer_id INT NOT NULL,
                plan_id INT NOT NULL,
                status VARCHAR(16) NOT NULL,
                auto_renew TINYINT NOT NULL DEFAULT 1,
                period_start DOUBLE NOT NULL,
                period_end DOUBLE NOT NULL,
                usage_requests INT NOT NULL DEFAULT 0,
                usage_downloads INT NOT NULL DEFAULT 0,
                usage_bytes BIGINT NOT NULL DEFAULT 0,
                usage_searches INT NOT NULL DEFAULT 0,
                created_at DOUBLE NOT NULL,
                canceled_at DOUBLE,
                INDEX idx_billing_subs_customer (customer_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS billing_invoices (
                id INT AUTO_INCREMENT PRIMARY KEY,
                customer_id INT NOT NULL,
                subscription_id INT,
                plan_id INT,
                amount_cents INT NOT NULL,
                currency VARCHAR(8) NOT NULL DEFAULT 'RUB',
                status VARCHAR(16) NOT NULL,
                period_start DOUBLE,
                period_end DOUBLE,
                description TEXT,
                issued_at DOUBLE NOT NULL,
                paid_at DOUBLE,
                due_at DOUBLE,
                yookassa_payment_id VARCHAR(64),
                yookassa_status VARCHAR(32),
                yookassa_confirmation_url TEXT,
                INDEX idx_billing_invoices_customer (customer_id),
                INDEX idx_billing_invoices_yk (yookassa_payment_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        ]
    return [
        """
        CREATE TABLE IF NOT EXISTS billing_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT,
            price_cents INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'RUB',
            period TEXT NOT NULL DEFAULT 'month',
            requests_per_period INTEGER NOT NULL DEFAULT 0,
            downloads_per_period INTEGER NOT NULL DEFAULT 0,
            bytes_per_period INTEGER NOT NULL DEFAULT 0,
            searches_per_period INTEGER NOT NULL DEFAULT 0,
            rate_limit_per_min INTEGER NOT NULL DEFAULT 60,
            max_api_keys INTEGER NOT NULL DEFAULT 1,
            max_page_size INTEGER NOT NULL DEFAULT 24,
            scopes TEXT NOT NULL,
            trial_days INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS billing_customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            notes TEXT,
            created_at REAL NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS billing_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            auto_renew INTEGER NOT NULL DEFAULT 1,
            period_start REAL NOT NULL,
            period_end REAL NOT NULL,
            usage_requests INTEGER NOT NULL DEFAULT 0,
            usage_downloads INTEGER NOT NULL DEFAULT 0,
            usage_bytes INTEGER NOT NULL DEFAULT 0,
            usage_searches INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            canceled_at REAL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_billing_subs_customer ON billing_subscriptions(customer_id)",
        """
        CREATE TABLE IF NOT EXISTS billing_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            subscription_id INTEGER,
            plan_id INTEGER,
            amount_cents INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'RUB',
            status TEXT NOT NULL,
            period_start REAL,
            period_end REAL,
            description TEXT,
            issued_at REAL NOT NULL,
            paid_at REAL,
            due_at REAL,
            yookassa_payment_id TEXT,
            yookassa_status TEXT,
            yookassa_confirmation_url TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_billing_invoices_customer ON billing_invoices(customer_id)",
    ]


def _ensure_billing_tables(conn) -> None:
    engine = "mysql" if IS_MYSQL else ("postgres" if IS_POSTGRES else "sqlite")
    for stmt in _billing_plan_sql(engine):
        conn.execute(stmt)
    if engine == "postgres":
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS billing_usage_daily (
                day VARCHAR(10) NOT NULL,
                customer_id INTEGER NOT NULL DEFAULT 0,
                key_id INTEGER NOT NULL DEFAULT 0,
                requests INTEGER NOT NULL DEFAULT 0,
                downloads INTEGER NOT NULL DEFAULT 0,
                bytes BIGINT NOT NULL DEFAULT 0,
                searches INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, customer_id, key_id)
            )
            """
        )
    elif engine == "mysql":
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS billing_usage_daily (
                day VARCHAR(10) NOT NULL,
                customer_id INT NOT NULL DEFAULT 0,
                key_id INT NOT NULL DEFAULT 0,
                requests INT NOT NULL DEFAULT 0,
                downloads INT NOT NULL DEFAULT 0,
                bytes BIGINT NOT NULL DEFAULT 0,
                searches INT NOT NULL DEFAULT 0,
                PRIMARY KEY (day, customer_id, key_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS billing_usage_daily (
                day TEXT NOT NULL,
                customer_id INTEGER NOT NULL DEFAULT 0,
                key_id INTEGER NOT NULL DEFAULT 0,
                requests INTEGER NOT NULL DEFAULT 0,
                downloads INTEGER NOT NULL DEFAULT 0,
                bytes INTEGER NOT NULL DEFAULT 0,
                searches INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, customer_id, key_id)
            )
            """
        )
    _ensure_invoice_yookassa_columns(conn)


def _ensure_invoice_yookassa_columns(conn) -> None:
    cols = _existing_table_columns(conn, "billing_invoices")
    specs = (
        ("yookassa_payment_id", "VARCHAR(64)", "TEXT"),
        ("yookassa_status", "VARCHAR(32)", "TEXT"),
        ("yookassa_confirmation_url", "TEXT", "TEXT"),
    )
    for name, sql_type, sqlite_type in specs:
        if name in cols:
            continue
        if IS_MYSQL or IS_POSTGRES:
            conn.execute(f"ALTER TABLE billing_invoices ADD COLUMN {name} {sql_type}")
        else:
            conn.execute(f"ALTER TABLE billing_invoices ADD COLUMN {name} {sqlite_type}")
    if not IS_MYSQL:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_billing_invoices_yk ON billing_invoices(yookassa_payment_id)"
        )


def _seed_billing_plans(conn) -> None:
    from app.billing import DEFAULT_PLANS
    import json

    count = conn.execute("SELECT COUNT(*) FROM billing_plans").fetchone()[0]
    if int(count or 0) > 0:
        return
    now = time.time()
    for plan in DEFAULT_PLANS:
        conn.execute(
            """
            INSERT INTO billing_plans (
                slug, name, description, price_cents, currency, period,
                requests_per_period, downloads_per_period, bytes_per_period,
                searches_per_period, rate_limit_per_min, max_api_keys, max_page_size,
                scopes, trial_days, is_active, sort_order, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                plan["slug"],
                plan["name"],
                plan["description"],
                plan["price_cents"],
                plan["currency"],
                plan["period"],
                plan["requests_per_period"],
                plan["downloads_per_period"],
                plan["bytes_per_period"],
                plan["searches_per_period"],
                plan["rate_limit_per_min"],
                plan["max_api_keys"],
                plan["max_page_size"],
                json.dumps(plan["scopes"], ensure_ascii=False),
                plan["trial_days"],
                plan["sort_order"],
                now,
            ),
        )


def _plan_row(r) -> dict:
    import json

    try:
        scopes = json.loads(r[14]) if isinstance(r[14], str) else list(r[14] or [])
    except (TypeError, ValueError):
        scopes = []
    return {
        "id": int(r[0]),
        "slug": r[1],
        "name": r[2],
        "description": r[3],
        "price_cents": int(r[4] or 0),
        "currency": r[5],
        "period": r[6],
        "requests_per_period": int(r[7] or 0),
        "downloads_per_period": int(r[8] or 0),
        "bytes_per_period": int(r[9] or 0),
        "searches_per_period": int(r[10] or 0),
        "rate_limit_per_min": int(r[11] or 0),
        "max_api_keys": int(r[12] or 0),
        "max_page_size": int(r[13] or 0),
        "scopes": [str(s) for s in scopes],
        "trial_days": int(r[15] or 0),
        "is_active": bool(int(r[16] or 0)),
        "sort_order": int(r[17] or 0),
        "created_at": r[18],
    }


_PLAN_SELECT = (
    "SELECT id, slug, name, description, price_cents, currency, period, "
    "requests_per_period, downloads_per_period, bytes_per_period, searches_per_period, "
    "rate_limit_per_min, max_api_keys, max_page_size, scopes, trial_days, is_active, "
    "sort_order, created_at FROM billing_plans"
)


def list_billing_plans(*, active_only: bool = False) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            sql = _PLAN_SELECT
            if active_only:
                sql += " WHERE is_active = 1"
            sql += " ORDER BY sort_order, name"
            return [_plan_row(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()


def get_billing_plan(plan_id: int) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(_PLAN_SELECT + " WHERE id = ?", (plan_id,)).fetchone()
        finally:
            conn.close()
    return _plan_row(row) if row else None


def upsert_billing_plan(data: dict, now: float) -> dict:
    import json

    scopes_json = json.dumps(list(data.get("scopes") or []), ensure_ascii=False)
    fields = (
        data["slug"],
        data["name"],
        data.get("description") or "",
        int(data.get("price_cents") or 0),
        data.get("currency") or "RUB",
        data.get("period") or "month",
        int(data.get("requests_per_period") or 0),
        int(data.get("downloads_per_period") or 0),
        int(data.get("bytes_per_period") or 0),
        int(data.get("searches_per_period") or 0),
        int(data.get("rate_limit_per_min") or 60),
        int(data.get("max_api_keys") or 1),
        int(data.get("max_page_size") or 24),
        scopes_json,
        int(data.get("trial_days") or 0),
        1 if data.get("is_active", True) else 0,
        int(data.get("sort_order") or 0),
    )
    with _lock:
        conn = _connect()
        try:
            if data.get("id"):
                conn.execute(
                    """
                    UPDATE billing_plans SET
                        slug=?, name=?, description=?, price_cents=?, currency=?, period=?,
                        requests_per_period=?, downloads_per_period=?, bytes_per_period=?,
                        searches_per_period=?, rate_limit_per_min=?, max_api_keys=?,
                        max_page_size=?, scopes=?, trial_days=?, is_active=?, sort_order=?
                    WHERE id=?
                    """,
                    (*fields, int(data["id"])),
                )
                plan_id = int(data["id"])
            else:
                conn.execute(
                    """
                    INSERT INTO billing_plans (
                        slug, name, description, price_cents, currency, period,
                        requests_per_period, downloads_per_period, bytes_per_period,
                        searches_per_period, rate_limit_per_min, max_api_keys, max_page_size,
                        scopes, trial_days, is_active, sort_order, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*fields, now),
                )
                row = conn.execute(
                    _PLAN_SELECT + " WHERE slug = ?", (data["slug"],)
                ).fetchone()
                conn.commit()
                return _plan_row(row)
            conn.commit()
        finally:
            conn.close()
    return get_billing_plan(plan_id)


def _customer_row(r) -> dict:
    return {
        "id": int(r[0]),
        "name": r[1],
        "email": r[2],
        "notes": r[3],
        "created_at": r[4],
    }


def list_billing_customers() -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id, name, email, notes, created_at FROM billing_customers ORDER BY name"
            ).fetchall()
            customers = [_customer_row(r) for r in rows]
        finally:
            conn.close()
    for c in customers:
        c["subscription"] = get_current_subscription(c["id"])
    return customers


def get_billing_customer(customer_id: int) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, name, email, notes, created_at FROM billing_customers WHERE id = ?",
                (customer_id,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    out = _customer_row(row)
    out["subscription"] = get_current_subscription(customer_id)
    return out


def create_billing_customer(name: str, email: str | None, notes: str | None, now: float) -> dict:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO billing_customers (name, email, notes, created_at) VALUES (?, ?, ?, ?)",
                (name, email, notes, now),
            )
            row = conn.execute(
                "SELECT id, name, email, notes, created_at FROM billing_customers "
                "WHERE name = ? AND created_at = ? ORDER BY id DESC",
                (name, now),
            ).fetchone()
            conn.commit()
        finally:
            conn.close()
    return _customer_row(row)


def _sub_row(r) -> dict:
    return {
        "id": int(r[0]),
        "customer_id": int(r[1]),
        "plan_id": int(r[2]),
        "status": r[3],
        "auto_renew": bool(int(r[4] or 0)),
        "period_start": r[5],
        "period_end": r[6],
        "usage_requests": int(r[7] or 0),
        "usage_downloads": int(r[8] or 0),
        "usage_bytes": int(r[9] or 0),
        "usage_searches": int(r[10] or 0),
        "created_at": r[11],
        "canceled_at": r[12],
    }


_SUB_SELECT = (
    "SELECT id, customer_id, plan_id, status, auto_renew, period_start, period_end, "
    "usage_requests, usage_downloads, usage_bytes, usage_searches, created_at, canceled_at "
    "FROM billing_subscriptions"
)


def get_current_subscription(customer_id: int) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                _SUB_SELECT + " WHERE customer_id = ? AND status != 'canceled' "
                "ORDER BY created_at DESC, id DESC",
                (customer_id,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    sub = _sub_row(row)
    sub["plan"] = get_billing_plan(sub["plan_id"])
    return sub


def _get_sub_conn(conn, sub_id: int) -> dict | None:
    row = conn.execute(_SUB_SELECT + " WHERE id = ?", (sub_id,)).fetchone()
    return _sub_row(row) if row else None


def _issue_invoice_conn(
    conn,
    *,
    customer_id: int,
    subscription_id: int,
    plan: dict,
    period_start: float,
    period_end: float,
    now: float,
    paid: bool,
) -> None:
    status = "paid" if paid or int(plan.get("price_cents") or 0) == 0 else "issued"
    paid_at = now if status == "paid" else None
    conn.execute(
        """
        INSERT INTO billing_invoices (
            customer_id, subscription_id, plan_id, amount_cents, currency, status,
            period_start, period_end, description, issued_at, paid_at, due_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            customer_id,
            subscription_id,
            plan["id"],
            int(plan.get("price_cents") or 0),
            plan.get("currency") or "RUB",
            status,
            period_start,
            period_end,
            f"{plan['name']} · {plan.get('period')}",
            now,
            paid_at,
            period_end,
        ),
    )


def start_subscription(
    *,
    customer_id: int,
    plan_id: int,
    now: float,
    auto_renew: bool = True,
    mark_paid: bool = False,
    use_trial: bool = True,
) -> dict:
    from app.billing import add_period

    plan = get_billing_plan(plan_id)
    if not plan or not plan["is_active"]:
        raise ValueError("Тариф не найден или выключен")
    trial = use_trial and int(plan.get("trial_days") or 0) > 0
    if trial:
        status = "trialing"
        period_end = now + int(plan["trial_days"]) * 86400
    else:
        status = "active"
        period_end = add_period(now, plan["period"])
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE billing_subscriptions SET status = 'canceled', canceled_at = ?
                WHERE customer_id = ? AND status != 'canceled'
                """,
                (now, customer_id),
            )
            conn.execute(
                """
                INSERT INTO billing_subscriptions (
                    customer_id, plan_id, status, auto_renew, period_start, period_end,
                    usage_requests, usage_downloads, usage_bytes, usage_searches, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?)
                """,
                (customer_id, plan_id, status, 1 if auto_renew else 0, now, period_end, now),
            )
            row = conn.execute(
                _SUB_SELECT + " WHERE customer_id = ? ORDER BY id DESC",
                (customer_id,),
            ).fetchone()
            sub = _sub_row(row)
            if not trial:
                _issue_invoice_conn(
                    conn,
                    customer_id=customer_id,
                    subscription_id=sub["id"],
                    plan=plan,
                    period_start=now,
                    period_end=period_end,
                    now=now,
                    paid=mark_paid,
                )
                if int(plan.get("price_cents") or 0) > 0 and not mark_paid:
                    conn.execute(
                        "UPDATE billing_subscriptions SET status = 'past_due' WHERE id = ?",
                        (sub["id"],),
                    )
                    sub["status"] = "past_due"
            conn.commit()
        finally:
            conn.close()
    sub["plan"] = plan
    return sub


def cancel_subscription(sub_id: int, now: float) -> bool:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id FROM billing_subscriptions WHERE id = ? AND status != 'canceled'",
                (sub_id,),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE billing_subscriptions SET status = 'canceled', canceled_at = ?, auto_renew = 0 WHERE id = ?",
                (now, sub_id),
            )
            conn.commit()
        finally:
            conn.close()
    return True


def _roll_subscription(conn, sub: dict, plan: dict, now: float) -> dict:
    from app.billing import add_period

    new_start = sub["period_end"]
    if now - new_start > 86400 * 3:
        new_start = now
    new_end = add_period(new_start, plan["period"])
    paid = int(plan.get("price_cents") or 0) == 0
    status = "active" if paid else "past_due"
    conn.execute(
        """
        UPDATE billing_subscriptions SET
            status = ?, period_start = ?, period_end = ?,
            usage_requests = 0, usage_downloads = 0, usage_bytes = 0, usage_searches = 0
        WHERE id = ?
        """,
        (status, new_start, new_end, sub["id"]),
    )
    _issue_invoice_conn(
        conn,
        customer_id=sub["customer_id"],
        subscription_id=sub["id"],
        plan=plan,
        period_start=new_start,
        period_end=new_end,
        now=now,
        paid=paid,
    )
    return _get_sub_conn(conn, sub["id"])


def resolve_billing(customer_id: int, now: float) -> dict:
    """Актуальная подписка клиента: продлевает период и выставляет счёт при необходимости."""
    from app.billing import add_period  # noqa: F401

    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                _SUB_SELECT + " WHERE customer_id = ? AND status != 'canceled' "
                "ORDER BY created_at DESC, id DESC",
                (customer_id,),
            ).fetchone()
            if not row:
                conn.commit()
                return {"status": "none"}
            sub = _sub_row(row)
            plan_row = conn.execute(_PLAN_SELECT + " WHERE id = ?", (sub["plan_id"],)).fetchone()
            plan = _plan_row(plan_row) if plan_row else None
            if not plan:
                conn.commit()
                return {"status": sub["status"], "subscription": sub, "plan": None}

            if sub["status"] == "trialing" and now >= float(sub["period_end"]):
                if sub["auto_renew"]:
                    paid = int(plan.get("price_cents") or 0) == 0
                    new_end = add_period(now, plan["period"])
                    status = "active" if paid else "past_due"
                    conn.execute(
                        """
                        UPDATE billing_subscriptions SET
                            status = ?, period_start = ?, period_end = ?,
                            usage_requests = 0, usage_downloads = 0, usage_bytes = 0, usage_searches = 0
                        WHERE id = ?
                        """,
                        (status, now, new_end, sub["id"]),
                    )
                    _issue_invoice_conn(
                        conn,
                        customer_id=customer_id,
                        subscription_id=sub["id"],
                        plan=plan,
                        period_start=now,
                        period_end=new_end,
                        now=now,
                        paid=paid,
                    )
                    sub = _get_sub_conn(conn, sub["id"])
                else:
                    conn.execute(
                        "UPDATE billing_subscriptions SET status = 'canceled', canceled_at = ? WHERE id = ?",
                        (now, sub["id"]),
                    )
                    conn.commit()
                    return {"status": "canceled", "subscription": sub, "plan": plan}

            while (
                sub
                and sub["auto_renew"]
                and sub["status"] in ("active",)
                and now >= float(sub["period_end"])
            ):
                sub = _roll_subscription(conn, sub, plan, now)

            conn.commit()
        finally:
            conn.close()
    if not sub:
        return {"status": "none"}
    return {
        "status": sub["status"],
        "subscription": sub,
        "plan": plan,
        "usage": {
            "requests": sub["usage_requests"],
            "downloads": sub["usage_downloads"],
            "bytes": sub["usage_bytes"],
            "searches": sub["usage_searches"],
        },
        "customer_id": customer_id,
    }


def meter_subscription(
    sub_id: int,
    *,
    requests: int = 0,
    downloads: int = 0,
    bytes_: int = 0,
    searches: int = 0,
) -> None:
    if not any((requests, downloads, bytes_, searches)):
        return
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE billing_subscriptions SET
                    usage_requests = usage_requests + ?,
                    usage_downloads = usage_downloads + ?,
                    usage_bytes = usage_bytes + ?,
                    usage_searches = usage_searches + ?
                WHERE id = ?
                """,
                (requests, downloads, bytes_, searches, sub_id),
            )
            conn.commit()
        finally:
            conn.close()


_INV_COLUMNS = (
    "i.id, i.customer_id, i.subscription_id, i.plan_id, i.amount_cents, "
    "i.currency, i.status, i.period_start, i.period_end, i.description, "
    "i.issued_at, i.paid_at, i.due_at, "
    "i.yookassa_payment_id, i.yookassa_status, i.yookassa_confirmation_url"
)


def _invoice_row(r) -> dict:
    return {
        "id": int(r[0]),
        "customer_id": int(r[1]),
        "subscription_id": r[2],
        "plan_id": r[3],
        "amount_cents": int(r[4] or 0),
        "currency": r[5],
        "status": r[6],
        "period_start": r[7],
        "period_end": r[8],
        "description": r[9],
        "issued_at": r[10],
        "paid_at": r[11],
        "due_at": r[12],
        "yookassa_payment_id": r[13] if len(r) > 13 else None,
        "yookassa_status": r[14] if len(r) > 14 else None,
        "yookassa_confirmation_url": r[15] if len(r) > 15 else None,
    }


def list_billing_invoices(limit: int = 100) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                f"""
                SELECT {_INV_COLUMNS}, c.name, p.name
                FROM billing_invoices i
                LEFT JOIN billing_customers c ON c.id = i.customer_id
                LEFT JOIN billing_plans p ON p.id = i.plan_id
                ORDER BY i.issued_at DESC, i.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    out = []
    for r in rows:
        item = _invoice_row(r)
        item["customer_name"] = r[16]
        item["plan_name"] = r[17]
        out.append(item)
    return out


def get_billing_invoice(invoice_id: int) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                f"""
                SELECT {_INV_COLUMNS}, c.name, p.name
                FROM billing_invoices i
                LEFT JOIN billing_customers c ON c.id = i.customer_id
                LEFT JOIN billing_plans p ON p.id = i.plan_id
                WHERE i.id = ?
                """,
                (invoice_id,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    item = _invoice_row(row)
    item["customer_name"] = row[16]
    item["plan_name"] = row[17]
    return item


def get_invoice_by_yookassa_id(payment_id: str) -> dict | None:
    if not payment_id:
        return None
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                f"SELECT {_INV_COLUMNS} FROM billing_invoices i WHERE i.yookassa_payment_id = ?",
                (payment_id,),
            ).fetchone()
        finally:
            conn.close()
    return _invoice_row(row) if row else None


def get_open_invoice_for_customer(customer_id: int) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                f"""
                SELECT {_INV_COLUMNS}
                FROM billing_invoices i
                WHERE i.customer_id = ? AND i.status = 'issued'
                ORDER BY i.issued_at DESC, i.id DESC
                LIMIT 1
                """,
                (customer_id,),
            ).fetchone()
        finally:
            conn.close()
    return _invoice_row(row) if row else None


def update_invoice_yookassa(
    invoice_id: int,
    *,
    payment_id: str | None = None,
    status: str | None = None,
    confirmation_url: str | None = None,
) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE billing_invoices
                SET yookassa_payment_id = COALESCE(?, yookassa_payment_id),
                    yookassa_status = COALESCE(?, yookassa_status),
                    yookassa_confirmation_url = COALESCE(?, yookassa_confirmation_url)
                WHERE id = ?
                """,
                (payment_id, status, confirmation_url, invoice_id),
            )
            conn.commit()
        finally:
            conn.close()


def pay_invoice(invoice_id: int, now: float) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, customer_id, subscription_id, status FROM billing_invoices WHERE id = ?",
                (invoice_id,),
            ).fetchone()
            if not row:
                return None
            if row[3] == "paid":
                conn.commit()
                return {"id": int(row[0]), "status": "paid", "already": True}
            conn.execute(
                "UPDATE billing_invoices SET status = 'paid', paid_at = ? WHERE id = ?",
                (now, invoice_id),
            )
            sub_id = row[2]
            if sub_id:
                unpaid = conn.execute(
                    "SELECT COUNT(*) FROM billing_invoices "
                    "WHERE subscription_id = ? AND status = 'issued'",
                    (sub_id,),
                ).fetchone()[0]
                if int(unpaid or 0) == 0:
                    conn.execute(
                        "UPDATE billing_subscriptions SET status = 'active' "
                        "WHERE id = ? AND status = 'past_due'",
                        (sub_id,),
                    )
            conn.commit()
        finally:
            conn.close()
    return {"id": invoice_id, "status": "paid"}


def billing_overview() -> dict:
    with _lock:
        conn = _connect()
        try:
            customers = conn.execute("SELECT COUNT(*) FROM billing_customers").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM billing_subscriptions WHERE status IN ('active', 'trialing')"
            ).fetchone()[0]
            past_due = conn.execute(
                "SELECT COUNT(*) FROM billing_subscriptions WHERE status = 'past_due'"
            ).fetchone()[0]
            unpaid = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) FROM billing_invoices WHERE status = 'issued'"
            ).fetchone()[0]
            paid_month = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) FROM billing_invoices "
                "WHERE status = 'paid' AND paid_at >= ?",
                (time.time() - 30 * 86400,),
            ).fetchone()[0]
        finally:
            conn.close()
    return {
        "customers": int(customers or 0),
        "active_subscriptions": int(active or 0),
        "past_due": int(past_due or 0),
        "unpaid_cents": int(unpaid or 0),
        "paid_last_30d_cents": int(paid_month or 0),
    }


def bump_usage_daily(
    *,
    customer_id: int = 0,
    key_id: int = 0,
    requests: int = 0,
    downloads: int = 0,
    bytes_: int = 0,
    searches: int = 0,
    now: float | None = None,
) -> None:
    if not any((requests, downloads, bytes_, searches)):
        return
    from datetime import datetime, timezone

    day = datetime.fromtimestamp(now or time.time(), tz=timezone.utc).strftime("%Y-%m-%d")
    cid = int(customer_id or 0)
    kid = int(key_id or 0)
    with _lock:
        conn = _connect()
        try:
            if IS_MYSQL:
                conn.execute(
                    """
                    INSERT INTO billing_usage_daily
                        (day, customer_id, key_id, requests, downloads, bytes, searches)
                    VALUES (?, ?, ?, ?, ?, ?, ?) AS new
                    ON DUPLICATE KEY UPDATE
                        requests = billing_usage_daily.requests + new.requests,
                        downloads = billing_usage_daily.downloads + new.downloads,
                        bytes = billing_usage_daily.bytes + new.bytes,
                        searches = billing_usage_daily.searches + new.searches
                    """,
                    (day, cid, kid, requests, downloads, bytes_, searches),
                )
            elif IS_POSTGRES:
                conn.execute(
                    """
                    INSERT INTO billing_usage_daily
                        (day, customer_id, key_id, requests, downloads, bytes, searches)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (day, customer_id, key_id) DO UPDATE SET
                        requests = billing_usage_daily.requests + EXCLUDED.requests,
                        downloads = billing_usage_daily.downloads + EXCLUDED.downloads,
                        bytes = billing_usage_daily.bytes + EXCLUDED.bytes,
                        searches = billing_usage_daily.searches + EXCLUDED.searches
                    """,
                    (day, cid, kid, requests, downloads, bytes_, searches),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO billing_usage_daily
                        (day, customer_id, key_id, requests, downloads, bytes, searches)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day, customer_id, key_id) DO UPDATE SET
                        requests = requests + excluded.requests,
                        downloads = downloads + excluded.downloads,
                        bytes = bytes + excluded.bytes,
                        searches = searches + excluded.searches
                    """,
                    (day, cid, kid, requests, downloads, bytes_, searches),
                )
            conn.commit()
        finally:
            conn.close()


def usage_metrics(days: int = 14) -> dict:
    from datetime import datetime, timedelta, timezone

    days = max(1, min(int(days), 90))
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=days - 1)).isoformat()
    with _lock:
        conn = _connect()
        try:
            series_rows = conn.execute(
                """
                SELECT day,
                       COALESCE(SUM(requests), 0),
                       COALESCE(SUM(downloads), 0),
                       COALESCE(SUM(bytes), 0),
                       COALESCE(SUM(searches), 0)
                FROM billing_usage_daily
                WHERE day >= ?
                GROUP BY day
                ORDER BY day
                """,
                (start,),
            ).fetchall()
            top_cust = conn.execute(
                """
                SELECT u.customer_id,
                       COALESCE(SUM(u.requests), 0),
                       COALESCE(SUM(u.downloads), 0),
                       COALESCE(SUM(u.bytes), 0),
                       COALESCE(SUM(u.searches), 0),
                       c.name
                FROM billing_usage_daily u
                LEFT JOIN billing_customers c ON c.id = u.customer_id
                WHERE u.day >= ? AND u.customer_id != 0
                GROUP BY u.customer_id, c.name
                ORDER BY SUM(u.requests) DESC
                LIMIT 10
                """,
                (start,),
            ).fetchall()
            top_keys = conn.execute(
                """
                SELECT u.key_id,
                       COALESCE(SUM(u.requests), 0),
                       COALESCE(SUM(u.downloads), 0),
                       k.name, k.key_prefix, k.customer_id
                FROM billing_usage_daily u
                LEFT JOIN api_keys k ON k.id = u.key_id
                WHERE u.day >= ? AND u.key_id != 0
                GROUP BY u.key_id, k.name, k.key_prefix, k.customer_id
                ORDER BY SUM(u.requests) DESC
                LIMIT 10
                """,
                (start,),
            ).fetchall()
            keys_active = conn.execute(
                "SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL"
            ).fetchone()[0]
            keys_total = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
        finally:
            conn.close()
    by_day = {
        r[0]: {
            "day": r[0],
            "requests": int(r[1] or 0),
            "downloads": int(r[2] or 0),
            "bytes": int(r[3] or 0),
            "searches": int(r[4] or 0),
        }
        for r in series_rows
    }
    series = []
    for i in range(days):
        d = (today - timedelta(days=days - 1 - i)).isoformat()
        series.append(by_day.get(d, {"day": d, "requests": 0, "downloads": 0, "bytes": 0, "searches": 0}))
    totals = {
        "requests": sum(x["requests"] for x in series),
        "downloads": sum(x["downloads"] for x in series),
        "bytes": sum(x["bytes"] for x in series),
        "searches": sum(x["searches"] for x in series),
    }
    return {
        "days": days,
        "series": series,
        "totals": totals,
        "today": series[-1] if series else None,
        "keys_active": int(keys_active or 0),
        "keys_total": int(keys_total or 0),
        "top_customers": [
            {
                "customer_id": int(r[0]),
                "requests": int(r[1] or 0),
                "downloads": int(r[2] or 0),
                "bytes": int(r[3] or 0),
                "searches": int(r[4] or 0),
                "name": r[5] or f"#{r[0]}",
            }
            for r in top_cust
        ],
        "top_keys": [
            {
                "key_id": int(r[0]),
                "requests": int(r[1] or 0),
                "downloads": int(r[2] or 0),
                "name": r[3] or f"key #{r[0]}",
                "key_prefix": r[4],
                "customer_id": r[5],
            }
            for r in top_keys
        ],
    }


def update_billing_customer(customer_id: int, name: str, email: str | None, notes: str | None) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id FROM billing_customers WHERE id = ?", (customer_id,)
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE billing_customers SET name = ?, email = ?, notes = ? WHERE id = ?",
                (name, email, notes, customer_id),
            )
            conn.commit()
        finally:
            conn.close()
    return get_billing_customer(customer_id)


def delete_billing_customer(customer_id: int, now: float) -> bool:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id FROM billing_customers WHERE id = ?", (customer_id,)
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE billing_subscriptions SET status = 'canceled', canceled_at = ?, auto_renew = 0 "
                "WHERE customer_id = ? AND status != 'canceled'",
                (now, customer_id),
            )
            conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE customer_id = ? AND revoked_at IS NULL",
                (now, customer_id),
            )
            conn.execute("DELETE FROM billing_customers WHERE id = ?", (customer_id,))
            conn.commit()
        finally:
            conn.close()
    return True


def delete_billing_plan(plan_id: int) -> str | None:
    """None = ок, иначе причина отказа."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT id FROM billing_plans WHERE id = ?", (plan_id,)).fetchone()
            if not row:
                return "not_found"
            used = conn.execute(
                "SELECT COUNT(*) FROM billing_subscriptions WHERE plan_id = ? AND status != 'canceled'",
                (plan_id,),
            ).fetchone()[0]
            if int(used or 0) > 0:
                return "in_use"
            conn.execute("DELETE FROM billing_plans WHERE id = ?", (plan_id,))
            conn.commit()
        finally:
            conn.close()
    return None


def void_invoice(invoice_id: int) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id, status FROM billing_invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
            if not row:
                return None
            if row[1] == "paid":
                return {"id": invoice_id, "status": "paid", "error": "already_paid"}
            conn.execute(
                "UPDATE billing_invoices SET status = 'void' WHERE id = ?", (invoice_id,)
            )
            conn.commit()
        finally:
            conn.close()
    return {"id": invoice_id, "status": "void"}


def update_api_key(
    key_id: int,
    *,
    name: str | None = None,
    scopes: list[str] | None = None,
    rate_limit_per_min: int | None = None,
) -> dict | None:
    import json

    with _lock:
        conn = _connect()
        try:
            row = conn.execute(_API_KEY_SELECT + " WHERE id = ?", (key_id,)).fetchone()
            if not row:
                return None
            current = _api_key_row(row)
            new_name = (name or current["name"])[:120]
            new_scopes = json.dumps(scopes if scopes is not None else current["scopes"], ensure_ascii=False)
            new_rpm = current["rate_limit_per_min"] if rate_limit_per_min is None else int(rate_limit_per_min)
            conn.execute(
                "UPDATE api_keys SET name = ?, scopes = ?, rate_limit_per_min = ? WHERE id = ?",
                (new_name, new_scopes, new_rpm, key_id),
            )
            row = conn.execute(_API_KEY_SELECT + " WHERE id = ?", (key_id,)).fetchone()
            conn.commit()
        finally:
            conn.close()
    return _api_key_row(row)

