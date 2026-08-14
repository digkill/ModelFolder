from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.db import Store
from app.hub import Hub
from app.providers import Clients
from app.registry import find_model

log = logging.getLogger("studio.orchestrator")

PLAN_SYSTEM = """You are the lead producer of a cloud game studio.
Return ONLY valid JSON (no markdown) with keys:
title, genre, logline, platform, scenes (array of 3 short scene descriptions),
catalog_queries (array of 3-6 short English search queries for a 3D model catalog),
image_prompt, video_prompt, voice_line, music_prompt, mesh_prompt, web_notes.
Write prompts in English. Title/logline/scenes may be in the user's language.
Target platform: {platform}.
The game must be assemblable as web, later wrapped to mobile/desktop."""


class Engine:
    def __init__(self, store: Store, api: Clients, hub: Hub) -> None:
        self.store = store
        self.api = api
        self.hub = hub

    async def run(self, project: dict) -> None:
        project_id = project["id"]
        try:
            await self.store.update_project(project_id, "planning")
            self.hub.publish(project_id, "status", {"status": "planning"})
            plan, model_used = await self.make_plan(project)
            title = plan.get("title") or _truncate(project["prompt"], 48)
            await self.store.update_project(project_id, "generating", title, plan)
            await self.note(
                project_id,
                "orchestrator",
                f"План готов ({model_used}): {title} — {plan.get('logline') or ''}",
            )
            self.hub.publish(project_id, "plan", plan)
            await asyncio.gather(
                self.search_catalog(project, plan),
                self.make_image(project, plan),
                self.make_video(project, plan),
                self.make_voice(project, plan),
                self.make_music(project, plan),
                self.make_mesh(project, plan),
                return_exceptions=True,
            )
            await self.store.update_project(project_id, "ready", title, plan)
            await self.note(
                project_id,
                "orchestrator",
                f"Сборка завершена. Можно смотреть превью и экспорт под {project['platform']}.",
            )
            self.hub.publish(project_id, "status", {"status": "ready"})
        except Exception as exc:
            log.exception("pipeline failed")
            await self.fail(project_id, "orchestrator", exc)

    async def make_plan(self, project: dict) -> tuple[dict, str]:
        job = await self.store.create_job(
            project_id=project["id"],
            agent="orchestrator",
            provider="auto",
            model=self.api.pick_orchestrator(),
            kind="plan",
            status="running",
            input={"prompt": project["prompt"], "platform": project["platform"]},
        )
        self.hub.publish(project["id"], "job", job)
        try:
            text, used = await self.api.chat(
                self.api.pick_orchestrator(),
                [
                    {"role": "system", "content": PLAN_SYSTEM.format(platform=project["platform"])},
                    {"role": "user", "content": project["prompt"]},
                ],
            )
        except Exception as exc:
            await self.store.finish_job(job["id"], "error", error=str(exc))
            raise
        plan = _parse_plan(text, project)
        job = await self.store.finish_job(job["id"], "done", {"model": used, "raw": text, "plan": plan})
        self.hub.publish(project["id"], "job", job)
        return plan, used

    async def search_catalog(self, project: dict, plan: dict) -> None:
        queries = [q for q in (plan.get("catalog_queries") or []) if str(q).strip()]
        if not queries:
            queries = [project["prompt"]]
        job = await self.store.create_job(
            project_id=project["id"],
            agent="catalog",
            provider="catalog",
            model="catalog-search",
            kind="catalog",
            input=queries,
        )
        all_hits: list[dict] = []
        seen: set[str] = set()
        for query in queries:
            try:
                hits = await self.api.search_catalog(query, 8)
            except Exception as exc:
                log.warning("catalog search %r: %s", query, exc)
                continue
            for hit in hits:
                path = hit.get("path") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                all_hits.append(hit)
                url = hit.get("view_url") or hit.get("preview_url") or ""
                asset = await self.store.add_asset(
                    project_id=project["id"],
                    job_id=job["id"],
                    kind="model",
                    title=hit.get("name") or "",
                    url=url,
                    meta=hit,
                )
                self.hub.publish(project["id"], "asset", asset)
        status = "done"
        error = None
        if not all_hits:
            status = "error"
            error = "catalog returned no models"
        job = await self.store.finish_job(job["id"], status, all_hits, error)
        self.hub.publish(project["id"], "job", job)

    async def make_image(self, project: dict, plan: dict) -> None:
        prompt = _nz(plan.get("image_prompt"), f"cinematic game concept art, {project['prompt']}")
        await self.media(
            project,
            "art-director",
            "image",
            "gpt-image-1.5",
            prompt,
            {"prompt": prompt, "aspect_ratio": "16:9", "quality": "high"},
        )

    async def make_video(self, project: dict, plan: dict) -> None:
        prompt = _nz(plan.get("video_prompt"), f"short cinematic trailer, {project['prompt']}")
        await self.media(
            project,
            "cinematographer",
            "video",
            "grok-imagine-video-1.5",
            prompt,
            {"prompt": prompt, "aspect_ratio": "16:9", "duration": 8, "resolution": "720p"},
        )

    async def make_voice(self, project: dict, plan: dict) -> None:
        line = _nz(plan.get("voice_line"), plan.get("logline") or "")
        line = _nz(line, project["prompt"])
        await self.media(
            project,
            "voice",
            "audio",
            "elevenlabs-tts",
            line,
            {"text": line, "voice": "Rachel M - Pro British Radio Presenter"},
        )

    async def make_music(self, project: dict, plan: dict) -> None:
        prompt = _nz(plan.get("music_prompt"), f"game soundtrack, {plan.get('genre') or ''}")
        await self.media(
            project,
            "composer",
            "music",
            "suno",
            prompt,
            {"prompt": prompt, "customMode": False, "instrumental": True},
        )

    async def make_mesh(self, project: dict, plan: dict) -> None:
        prompt = _nz(plan.get("mesh_prompt"), project["prompt"])
        job = await self.store.create_job(
            project_id=project["id"],
            agent="sculptor",
            provider="meshy",
            model="meshy-text-to-3d",
            kind="mesh",
            input={"prompt": prompt},
        )
        try:
            result = await self.api.meshy_text_to_3d(prompt)
        except Exception as exc:
            await self.store.finish_job(job["id"], "error", error=str(exc))
            await self.note(project["id"], "sculptor", f"Meshy: {exc}")
            self.hub.publish(project["id"], "job", {**job, "status": "error", "error": str(exc)})
            return
        job = await self.store.finish_job(job["id"], "done", result.get("raw"))
        for i, url in enumerate(result.get("urls") or [], start=1):
            asset = await self.store.add_asset(
                project_id=project["id"],
                job_id=job["id"] if job else None,
                kind="mesh",
                title=f"mesh-{i}",
                url=url,
                meta=result,
            )
            self.hub.publish(project["id"], "asset", asset)
        self.hub.publish(project["id"], "job", job)

    async def media(
        self,
        project: dict,
        agent: str,
        kind: str,
        model_id: str,
        title: str,
        payload: dict[str, Any],
    ) -> None:
        model = find_model(model_id)
        job = await self.store.create_job(
            project_id=project["id"],
            agent=agent,
            provider=model.get("provider") or "kie",
            model=model["id"],
            kind=kind,
            input=payload,
        )
        self.hub.publish(project["id"], "job", job)
        try:
            result = await self.api.create_kie_task(model.get("kie_model") or model["id"], payload)
        except Exception as exc:
            await self.store.finish_job(job["id"], "error", error=str(exc))
            await self.note(project["id"], agent, f"{kind}: {exc}")
            self.hub.publish(project["id"], "job", {**job, "status": "error", "error": str(exc)})
            return
        job = await self.store.finish_job(job["id"], "done", result.get("raw"))
        for i, url in enumerate(result.get("urls") or [], start=1):
            asset = await self.store.add_asset(
                project_id=project["id"],
                job_id=job["id"] if job else None,
                kind=kind,
                title=f"{title}-{i}",
                url=url,
                meta=result,
            )
            self.hub.publish(project["id"], "asset", asset)
        self.hub.publish(project["id"], "job", job)

    async def fail(self, project_id: str, role: str, err: Exception) -> None:
        await self.store.update_project(project_id, "error")
        await self.note(project_id, role, str(err))
        self.hub.publish(project_id, "status", {"status": "error", "error": str(err)})

    async def note(self, project_id: str, role: str, content: str) -> None:
        message = await self.store.add_message(project_id, role, content)
        self.hub.publish(project_id, "message", message)


def _parse_plan(text: str, project: dict) -> dict:
    plan = {
        "title": _truncate(project["prompt"], 48),
        "genre": "",
        "logline": project["prompt"],
        "platform": project["platform"],
        "scenes": [],
        "catalog_queries": [],
        "image_prompt": "",
        "video_prompt": "",
        "voice_line": "",
        "music_prompt": "",
        "mesh_prompt": "",
        "web_notes": "",
    }
    raw = _extract_json(text)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                plan.update({k: parsed[k] for k in plan if k in parsed})
        except json.JSONDecodeError:
            pass
    if not plan.get("platform"):
        plan["platform"] = project["platform"]
    return plan


def _extract_json(text: str) -> str:
    text = text.strip()
    if "```" in text:
        text = text.split("```", 1)[1]
        text = text.removeprefix("json").strip()
        if "```" in text:
            text = text.split("```", 1)[0]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return ""


def _nz(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


def _truncate(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    return text[:n] + "…"
