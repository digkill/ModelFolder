from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.db import Store
from app.game import (
    ROLE_EXPECT,
    _role,
    context_blob,
    detect_genre,
    filter_queries,
    foreign_hay,
    sanitize_play,
    hit_hay,
    history_messages,
    in_project_context,
    inspect_build,
    looks_like_car,
    looks_like_race_car,
    pick_cast,
    pick_url,
    pose_from_bbox,
    project_brief,
    rank_catalog_hits,
    scene_search_plan,
    slot_need,
    wants_game,
    wants_review,
    write_game,
)
from app.hub import Hub
from app.providers import Clients, _extract_audio_urls
from app.registry import find_model
from app.review import (
    build_review_prompt,
    default_gdd,
    format_review_note,
    parse_json_obj,
)

log = logging.getLogger("studio.orchestrator")

PLAN_SYSTEM = """You are the lead producer of a cloud game studio.
This user prompt is the ONLY brief. Do not reuse characters, settings, or mechanics from any other game.
Return ONLY valid JSON (no markdown) with keys:
title, genre, logline, platform, scenes (array of 3 short scene descriptions),
catalog_queries (6-10 SHORT English object names for THIS brief, never sentences),
image_prompt, video_prompt, voice_line, music_prompt, web_notes,
play (object of mechanics for THIS prompt).
play MUST contain:
camera (chase|orbit|top|first), move (walk|drive|grid), jump (bool), mouse_look (bool),
sprint (bool), win (collect|laps|reach|survive), goal_count (int), goal_label (short),
arena (open|ring|maze|path), hazards (none|ghosts|traffic), hint (short controls in the user's language).
Invent mechanics that match THIS prompt. NEVER default to FPS / jump / mouse-look / crystal-hunt.
Racing prompt → chase+drive+laps+ring. Pac-Man/labyrinth → top+grid+collect+maze, no jump, no mouse.
Walking tour → orbit+walk+reach, no jump. Platformer → orbit+walk+jump. Shooter only if the prompt asks for a shooter.
genre is a short label for THIS prompt, not a copy of a previous project.
3D comes only from catalog search. Title/logline/scenes may be in the user's language.
Target platform: {platform}."""

