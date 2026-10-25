"""Откуда хаб узнаёт об изменениях.

1. Вебхук МойСклад (быстро, только на платных тарифах): тело уведомления → события
   в очередь, ответ сразу. МойСклад ждёт не дольше 1500 мс и повторяет с тем же
   requestId (dictionaries/_webhook.md).
2. Опрос (страховка и бесплатные тарифы): раз в HUB_POLL_SECONDS — сущности
   с updated >= курсора, отдельный курсор на каждый тип.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .entities import TYPES
from .events import Event
from .moysklad import MoySklad, entity_id
from .storage import Storage

log = logging.getLogger("hub.ingest")
MSK = ZoneInfo("Europe/Moscow")


def parse_webhook(payload: dict) -> list[Event]:
    out = []
    for e in payload.get("events", []):
        meta = e.get("meta", {})
        if meta.get("type") not in TYPES or not meta.get("href"):
            continue
        out.append(Event(entity=meta["type"], entity_id=entity_id(meta["href"]),
                         action=e.get("action", "UPDATE"), fields=e.get("updatedFields"),
                         source="webhook"))
    return out


async def accept_webhook(storage: Storage, payload: dict, request_id: str) -> int:
    """Положить события в очередь. Возвращает, сколько новых."""
    added = 0
    for i, ev in enumerate(parse_webhook(payload)):
        added += await storage.enqueue(ev, f"{request_id or 'no-request-id'}:{i}")
    return added


def initial_cursor(lookback_hours: int) -> str:
    return (datetime.now(MSK) - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d %H:%M:%S")


async def poll(ms: MoySklad, storage: Storage, type_: str, lookback_hours: int = 0) -> int:
    """Один круг опроса типа: новые изменения → очередь. Возвращает, сколько новых."""
    key = f"cursor:{type_}"
    cursor = await storage.get(key)
    if cursor is None:
        cursor = initial_cursor(lookback_hours)
        await storage.set(key, cursor)
        log.info("первый опрос %s: с %s", type_, cursor)
    newest, added = cursor, 0
    # updated>= включительно: последняя запись придёт снова, её отсечёт уникальность в очереди
    async for row in ms.list(type_, filter=f"updated>={cursor[:19]}", order="updated,asc",
                             limit=1000):
        updated = row.get("updated", "")
        added += await storage.enqueue(Event(entity=type_, entity_id=row["id"], action="POLL",
                                             source="poll"), f"poll:{updated}")
        newest = max(newest, updated)
    if newest != cursor:
        await storage.set(key, newest)
    return added
