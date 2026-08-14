from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def dump(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [dump(v) for v in value]
    if isinstance(value, dict):
        return {k: dump(v) for k, v in value.items()}
    return value


def _row_project(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "prompt": row["prompt"],
        "platform": row["platform"],
        "status": row["status"],
        "plan": row.get("plan_json") or {},
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _row_job(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "agent": row["agent"],
        "provider": row["provider"],
        "model": row["model"],
        "kind": row["kind"],
        "status": row["status"],
        "input": row.get("input_json") or {},
        "output": row.get("output_json") or {},
        "error": row.get("error"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _row_asset(row: dict) -> dict:
    job_id = row.get("job_id")
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "job_id": str(job_id) if job_id else None,
        "kind": row["kind"],
        "title": row["title"],
        "url": row["url"],
        "meta": row.get("meta_json") or {},
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def _row_message(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "role": row["role"],
        "content": row["content"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def _sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            if stmt:
                statements.append(stmt)
            buf = []
    rest = "\n".join(buf).strip().rstrip(";").strip()
    if rest:
        statements.append(rest)
    return statements


async def migrate(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        for path in sorted(MIGRATIONS.glob("*.sql")):
            version = path.name
            cur = await conn.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (version,))
            if await cur.fetchone():
                continue
            for stmt in _sql_statements(path.read_text(encoding="utf-8")):
                await conn.execute(stmt)
            await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))


class Store:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def create_project(self, title: str, prompt: str, platform: str) -> dict:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO projects (title, prompt, platform)
                    VALUES (%s, %s, %s)
                    RETURNING id, title, prompt, platform, status, plan_json, created_at, updated_at
                    """,
                    (title, prompt, platform),
                )
                return _row_project(await cur.fetchone())

    async def list_projects(self) -> list[dict]:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, title, prompt, platform, status, plan_json, created_at, updated_at
                    FROM projects ORDER BY created_at DESC LIMIT 50
                    """
                )
                return [_row_project(r) for r in await cur.fetchall()]

    async def get_project(self, project_id: str) -> dict | None:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, title, prompt, platform, status, plan_json, created_at, updated_at
                    FROM projects WHERE id = %s
                    """,
                    (project_id,),
                )
                row = await cur.fetchone()
                if not row:
                    return None
                project = _row_project(row)
                project["jobs"] = await self.list_jobs(project_id)
                project["assets"] = await self.list_assets(project_id)
                project["messages"] = await self.list_messages(project_id)
                return project

    async def update_project(self, project_id: str, status: str, title: str = "", plan: Any = None) -> None:
        async with self.pool.connection() as conn:
            if plan is None:
                await conn.execute(
                    """
                    UPDATE projects SET status = %s,
                        title = COALESCE(NULLIF(%s, ''), title),
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (status, title, project_id),
                )
                return
            await conn.execute(
                """
                UPDATE projects SET status = %s,
                    title = COALESCE(NULLIF(%s, ''), title),
                    plan_json = %s, updated_at = now()
                WHERE id = %s
                """,
                (status, title, Json(plan), project_id),
            )

    async def create_job(self, **kwargs: Any) -> dict:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO jobs (project_id, agent, provider, model, kind, status, input_json, output_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id, project_id, agent, provider, model, kind, status, input_json, output_json, error, created_at, updated_at
                    """,
                    (
                        kwargs["project_id"],
                        kwargs["agent"],
                        kwargs["provider"],
                        kwargs["model"],
                        kwargs["kind"],
                        kwargs.get("status") or "running",
                        Json(kwargs.get("input") or {}),
                        Json(kwargs.get("output") or {}),
                    ),
                )
                return _row_job(await cur.fetchone())

    async def finish_job(self, job_id: str, status: str, output: Any = None, error: str | None = None) -> dict | None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE jobs SET status = %s, output_json = %s, error = %s, updated_at = now()
                WHERE id = %s
                """,
                (status, Json(output if output is not None else {}), error, job_id),
            )
        return await self.get_job(job_id)

    async def get_job(self, job_id: str) -> dict | None:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, project_id, agent, provider, model, kind, status, input_json, output_json, error, created_at, updated_at
                    FROM jobs WHERE id = %s
                    """,
                    (job_id,),
                )
                row = await cur.fetchone()
                return _row_job(row) if row else None

    async def list_jobs(self, project_id: str) -> list[dict]:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, project_id, agent, provider, model, kind, status, input_json, output_json, error, created_at, updated_at
                    FROM jobs WHERE project_id = %s ORDER BY created_at
                    """,
                    (project_id,),
                )
                return [_row_job(r) for r in await cur.fetchall()]

    async def add_asset(self, **kwargs: Any) -> dict:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO assets (project_id, job_id, kind, title, url, meta_json)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    RETURNING id, project_id, job_id, kind, title, url, meta_json, created_at
                    """,
                    (
                        kwargs["project_id"],
                        kwargs.get("job_id"),
                        kwargs["kind"],
                        kwargs.get("title") or "",
                        kwargs.get("url") or "",
                        Json(kwargs.get("meta") or {}),
                    ),
                )
                return _row_asset(await cur.fetchone())

    async def list_assets(self, project_id: str) -> list[dict]:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, project_id, job_id, kind, title, url, meta_json, created_at
                    FROM assets WHERE project_id = %s ORDER BY created_at
                    """,
                    (project_id,),
                )
                return [_row_asset(r) for r in await cur.fetchall()]

    async def add_message(self, project_id: str, role: str, content: str) -> dict:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO messages (project_id, role, content)
                    VALUES (%s, %s, %s)
                    RETURNING id, project_id, role, content, created_at
                    """,
                    (project_id, role, content),
                )
                return _row_message(await cur.fetchone())

    async def list_messages(self, project_id: str) -> list[dict]:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, project_id, role, content, created_at
                    FROM messages WHERE project_id = %s ORDER BY created_at
                    """,
                    (project_id,),
                )
                return [_row_message(r) for r in await cur.fetchall()]