CHAT_SYSTEM = """You are the lead producer of ModelFolder Studio for ONE existing project.
You cannot write game code, cannot start a backlog, cannot "go make a track".
The engine builds the playable HTML from the play spec. Your job is to talk about what is ALREADY built in THIS project.
Rules:
- Reply in the user's language, 4-8 short sentences.
- Never invent files or promise future work ("сейчас сделаю трассу", "начну с анимаций").
- Never mention other studio projects, leftover catalog junk, or assets not listed in the context.
- If a rebuild just ran, say the play spec (camera, move, win), which catalog model is the player, how to play. Game stays in the panel — fullscreen only if the user clicks the button.
- The generated still is the splash; the generated clip is the trailer preview before play.
- If the user hates it, be blunt about the inspection (wrong hero, wrong play spec) — do not soothe with a 5-step plan.
- Do not dump JSON.

{context}
"""


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
            plan = await self.make_gdd(project, plan)
            await self.store.update_project(project_id, "generating", title, plan)
            await asyncio.gather(
                self.search_catalog(project, plan),
                self.make_image(project, plan),
                self.make_video(project, plan),
                self.make_voice(project, plan),
                self.make_music(project, plan),
                *([self.make_mesh(project, plan)] if self.api.cfg.studio_use_meshy else []),
                return_exceptions=True,
            )
            await self.assemble_game(project)
            plan = await self.review_game(project) or plan
            await self.store.update_project(project_id, "ready", title, plan)
            await self.note(
                project_id,
                "orchestrator",
                f"Сборка завершена. Мини-игра готова — можно играть в превью и экспортировать под {project['platform']}.",
            )
            self.hub.publish(project_id, "status", {"status": "ready"})
        except Exception as exc:
            log.exception("pipeline failed")
            await self.fail(project_id, "orchestrator", exc)

    async def rerun_media(self, project: dict) -> None:
        plan = project.get("plan") or {}
        if not plan:
            await self.run(project)
            return
        project_id = project["id"]
        await self.store.update_project(project_id, "generating", project.get("title") or "", plan)
        self.hub.publish(project_id, "status", {"status": "generating"})
        await asyncio.gather(
            self.search_catalog(project, plan),
            self.make_image(project, plan),
            self.make_video(project, plan),
            self.make_voice(project, plan),
            self.make_music(project, plan),
            *([self.make_mesh(project, plan)] if self.api.cfg.studio_use_meshy else []),
            return_exceptions=True,
        )
        await self.assemble_game(project)
        plan = await self.review_game(project) or plan
        await self.store.update_project(project_id, "ready", project.get("title") or "", plan)
        self.hub.publish(project_id, "status", {"status": "ready"})

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
                    {
                        "role": "user",
                        "content": (
                            f"Project id: {project['id']}\n"
                            "This prompt is the entire brief. Do not borrow from other games.\n\n"
                            f"{project['prompt']}"
                        ),
                    },
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
        fresh = await self.store.get_project(project["id"]) or dict(project)
        fresh["plan"] = plan
        await self.fill_scene_from_catalog(fresh)

    async def make_image(self, project: dict, plan: dict) -> None:
        prompt = _nz(
            plan.get("image_prompt"),
            f"widescreen game title splash, cinematic key art, no UI chrome, {project['prompt']}",
        )
        await self.media(
            project,
            "art-director",
            "image",
            "gpt-image-1.5",
            prompt,
            {"prompt": prompt, "aspect_ratio": "3:2", "quality": "medium"},
        )

    async def make_video(self, project: dict, plan: dict) -> None:
        prompt = _nz(
            plan.get("video_prompt"),
            f"short cinematic game trailer preview, 16:9, no UI chrome, {project['prompt']}",
        )
        await self.media(
            project,
            "cinematographer",
            "video",
            "grok-imagine-video-1.5",
            prompt,
            {
                "prompt": prompt,
                "aspect_ratio": "16:9",
                "mode": "normal",
                "duration": 6,
                "resolution": "720p",
            },
        )

    async def make_voice(self, project: dict, plan: dict) -> None:
        line = _nz(plan.get("voice_line"), plan.get("logline") or "")
        line = _nz(line, project["prompt"])
        job = await self.store.create_job(
            project_id=project["id"],
            agent="voice",
            provider="openai",
            model="tts-1",
            kind="audio",
            input={"text": line, "voice": "nova"},
        )
        self.hub.publish(project["id"], "job", job)
        try:
            result = await self.api.speak(line)
        except Exception as exc:
            await self.store.finish_job(job["id"], "error", error=str(exc))
            await self.note(project["id"], "voice", f"audio: {exc}")
            self.hub.publish(project["id"], "job", {**job, "status": "error", "error": str(exc)})
            return
        job = await self.store.finish_job(job["id"], "done", result.get("raw"))
        for i, url in enumerate(result.get("urls") or [], start=1):
            asset = await self.store.add_asset(
                project_id=project["id"],
                job_id=job["id"] if job else None,
                kind="audio",
                title=f"{line}-{i}" if len(line) < 80 else f"voice-{i}",
                url=url,
                meta=result,
            )
            self.hub.publish(project["id"], "asset", asset)
        self.hub.publish(project["id"], "job", job)

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
        if not self.api.cfg.meshy_api_key:
            await self.note(project["id"], "sculptor", "Meshy пропущен: нет MESHY_API_KEY")
            return
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
            result = await self.api.create_media_task(model, payload)
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
        if kind in {"image", "video", "music", "audio"}:
            await self.refresh_game_media(project["id"])

    async def refresh_game_media(self, project_id: str) -> None:
        fresh = await self.store.get_project(project_id)
        if not fresh or not any(a.get("kind") == "game" for a in (fresh.get("assets") or [])):
            return
        self.api.publicize_project(fresh)
        try:
            write_game(fresh)
        except Exception as exc:
            log.warning("refresh game media: %s", exc)

    async def salvage_music(self, project: dict) -> int:
        fresh = await self.store.get_project(project["id"]) or project
        urls: list[str] = []
        task_ids: list[str] = []
        for job in fresh.get("jobs") or []:
            if job.get("kind") != "music":
                continue
            urls.extend(_extract_audio_urls(job.get("output") or {}))
            data = job.get("output") if isinstance(job.get("output"), dict) else {}
            task_id = str(((data.get("data") or {}) if isinstance(data, dict) else {}).get("taskId") or "")
            if task_id:
                task_ids.append(task_id)
        cached = await self._cache_audio_urls(urls)
        if not cached:
            for task_id in task_ids:
                try:
                    polled = await self.api._poll_suno(task_id)
                    more = list(polled.get("urls") or [])
                    more.extend(_extract_audio_urls(polled.get("raw") or {}))
                    cached = await self._cache_audio_urls(more)
                    if cached:
                        break
                except Exception as exc:
                    log.warning("suno salvage poll: %s", exc)
        if not cached:
            return 0
        music_assets = [a for a in (fresh.get("assets") or []) if a.get("kind") == "music"]
        if music_assets:
            for asset, url in zip(music_assets, cached):
                await self.store.update_asset_url(asset["id"], url)
            for url in cached[len(music_assets) :]:
                await self.store.add_asset(project_id=project["id"], kind="music", title="soundtrack", url=url)
        else:
            for url in cached:
                await self.store.add_asset(project_id=project["id"], kind="music", title="soundtrack", url=url)
        await self.assemble_game(project)
        return len(cached)

    async def _cache_audio_urls(self, urls: list[str]) -> list[str]:
        seen: set[str] = set()
        cached: list[str] = []
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                cached.append(await self.api.cache_audio(url))
            except Exception as exc:
                log.warning("cache audio %s: %s", url[:100], exc)
        return cached

    async def make_gdd(self, project: dict, plan: dict) -> dict:
        fallback = default_gdd(project, plan)
        job = await self.store.create_job(
            project_id=project["id"],
            agent="game-designer",
            provider="auto",
            model=self.api.pick_review_model(),
            kind="gdd",
            input={"prompt": project.get("prompt"), "plan": plan},
        )
        self.hub.publish(project["id"], "job", job)
        try:
            text, used = await self.api.chat(
                self.api.pick_review_model(),
                build_review_prompt("gdd", {**project, "plan": plan}, fallback, {}),
            )
            gdd = parse_json_obj(text, fallback)
        except Exception as exc:
            gdd = fallback
            await self.store.finish_job(job["id"], "error", {"gdd": gdd}, str(exc))
            await self.note(project["id"], "game-designer", f"ТЗ по шаблону: {exc}")
            return {**plan, "gdd": gdd}
        job = await self.store.finish_job(job["id"], "done", {"model": used, "gdd": gdd})
        self.hub.publish(project["id"], "job", job)
        await self.note(project["id"], "game-designer", format_review_note("gdd", gdd))
        return {**plan, "gdd": gdd}

    async def review_game(self, project: dict) -> dict | None:
        project_id = project["id"]
        fresh = self.api.publicize_project(await self.store.get_project(project_id)) or project
        plan = dict(fresh.get("plan") or {})
        if not plan.get("gdd"):
            plan = await self.make_gdd(fresh, plan)
            await self.store.update_project(
                project_id, fresh.get("status") or "ready", fresh.get("title") or "", plan
            )
        gdd = plan.get("gdd") if isinstance(plan.get("gdd"), dict) else default_gdd(fresh, plan)
        inspection = inspect_build(fresh)
        playtest_fb = {
            "score": 5,
            "fun": 5,
            "clarity": 5,
            "would_play_again": False,
            "session_notes": "Не удалось получить живой плейтест.",
            "bugs": [],
            "praise": [],
            "verdict": "rough",
        }
        spec_fb = {
            "match_percent": 0,
            "passed": [],
            "failed": [],
            "blockers": [],
            "verdict": "rework",
            "summary": "Сверку не удалось выполнить.",
        }
        playtest, spec = await asyncio.gather(
            self._review_agent(fresh, "playtester", "playtest", "playtest", gdd, inspection, playtest_fb),
            self._review_agent(fresh, "spec-review", "spec-review", "spec", gdd, inspection, spec_fb),
        )
        plan["reviews"] = {"inspection": inspection, "playtest": playtest, "spec": spec}
        await self.store.update_project(
            project_id, fresh.get("status") or "ready", fresh.get("title") or "", plan
        )
        self.hub.publish(project_id, "review", plan["reviews"])
        return plan

    async def _review_agent(
        self,
        project: dict,
        agent: str,
        job_kind: str,
        prompt_kind: str,
        gdd: dict,
        inspection: dict,
        fallback: dict,
    ) -> dict:
        model = self.api.pick_review_model()
        job = await self.store.create_job(
            project_id=project["id"],
            agent=agent,
            provider="auto",
            model=model,
            kind=job_kind,
            input={"gdd": gdd, "inspection": inspection},
        )
        self.hub.publish(project["id"], "job", job)
        try:
            text, used = await self.api.chat(
                model,
                build_review_prompt(prompt_kind, project, gdd, inspection),
            )
            data = parse_json_obj(text, fallback)
        except Exception as exc:
            await self.store.finish_job(job["id"], "error", fallback, str(exc))
            await self.note(project["id"], agent, f"{job_kind}: {exc}")
            self.hub.publish(project["id"], "job", {**job, "status": "error", "error": str(exc)})
            return {**fallback, "error": str(exc)}
        job = await self.store.finish_job(job["id"], "done", {"model": used, **data})
        self.hub.publish(project["id"], "job", job)
        await self.note(project["id"], agent, format_review_note(prompt_kind, data))
        return data

    async def purge_foreign_models(self, project: dict) -> int:
        ids = [
            str(asset["id"])
            for asset in (project.get("assets") or [])
            if asset.get("kind") in {"model", "mesh"}
            and asset.get("id")
            and not in_project_context(asset, project)
        ]
        if not ids:
            return 0
        return await self.store.delete_assets(project["id"], ids)

    async def fill_scene_from_catalog(self, project: dict) -> dict:
        project_id = project["id"]
        fresh = await self.store.get_project(project_id)
        if not fresh:
            return {"added": 0, "counts": {}}
        if project.get("plan"):
            fresh["plan"] = project["plan"]
        self.api.publicize_project(fresh)
        removed = await self.purge_foreign_models(fresh)
        if removed:
            await self.note(
                project_id,
                "catalog",
                f"Убрано {removed} моделей не из контекста этого проекта.",
            )
            fresh = await self.store.get_project(project_id) or fresh
            if project.get("plan"):
                fresh["plan"] = project["plan"]
            self.api.publicize_project(fresh)
        genre = detect_genre(fresh)
        slots = slot_need(fresh)
        counts = {role: 0 for role in slots}
        existing: set[str] = set()
        for asset in fresh.get("assets") or []:
            if asset.get("kind") not in {"model", "mesh"}:
                continue
            if not in_project_context(asset, fresh):
                continue
            meta = asset.get("meta") or {}
            if meta.get("path"):
                existing.add(str(meta["path"]))
            if asset.get("url"):
                existing.add(str(asset["url"]))
            tagged = str(meta.get("scene_role") or "").strip().lower()
            if tagged == "car" and not looks_like_car(asset):
                continue
            if tagged and tagged != "car" and looks_like_car(asset):
                continue
            if tagged in counts and (meta.get("vision_fit") is True):
                counts[tagged] += 1
        counts["car"] = max(
            int(counts.get("car") or 0),
            sum(
                1
                for a in (fresh.get("assets") or [])
                if looks_like_race_car(a)
                and in_project_context(a, fresh)
                and (a.get("meta") or {}).get("vision_fit") is True
            ),
        )
        if "car" not in slots:
            counts.pop("car", None)
        plan_rows = scene_search_plan(fresh)
        job = await self.store.create_job(
            project_id=project_id,
            agent="catalog",
            provider="catalog",
            model="catalog-scene",
            kind="catalog",
            input=[{"role": role, "queries": queries} for role, _animated, queries in plan_rows],
        )
        self.hub.publish(project_id, "job", job)
        added = 0
        searched: list[str] = []
        rejected: list[str] = []
        brief = " ".join(
            str(x or "")
            for x in (
                fresh.get("title"),
                (fresh.get("plan") or {}).get("logline"),
                fresh.get("prompt"),
                detect_genre(fresh),
            )
        )[:400]
        try:
            for role, animated, queries in plan_rows:
                need = max(0, int(slots.get(role, 2)) - int(counts.get(role) or 0))
                if need <= 0:
                    continue
                candidates: list[dict] = []
                seen_cand: set[str] = set()
                for query in queries:
                    text = str(query or "").strip()
                    if not text or foreign_hay(text, genre) or len(candidates) >= max(need * 3, 6):
                        continue
                    searched.append(f"{role}:{text}")
                    try:
                        hits = await self.api.search_catalog(
                            text, min(8, need + 4), animated=animated, by_name=(role == "car")
                        )
                    except Exception as exc:
                        log.warning("scene catalog %s %r: %s", role, text, exc)
                        continue
                    for hit in rank_catalog_hits(hits, role):
                        path = str(hit.get("path") or "")
                        url = hit.get("view_url") or hit.get("preview_url") or ""
                        if not path or not url or path in existing or url in existing or path in seen_cand:
                            continue
                        if foreign_hay(hit_hay(hit), genre):
                            continue
                        seen_cand.add(path)
                        candidates.append(hit)
                        if len(candidates) >= max(need * 3, 6):
                            break
                if role == "car":
                    named: list[dict] = []
                    rest: list[dict] = []
                    for hit in candidates:
                        if looks_like_race_car(hit):
                            hit["vision_fit"] = True
                            hit["vision_score"] = 8
                            hit["vision_pose"] = "ok"
                            hit["vision_label"] = "car-by-name"
                            hit["vision_size_ok"] = True
                            named.append(hit)
                        else:
                            rest.append(hit)
                    judged = named + await self.api.judge_catalog_hits(
                        rest,
                        role=role,
                        expect=ROLE_EXPECT.get(role) or role,
                        brief=brief,
                    )
                else:
                    judged = await self.api.judge_catalog_hits(
                        candidates,
                        role=role,
                        expect=ROLE_EXPECT.get(role) or role,
                        brief=brief,
                    )
                good = [
                    hit
                    for hit in judged
                    if hit.get("vision_fit") and int(hit.get("vision_score") or 0) >= 7
                ]
                for hit in judged:
                    if hit in good:
                        continue
                    rejected.append(
                        f"{role}:{hit.get('name') or hit.get('path')} "
                        f"→ {hit.get('vision_label') or 'нет'} ({hit.get('vision_score') or 0}/10)"
                    )
                for hit in good:
                    path = str(hit.get("path") or "")
                    url = hit.get("view_url") or hit.get("preview_url") or ""
                    if not path or not url or path in existing or need <= 0:
                        continue
                    if foreign_hay(hit_hay(hit), genre):
                        continue
                    existing.add(path)
                    existing.add(url)
                    meta = {**hit, "scene_role": role}
                    meta.update(pose_from_bbox(role, hit.get("bbox") or ((hit.get("geometry") or {}) if isinstance(hit.get("geometry"), dict) else {}).get("bbox")))
                    asset = await self.store.add_asset(
                        project_id=project_id,
                        job_id=job["id"],
                        kind="model",
                        title=hit.get("name") or "",
                        url=url,
                        meta=meta,
                    )
                    self.hub.publish(project_id, "asset", asset)
                    added += 1
                    need -= 1
                    counts[role] = int(counts.get(role) or 0) + 1
                    if need <= 0:
                        break
        except Exception as exc:
            job = await self.store.finish_job(job["id"], "error", {"searched": searched, "added": added}, str(exc))
            self.hub.publish(project_id, "job", job)
            log.warning("fill scene catalog: %s", exc)
            return {"added": added, "counts": counts, "error": str(exc)}
        job = await self.store.finish_job(
            job["id"],
            "done" if added or any(counts.values()) else "error",
            {"searched": searched, "added": added, "counts": counts, "rejected": rejected[:24]},
            None if added or any(counts.values()) else "catalog returned no scene models",
        )
        self.hub.publish(project_id, "job", job)
        if added or rejected:
            kept = ", ".join(f"{role}={counts.get(role, 0)}" for role in slots)
            skip = ("; отказано: " + ", ".join(rejected[:8])) if rejected else ""
            await self.note(
                project_id,
                "catalog",
                f"Каталог проверил превью. Взято: {kept}{skip}",
            )
        return {"added": added, "counts": counts}

    async def audit_placement_pose(self, project: dict) -> int:
        genre = detect_genre(project)
        cast = pick_cast(project)
        wanted = {str((cast.get("hero") or {}).get("url") or "")}
        wanted.update(str(item.get("url") or "") for item in (cast.get("placements") or []))
        wanted.discard("")
        brief = project_brief(project)[:400]
        checked = 0
        for asset in project.get("assets") or []:
            url = str(asset.get("url") or "")
            if url not in wanted:
                continue
            meta = dict(asset.get("meta") or {})
            if meta.get("vision_pose"):
                continue
            preview = meta.get("preview_url") or ""
            if not preview:
                continue
            role = str(meta.get("scene_role") or _role(asset, genre))
            hit = {
                **meta,
                "name": asset.get("title") or "",
                "preview_url": preview,
                "bbox": meta.get("bbox") or ((meta.get("geometry") or {}) if isinstance(meta.get("geometry"), dict) else {}).get("bbox") or [],
            }
            judged = await self.api.judge_catalog_hits(
                [hit],
                role=role,
                expect=ROLE_EXPECT.get(role) or role,
                brief=brief,
            )
            if not judged:
                continue
            row = judged[0]
            for key, value in row.items():
                if str(key).startswith("vision_"):
                    meta[key] = value
            meta.update(pose_from_bbox(role, hit.get("bbox")))
            await self.store.update_asset_url(asset["id"], url, meta)
            asset["meta"] = meta
            checked += 1
        return checked

    async def assemble_game(self, project: dict) -> dict | None:
        project_id = project["id"]
        scene = await self.fill_scene_from_catalog(project)
        fresh = await self.store.get_project(project_id)
        if not fresh:
            return None
        self.api.publicize_project(fresh)
        posed = await self.audit_placement_pose(fresh)
        if posed:
            await self.note(
                project_id,
                "catalog",
                f"Рендер-проверка позы и размера: {posed} моделей.",
            )
            fresh = await self.store.get_project(project_id) or fresh
            self.api.publicize_project(fresh)
        try:
            url = write_game(fresh)
        except Exception as exc:
            log.warning("assemble game: %s", exc)
            await self.note(project_id, "orchestrator", f"Мини-игру не удалось собрать: {exc}")
            return None
        cast = pick_cast(fresh)
        genre = detect_genre(fresh)
        play = sanitize_play(fresh)
        await self.store.replace_assets_of_kind(project_id, "game")
        asset = await self.store.add_asset(
            project_id=project_id,
            kind="game",
            title=fresh.get("title") or "Mini game",
            url=url,
            meta={
                "engine": "three",
                "mode": genre,
                "play": play,
                "controls": play.get("hint") or "",
                "placements": len(cast.get("placements") or []),
                "catalog_counts": cast.get("counts") or {},
                "scene_added": scene.get("added") or 0,
            },
        )
        self.hub.publish(project_id, "asset", asset)
        placed = len(cast.get("placements") or [])
        how = play.get("hint") or "WASD"
        splash = "картинка — сплеш, ролик — превью. " if pick_url(fresh, ("image",)) or pick_url(fresh, ("video",)) else ""
        await self.note(
            project_id,
            "orchestrator",
            f"Мини-игра ({genre}) собрана из {placed} моделей каталога. {splash}Открой вкладку «Игра» или {url}. {how}",
        )
        return asset

    async def chat(self, project: dict, message: str, model: str) -> tuple[str, str]:
        project_id = project["id"]
        extra = ""
        rebuilt = False
        if wants_game(message):
            asset = await self.assemble_game(project)
            rebuilt = bool(asset)
            extra += (
                "\nThe engine JUST rebuilt the playable mini-game. "
                "Do not propose a new production plan. Describe the current build only.\n"
            )
        if wants_review(message):
            await self.review_game(project)
            extra += (
                "\nPlaytesters and spec-review just ran. Summarize scores in 4 sentences. No JSON.\n"
            )
        fresh = self.api.publicize_project(await self.store.get_project(project_id)) or project
        inspection = inspect_build(fresh)
        cast = pick_cast(fresh)
        extra += (
            f"\nInspection: genre={inspection.get('genre')} "
            f"hero={((cast.get('hero') or {}).get('title') or 'none')} "
            f"win={inspection.get('win_condition')} "
            f"controls={inspection.get('controls')} "
            f"rebuilt={rebuilt}\n"
        )
        system = CHAT_SYSTEM.replace("{context}", f"{context_blob(fresh)}{extra}")
        messages = [{"role": "system", "content": system}, *history_messages(fresh)]
        text, used = await self.api.chat(model, messages)
        return text, used

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
        "play": {},
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
    merged = {**project, "plan": plan}
    genre = detect_genre(merged)
    plan["genre"] = genre
    queries = filter_queries(
        [str(q).strip() for q in (plan.get("catalog_queries") or []) if str(q).strip() and len(str(q)) < 60],
        genre,
    )
    if genre == "racing":
        if len(queries) < 4:
            queries = [
                "bmw sports car",
                "supercar vehicle",
                "crash barrier",
                "pine tree",
                "grandstand building",
                "traffic cone",
            ]
        if not str(plan.get("voice_line") or "").strip():
            plan["voice_line"] = "Ready, set, go!"
        if not str(plan.get("music_prompt") or "").strip():
            plan["music_prompt"] = "upbeat electronic racing soundtrack, high BPM"
    elif genre == "maze":
        if len(queries) < 4:
            queries = [
                "hedge bush",
                "maze hedge",
                "arcade character",
                "garden lantern",
                "green bush",
                "ghost character",
            ]
        if not str(plan.get("voice_line") or "").strip():
            plan["voice_line"] = "Собери все точки. Не врезайся в стены."
        if not str(plan.get("music_prompt") or "").strip():
            plan["music_prompt"] = "playful 8-bit arcade maze soundtrack, looping"
    if not isinstance(plan.get("play"), dict):
        plan["play"] = {}
    plan["play"] = sanitize_play({**merged, "plan": plan})
    plan["catalog_queries"] = queries[:10]
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
