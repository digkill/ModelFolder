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


def _ensure_asset_columns(conn) -> None:
    if IS_SERVER_DB:
        return  # серверная схема уже содержит все колонки
    cur = conn.execute("PRAGMA table_info(assets)")
    cols = {row[1] for row in cur.fetchall()}
    wanted = {
        "blend_path": "TEXT",
        "preview_source": "TEXT",
        "content_adult": "INTEGER",
        "content_nudity": "INTEGER",
        "content_violence": "INTEGER",
        "content_horror": "INTEGER",
        "content_gore": "INTEGER",
        "content_sensitive_tags": "TEXT",
        "safety_checked_at": "REAL",
        "description": "TEXT",
        "description_source": "TEXT",
        "described_at": "REAL",
        "embedded_at": "REAL",
    }
    for name, typ in wanted.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE assets ADD COLUMN {name} {typ}")


def init_db() -> None:
    with _lock:
        conn = _connect()
        try:
            if IS_MYSQL:
                for stmt in MYSQL_SCHEMA:
                    conn.execute(stmt)
            elif IS_POSTGRES:
                for stmt in POSTGRES_SCHEMA:
                    conn.execute(stmt)
            else:
                conn.executescript(SCHEMA)
                _ensure_asset_columns(conn)
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
                    SELECT path, name, preview_file FROM assets
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
                    SELECT path, name, preview_file FROM assets
                    WHERE preview_status = 'ok' AND preview_file IS NOT NULL
                    ORDER BY path
                    LIMIT ?
                    """,
                    (limit,),
                )
            return [
                {"path": r[0], "name": r[1], "preview_file": r[2]}
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
