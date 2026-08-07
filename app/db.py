import sqlite3
import threading
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
}

_EXTRA_INDEXES = (
    ("idx_assets_content_hash", "assets(content_hash)"),
    ("idx_assets_category", "assets(category)"),
    ("idx_assets_age_rating", "assets(age_rating)"),
    ("idx_assets_nsfw", "assets(nsfw)"),
    ("idx_assets_kid_friendly", "assets(kid_friendly)"),
    ("idx_assets_ext", "assets(ext)"),
    ("idx_assets_animation_count", "assets(animation_count)"),
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


def _existing_asset_columns(conn) -> set[str]:
    if IS_MYSQL:
        cur = conn.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'assets'"
        )
    elif IS_POSTGRES:
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'assets'"
        )
    else:
        cur = conn.execute("PRAGMA table_info(assets)")
        return {row[1] for row in cur.fetchall()}
    return {str(r[0]) for r in cur.fetchall()}


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
        where.append("a.preview_status = 'ok' AND a.preview_file IS NOT NULL")
    if animated is not None:
        # Признак берём из метаданных файла (клипы анимации), а не из тегов AI:
        # он точный и есть у модели сразу после заливки.
        where.append(
            "a.animation_count > 0" if animated
            else "(a.animation_count IS NULL OR a.animation_count = 0)"
        )
    if rigged is not None:
        where.append("a.has_rig = 1" if rigged else "(a.has_rig IS NULL OR a.has_rig = 0)")
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
}


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
