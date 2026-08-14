from __future__ import annotations

import asyncio
import json
from collections import defaultdict

from app.db import dump


class Hub:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[str]]] = defaultdict(set)

    def subscribe(self, project_id: str) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self._subs[project_id].add(queue)
        return queue

    def unsubscribe(self, project_id: str, queue: asyncio.Queue[str]) -> None:
        subs = self._subs.get(project_id)
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            self._subs.pop(project_id, None)

    def publish(self, project_id: str, typ: str, payload: object) -> None:
        data = json.dumps(
            {"type": typ, "project_id": project_id, "payload": dump(payload)},
            ensure_ascii=False,
            default=str,
        )
        for queue in list(self._subs.get(project_id, ())):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass
