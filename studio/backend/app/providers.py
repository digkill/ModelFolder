from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.config import Settings
from app.registry import find_model

INTERNAL_CATALOG_PREFIXES = (
    "http://modelfolder-web:8000",
    "http://modelfolder-web",
    "https://modelfolder-web:8000",
    "http://app:8000",
    "http://catalog:8000",
)

URL_HINTS = (".png", ".jpg", ".webp", ".mp4", ".mp3", ".wav", ".glb", ".webm", "cdn", "storage")
AUDIO_KEYS = (
    "audioUrl",
    "audio_url",
    "sourceAudioUrl",
    "source_audio_url",
    "streamAudioUrl",
    "stream_audio_url",
    "sourceStreamAudioUrl",
    "source_stream_audio_url",
)
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a"}
AUDIO_DIR = Path("/tmp/studio-audio")


class Clients:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))

    async def aclose(self) -> None:
        await self.http.aclose()

    def publicize_url(self, url: str) -> str:
        text = (url or "").strip()
        if not text:
            return ""
        prefixes = [self.cfg.catalog, *INTERNAL_CATALOG_PREFIXES]
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix) :] or "/"
                break
        public = self.cfg.catalog_public
        if public and text.startswith("/"):
            return public + text
        return text

    def publicize_project(self, project: dict[str, Any] | None) -> dict[str, Any] | None:
        if not project:
            return project
        for asset in project.get("assets") or []:
            asset["url"] = self.publicize_url(asset.get("url") or "")
            meta = asset.get("meta")
            if isinstance(meta, dict):
                for key in ("view_url", "preview_url"):
                    if meta.get(key):
                        meta[key] = self.publicize_url(str(meta[key]))
        return project

    def pick_orchestrator(self) -> str:
        pref = (self.cfg.studio_orchestrator or "auto").strip().lower()
        if pref and pref != "auto":
            return pref
        if self.cfg.anthropic_key:
            return "anthropic-claude"
        if self.cfg.kie_key:
            return "claude-opus-5"
        if self.cfg.grok_key:
            return "xai-grok"
        if self.cfg.openai_api_key:
            return "openai-gpt"
        return "claude-opus-5"

    def pick_review_model(self) -> str:
        """GDD / playtest / spec — same brain as the producer."""
        return self.pick_orchestrator()

    async def chat(self, model_id: str, messages: list[dict[str, str]]) -> tuple[str, str]:
        model = find_model(model_id)
        provider = model.get("provider") or "kie"
        if provider == "openai":
            return await self._openai_chat(messages), "openai"
        if provider == "anthropic":
            return await self._anthropic_chat(messages), "anthropic"
        if provider == "grok":
            return await self._grok_chat(messages), "grok"
        try:
            return await self._kie_chat(model, messages), f"kie:{model['id']}"
        except Exception as err:
            try:
                return await self._fallback_chat(messages), "fallback"
            except Exception:
                raise err from None

    async def _fallback_chat(self, messages: list[dict[str, str]]) -> str:
        if self.cfg.anthropic_key:
            return await self._anthropic_chat(messages)
        if self.cfg.openai_api_key:
            return await self._openai_chat(messages)
        if self.cfg.grok_key:
            return await self._grok_chat(messages)
        raise RuntimeError("no chat provider keys configured")

    async def _openai_chat(self, messages: list[dict[str, str]]) -> str:
        if not self.cfg.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is empty")
        return await self._post_chat(
            f"{self.cfg.openai_base}/chat/completions",
            self.cfg.openai_api_key,
            {"model": "gpt-4o-mini", "temperature": 0.4, "messages": messages},
        )

    async def _grok_chat(self, messages: list[dict[str, str]]) -> str:
        if not self.cfg.grok_key:
            raise RuntimeError("GROK_API_KEY is empty")
        return await self._post_chat(
            f"{self.cfg.grok_base}/chat/completions",
            self.cfg.grok_key,
            {"model": "grok-4-6", "messages": messages},
        )

    async def _anthropic_chat(self, messages: list[dict[str, str]]) -> str:
        if not self.cfg.anthropic_key:
            raise RuntimeError("ANTHROPIC_API_KEY is empty")
        system = ""
        api_msgs: list[dict[str, str]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system = msg.get("content") or ""
                continue
            role = msg.get("role") or "user"
            if role not in {"user", "assistant"}:
                role = "user"
            api_msgs.append({"role": role, "content": msg.get("content") or ""})
        body: dict[str, Any] = {"model": "claude-sonnet-4-6", "max_tokens": 4000, "messages": api_msgs}
        if system:
            body["system"] = system
        raw = await self._do_json(
            "POST",
            "https://api.anthropic.com/v1/messages",
            self.cfg.anthropic_key,
            body,
            extra={"anthropic-version": "2023-06-01"},
        )
        content = (raw.get("content") or []) if isinstance(raw, dict) else []
        if not content:
            raise RuntimeError(f"empty anthropic response: {_truncate(raw, 400)}")
        return content[0].get("text") or ""

    async def _kie_chat(self, model: dict, messages: list[dict[str, str]]) -> str:
        if not self.cfg.kie_key:
            raise RuntimeError("KIE_API_KEY is empty")
        path = model.get("chat_path") or "/chat/completions"
        return await self._post_chat(
            f"{self.cfg.kie_base}{path}",
            self.cfg.kie_key,
            {
                "model": model.get("kie_model") or model["id"],
                "messages": messages,
                "stream": False,
            },
        )

    async def _post_chat(self, url: str, key: str, body: dict[str, Any]) -> str:
        raw = await self._do_json("POST", url, key, body)
        choices = raw.get("choices") if isinstance(raw, dict) else None
        if not choices:
            raise RuntimeError(f"empty chat response: {_truncate(raw, 400)}")
        return _stringify(choices[0].get("message", {}).get("content"))

    async def create_media_task(self, model: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        if (model.get("task_api") or "") == "suno" or model.get("kind") == "music":
            return await self.create_suno_task(payload, model.get("kie_model") or "V5")
        return await self.create_kie_task(model.get("kie_model") or model["id"], payload)

    async def create_kie_task(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.cfg.kie_key:
            raise RuntimeError("KIE_API_KEY is empty")
        body: dict[str, Any] = {"model": model, "input": payload}
        if self.cfg.kie_callback_url:
            body["callBackUrl"] = self.cfg.kie_callback_url
        raw = await self._do_json(
            "POST",
            f"{self.cfg.kie_base}/jobs/createTask",
            self.cfg.kie_key,
            body,
        )
        if isinstance(raw, dict) and raw.get("code") not in (None, 200):
            raise RuntimeError(f"kie createTask failed: {_truncate(raw, 400)}")
        task_id = ((raw.get("data") or {}) if isinstance(raw, dict) else {}).get("taskId") or ""
        if not task_id:
            raise RuntimeError(f"kie createTask failed: {_truncate(raw, 400)}")
        return await self._poll_kie(task_id)

    async def create_suno_task(self, payload: dict[str, Any], version: str = "V5") -> dict[str, Any]:
        if not self.cfg.kie_key:
            raise RuntimeError("KIE_API_KEY is empty")
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise RuntimeError("suno prompt is empty")
        raw = await self._do_json(
            "POST",
            f"{self.cfg.kie_base}/generate",
            self.cfg.kie_key,
            {
                "prompt": prompt,
                "customMode": False,
                "instrumental": bool(payload.get("instrumental", True)),
                "model": version,
                "callBackUrl": self.cfg.kie_callback_url,
            },
        )
        if isinstance(raw, dict) and raw.get("code") not in (None, 200):
            raise RuntimeError(f"suno generate failed: {_truncate(raw, 400)}")
        task_id = ((raw.get("data") or {}) if isinstance(raw, dict) else {}).get("taskId") or ""
        if not task_id:
            raise RuntimeError(f"suno generate failed: {_truncate(raw, 400)}")
        result = await self._poll_suno(task_id)
        cached: list[str] = []
        for url in result.get("urls") or []:
            try:
                cached.append(await self.cache_audio(url))
            except Exception:
                cached.append(url)
        result["urls"] = cached or result.get("urls") or []
        return result

    async def _poll_kie(self, task_id: str) -> dict[str, Any]:
        deadline = asyncio.get_event_loop().time() + 4 * 60
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(4)
            try:
                raw = await self._do_json(
                    "GET",
                    f"{self.cfg.kie_base}/jobs/recordInfo?taskId={quote(task_id)}",
                    self.cfg.kie_key,
                    None,
                )
            except Exception:
                continue
            state = str(((raw.get("data") or {}) if isinstance(raw, dict) else {}).get("state") or "").lower()
            urls = _extract_urls(raw)
            if state in {"success", "succeed", "completed"} or (urls and state in {"", "success"}):
                return {"task_id": task_id, "status": "done", "urls": urls, "raw": raw}
            if state in {"fail", "failed"}:
                data = (raw.get("data") or {}) if isinstance(raw, dict) else {}
                raise RuntimeError(str(data.get("failMsg") or data.get("failCode") or "kie task failed"))
        raise RuntimeError(f"kie task timeout: {task_id}")

    async def _poll_suno(self, task_id: str) -> dict[str, Any]:
        deadline = asyncio.get_event_loop().time() + 6 * 60
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(5)
            try:
                raw = await self._do_json(
                    "GET",
                    f"{self.cfg.kie_base}/generate/record-info?taskId={quote(task_id)}",
                    self.cfg.kie_key,
                    None,
                )
            except Exception:
                continue
            data = (raw.get("data") or {}) if isinstance(raw, dict) else {}
            status = str(data.get("status") or "").upper()
            urls = _extract_audio_urls(raw)
            if status in {"SUCCESS", "FIRST_SUCCESS"} and urls:
                return {"task_id": task_id, "status": "done", "urls": urls, "raw": raw}
            if status in {"CREATE_TASK_FAILED", "GENERATE_AUDIO_FAILED", "SENSITIVE_WORD_ERROR"}:
                raise RuntimeError(f"suno task failed: {_truncate(raw, 400)}")
        raise RuntimeError(f"suno task timeout: {task_id}")

    async def speak(self, text: str) -> dict[str, Any]:
        """Озвучка: OpenAI TTS. Kie ElevenLabs сейчас отвечает Internal Error."""
        line = (text or "").strip()
        if not line:
            raise RuntimeError("tts text is empty")
        if not self.cfg.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is empty")
        resp = await self.http.post(
            f"{self.cfg.openai_base}/audio/speech",
            headers={"Authorization": f"Bearer {self.cfg.openai_api_key}"},
            json={"model": "tts-1", "voice": "nova", "input": line[:4096]},
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"openai tts -> {resp.status_code} {_truncate(resp.text, 300)}")
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{uuid4().hex}.mp3"
        (AUDIO_DIR / name).write_bytes(resp.content)
        return {
            "task_id": name,
            "status": "done",
            "urls": [f"/app/api/v1/audio/{name}"],
            "raw": {"provider": "openai", "model": "tts-1", "file": name},
        }

    async def cache_audio(self, url: str) -> str:
        text = (url or "").strip()
        if not text:
            raise RuntimeError("empty audio url")
        if text.startswith("/app/api/v1/audio/"):
            return text
        resp = await self.http.get(
            text,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 ModelFolderStudio/1.0"},
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"audio fetch {resp.status_code} {_truncate(resp.text, 200)}")
        ctype = (resp.headers.get("content-type") or "").lower()
        if "image" in ctype:
            raise RuntimeError(f"audio url returned image: {text[:120]}")
        ext = ".mp3"
        if "wav" in ctype:
            ext = ".wav"
        elif "ogg" in ctype:
            ext = ".ogg"
        elif "mp4" in ctype or "m4a" in ctype or "aac" in ctype:
            ext = ".m4a"
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{uuid4().hex}{ext}"
        (AUDIO_DIR / name).write_bytes(resp.content)
        if len(resp.content) < 2000:
            raise RuntimeError("audio file too small")
        return f"/app/api/v1/audio/{name}"

    async def meshy_text_to_3d(self, prompt: str) -> dict[str, Any]:
        if not self.cfg.meshy_api_key:
            raise RuntimeError("MESHY_API_KEY is empty")
        created = await self._do_json(
            "POST",
            f"{self.cfg.meshy_base}/text-to-3d",
            self.cfg.meshy_api_key,
            {"mode": "preview", "prompt": prompt, "art_style": "realistic"},
        )
        result_id = created.get("result") if isinstance(created, dict) else ""
        if not result_id:
            raise RuntimeError(f"meshy create failed: {_truncate(created, 400)}")
        deadline = asyncio.get_event_loop().time() + 6 * 60
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(5)
            try:
                info = await self._do_json(
                    "GET",
                    f"{self.cfg.meshy_base}/text-to-3d/{result_id}",
                    self.cfg.meshy_api_key,
                    None,
                )
            except Exception:
                continue
            status = str(info.get("status") or "") if isinstance(info, dict) else ""
            model_url = info.get("model_url") if isinstance(info, dict) else ""
            if status.upper() == "SUCCEEDED" and model_url:
                return {"task_id": result_id, "status": "done", "urls": [model_url], "raw": info}
            if status.upper() == "FAILED":
                raise RuntimeError(f"meshy failed: {_truncate(info, 400)}")
        raise RuntimeError("meshy timeout")

    async def search_catalog(
        self,
        query: str,
        limit: int = 12,
        *,
        animated: bool | None = None,
        by_name: bool = False,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            limit = 12
        auth = self.cfg.catalog_basic
        body: dict[str, Any] = {
            "limit": limit,
            "offset": 0,
            "ext": ["glb", "gltf"],
            "only_with_preview": True,
        }
        if by_name:
            body["name_contains"] = query
            body["sort"] = "name"
        else:
            body["query"] = query
        if animated is not None:
            body["animated"] = animated
        try:
            raw = await self._do_json(
                "POST",
                f"{self.cfg.catalog}/api/catalog/search",
                "",
                body,
                auth=auth,
            )
        except Exception:
            qs = f"name_contains={quote(query)}&ext=glb,gltf" if by_name else f"tags={quote(query)}"
            raw = await self._do_json(
                "GET",
                f"{self.cfg.catalog}/api/models?{qs}",
                "",
                None,
                auth=auth,
            )
        items = raw.get("items") if isinstance(raw, dict) else []
        out: list[dict[str, Any]] = []
        for item in items or []:
            view = self.publicize_url(item.get("view_url") or "")
            preview = self.publicize_url(item.get("preview_url") or "")
            geom = item.get("geometry") if isinstance(item.get("geometry"), dict) else {}
            out.append(
                {
                    "name": item.get("name") or "",
                    "path": item.get("path") or "",
                    "ext": item.get("ext") or "",
                    "view_url": view,
                    "preview_url": preview,
                    "tag_list": item.get("tag_list") or [],
                    "score": item.get("score") or 0,
                    "animated": bool(item.get("animated") or geom.get("animations")),
                    "rigged": bool(item.get("rigged") or geom.get("rigged")),
                    "animations": geom.get("animations") or 0,
                    "category": item.get("category") or "",
                    "geometry": geom,
                    "bbox": geom.get("bbox") or [],
                }
            )
        return out

    def internal_catalog_url(self, url: str) -> str:
        text = (url or "").strip()
        if not text:
            return ""
        if text.startswith("/"):
            return self.cfg.catalog.rstrip("/") + text
        public = (self.cfg.catalog_public or "").rstrip("/")
        if public and text.startswith(public):
            return self.cfg.catalog.rstrip("/") + text[len(public) :]
        return text

    async def fetch_preview_data_url(self, preview_url: str) -> str:
        url = self.internal_catalog_url(preview_url)
        if not url:
            return ""
        resp = await self.http.get(url, auth=self.cfg.catalog_basic, timeout=20.0)
        if resp.status_code >= 300:
            raise RuntimeError(f"preview {resp.status_code}")
        ctype = (resp.headers.get("content-type") or "image/png").split(";", 1)[0].strip()
        if not ctype.startswith("image/") or len(resp.content) < 64:
            raise RuntimeError("preview is not an image")
        b64 = base64.b64encode(resp.content).decode("ascii")
        return f"data:{ctype};base64,{b64}"

    async def judge_catalog_hits(
        self,
        hits: list[dict[str, Any]],
        *,
        role: str,
        expect: str,
        brief: str,
    ) -> list[dict[str, Any]]:
        if not hits:
            return []
        if not self.cfg.openai_api_key:
            return [
                {
                    **hit,
                    "vision_fit": True,
                    "vision_score": 5,
                    "vision_label": "unverified",
                    "vision_pose": "ok",
                    "vision_size_ok": True,
                    "vision_meters_h": 0,
                }
                for hit in hits
            ]
        sem = asyncio.Semaphore(4)

        async def one(hit: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                return await self._judge_one_hit(hit, role=role, expect=expect, brief=brief)

        judged = list(await asyncio.gather(*[one(hit) for hit in hits[:8]]))
        judged.sort(key=lambda h: (-int(h.get("vision_fit") or 0), -int(h.get("vision_score") or 0)))
        return judged

    async def _judge_one_hit(
        self,
        hit: dict[str, Any],
        *,
        role: str,
        expect: str,
        brief: str,
    ) -> dict[str, Any]:
        preview = hit.get("preview_url") or ""
        if not preview:
            return {**hit, "vision_fit": False, "vision_score": 0, "vision_label": "no-preview", "vision_reason": "no preview"}
        bbox = hit.get("bbox") or ((hit.get("geometry") or {}) if isinstance(hit.get("geometry"), dict) else {}).get("bbox") or []
        try:
            data_url = await self.fetch_preview_data_url(str(preview))
            raw = await self._post_chat(
                f"{self.cfg.openai_base}/chat/completions",
                self.cfg.openai_api_key,
                {
                    "model": "gpt-4o-mini",
                    "temperature": 0.1,
                    "max_tokens": 260,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "You screen a 3D catalog RENDER (thumbnail) for a game. Be strict. "
                                        f"Project: {brief}. Slot: {role}. "
                                        f"Accept only if the picture clearly shows: {expect} "
                                        "The thumbnail is auto-framed, so ignore pixel size; judge SHAPE and POSE. "
                                        "Check orientation: wheels/feet/trunk should sit on the ground, not on a side or nose. "
                                        "Check proportions: car longer than tall; person/tree taller than wide; cone short. "
                                        "If the right object is lying on its side, fit=true and set pose to needs_x-90 or needs_x90. "
                                        "If exploded, unreadable, or the wrong object, fit=false and pose=unusable. "
                                        f"Raw file bbox (may be Z-up): {bbox}. "
                                        'Return JSON only: {"fit":true/false,"score":0-10,'
                                        '"label":"what you see","reason":"short",'
                                        '"upright":true/false,'
                                        '"pose":"ok|needs_x90|needs_x-90|needs_z90|upside_down|unusable",'
                                        '"yaw":0,'
                                        '"meters_h":0,'
                                        '"size_ok":true/false}. '
                                        "yaw is extra degrees 0/90/180/-90 so the front faces the camera. "
                                        "meters_h is estimated real-world height. "
                                        "score 0 = wrong object, 10 = perfect for this slot."
                                    ),
                                },
                                {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                            ],
                        }
                    ],
                },
            )
            data = _json_obj(raw)
            score = int(data.get("score") or 0)
            pose = str(data.get("pose") or "ok")
            size_ok = data.get("size_ok")
            if size_ok is None:
                size_ok = True
            fit = bool(data.get("fit")) and score >= 7 and pose != "unusable" and bool(size_ok)
            yaw = data.get("yaw") or 0
            try:
                yaw = int(yaw)
            except (TypeError, ValueError):
                yaw = 0
            if yaw not in {0, 90, 180, -90, 270}:
                yaw = 0
            meters = data.get("meters_h") or 0
            try:
                meters = float(meters)
            except (TypeError, ValueError):
                meters = 0.0
            return {
                **hit,
                "vision_fit": fit,
                "vision_score": score,
                "vision_label": str(data.get("label") or "")[:80],
                "vision_reason": str(data.get("reason") or "")[:200],
                "vision_upright": bool(data.get("upright")),
                "vision_pose": pose[:24],
                "vision_yaw": yaw,
                "vision_meters_h": meters,
                "vision_size_ok": bool(size_ok),
            }
        except Exception as exc:
            return {
                **hit,
                "vision_fit": False,
                "vision_score": 0,
                "vision_label": "error",
                "vision_reason": str(exc)[:200],
            }

    async def _do_json(
        self,
        method: str,
        url: str,
        bearer: str,
        body: dict[str, Any] | None,
        extra: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if body is not None and method != "GET":
            headers["Content-Type"] = "application/json"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if extra:
            headers.update(extra)
        resp = await self.http.request(
            method,
            url,
            headers=headers,
            json=body if method != "GET" else None,
            auth=auth,
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"{method} {url} -> {resp.status_code} {_truncate(resp.text, 400)}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}


def _json_obj(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if "```" in raw:
        raw = raw.split("```", 1)[1]
        raw = raw.removeprefix("json").strip()
        if "```" in raw:
            raw = raw.split("```", 1)[0]
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return json.dumps(value, ensure_ascii=False)


def _extract_audio_urls(blob: Any) -> list[str]:
    seen: set[str] = set()
    preferred: list[str] = []
    fallback: list[str] = []

    def consider(url: str, *, stream: bool) -> None:
        text = url.strip()
        if not text.startswith("http") or text in seen:
            return
        lower = text.lower()
        if any(x in lower for x in (".png", ".jpg", ".jpeg", ".webp", ".gif", "/image_")):
            return
        seen.add(text)
        (fallback if stream else preferred).append(text)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in AUDIO_KEYS and isinstance(item, str):
                    consider(item, stream="stream" in key.lower())
                else:
                    walk(item)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if isinstance(value, str) and value.strip().startswith("{"):
            try:
                walk(json.loads(value))
            except Exception:
                return

    walk(blob)
    return preferred + fallback


def _extract_urls(blob: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, str):
            return
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                walk(json.loads(text))
                return
            except Exception:
                pass
        if text.startswith("http") and any(hint in text for hint in URL_HINTS) and text not in seen:
            seen.add(text)
            out.append(text)

    walk(blob)
    return out


def _truncate(value: Any, n: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:n]
