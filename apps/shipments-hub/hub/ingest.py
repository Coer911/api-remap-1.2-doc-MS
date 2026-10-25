"""Откуда хаб узнаёт об отгрузках.

1. Вебхук МойСклад (быстро, только на платных тарифах): тело уведомления
   разбирается на события и кладётся в очередь. Ответ — сразу, обработка —
   потом: МойСклад ждёт не дольше 1500 мс и повторяет с тем же requestId.
2. Опрос (страховка и бесплатные тарифы): раз в HUB_POLL_SECONDS берём
   отгрузки с updated >= курсора. Курсор — значение updated последней
   увиденной отгрузки, в формате и часовом поясе самого МойСклад.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import Event
from .moysklad import MoySklad, entity_id
from .store import Store

log = logging.getLogger("hub.ingest")
MSK = ZoneInfo("Europe/Moscow")
CURSOR = "cursor:demand"
ENTITIES = {"demand"}


def parse_webhook(payload: dict) -> list[Event]:
    """Тело вебхука → события по интересным хабу сущностям."""
    out = []
    for e in payload.get("events", []):
        meta = e.get("meta", {})
        if meta.get("type") not in ENTITIES or not meta.get("href"):
            continue
        out.append(Event(entity_id=entity_id(meta["href"]), action=e.get("action", "UPDATE"),
                         fields=e.get("updatedFields"), source="webhook", entity=meta["type"]))
    return out


def accept_webhook(store: Store, payload: dict, request_id: str) -> int:
    """Положить события в очередь. Возвращает, сколько новых."""
    added = 0
    for i, event in enumerate(parse_webhook(payload)):
        rid = f"{request_id or 'no-request-id'}:{i}"
        added += store.enqueue(event, rid)
    return added


def initial_cursor(lookback_hours: int) -> str:
    return (datetime.now(MSK) - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d %H:%M:%S")


async def poll(ms: MoySklad, store: Store, lookback_hours: int = 0) -> int:
    """Один круг опроса: новые изменения отгрузок → очередь. Возвращает, сколько новых."""
    cursor = store.get(CURSOR)
    if cursor is None:
        cursor = initial_cursor(lookback_hours)
        store.set(CURSOR, cursor)
        log.info("первый запуск: курсор опроса %s", cursor)
    newest, added = cursor, 0
    # updated>= включительно: последняя отгрузка придёт снова, её отсечёт UNIQUE в очереди
    async for row in ms.iterate("entity/demand", limit=100,
                                filter=f"updated>={cursor[:19]}", order="updated,asc"):
        updated = row.get("updated", "")
        added += store.enqueue(Event(entity_id=row["id"], action="POLL", fields=None,
                                     source="poll"), f"poll:{updated}")
        newest = max(newest, updated)
    if newest != cursor:
        store.set(CURSOR, newest)
    return added
