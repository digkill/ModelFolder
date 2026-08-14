from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.registry import find_model

URL_HINTS = (".png", ".jpg", ".webp", ".mp4", ".mp3", ".wav", ".glb", ".webm", "cdn", "storage")


class Clients:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))

    async def aclose(self) -> None:
        await self.http.aclose()

    def pick_orchestrator(self) -> str:
        pref = (self.cfg.studio_orchestrator or "auto").strip().lower()
        if pref and pref != "auto":
            return pref
        if self.cfg.kie_key:
            return "claude-opus-5"
        if self.cfg.anthropic_key:
            return "anthropic-claude"
        if self.cfg.grok_key:
            return "xai-grok"
        if self.cfg.openai_api_key:
            return "openai-gpt"
        return "claude-opus-5"

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

    async def create_kie_task(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.cfg.kie_key:
            raise RuntimeError("KIE_API_KEY is empty")
        raw = await self._do_json(
            "POST",
            f"{self.cfg.kie_base}/jobs/createTask",
            self.cfg.kie_key,
            {"model": model, "input": payload},
        )
        task_id = ((raw.get("data") or {}) if isinstance(raw, dict) else {}).get("taskId") or ""
        if not task_id:
            raise RuntimeError(f"kie createTask failed: {_truncate(raw, 400)}")
        return await self._poll_kie(task_id)

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
                raise RuntimeError(f"kie task failed: {_truncate(raw, 400)}")
        raise RuntimeError(f"kie task timeout: {task_id}")

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

    async def search_catalog(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        if limit <= 0:
            limit = 12
        auth = self.cfg.catalog_basic
        try:
            raw = await self._do_json(
                "POST",
                f"{self.cfg.catalog}/api/catalog/search",
                "",
                {"query": query, "limit": limit, "offset": 0},
                auth=auth,
            )
        except Exception:
            raw = await self._do_json(
                "GET",
                f"{self.cfg.catalog}/api/models?tags={quote(query)}",
                "",
                None,
                auth=auth,
            )
        items = raw.get("items") if isinstance(raw, dict) else []
        out: list[dict[str, Any]] = []
        for item in items or []:
            view = item.get("view_url") or ""
            preview = item.get("preview_url") or ""
            if view and not view.startswith("http"):
                view = self.cfg.catalog + view
            if preview and not preview.startswith("http"):
                preview = self.cfg.catalog + preview
            out.append(
                {
                    "name": item.get("name") or "",
                    "path": item.get("path") or "",
                    "ext": item.get("ext") or "",
                    "view_url": view,
                    "preview_url": preview,
                    "tag_list": item.get("tag_list") or [],
                    "score": item.get("score") or 0,
                }
            )
        return out

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
