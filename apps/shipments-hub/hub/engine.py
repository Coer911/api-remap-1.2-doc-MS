"""Движок: события из очереди → свежая отгрузка из МойСклад → автоматизации.

Гарантии:
  * каждая пара (автоматизация, ключ) успешно выполняется один раз — журнал runs
    и, если задано, доп. поле-отметка в самом МойСклад;
  * несколько событий по одной отгрузке за раз склеиваются в одно;
  * сбой — повтор через 1 мин, 5 мин, 30 мин, 2 ч, 6 ч, потом dead и алерт.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .automations import Automation, Context, Skip
from .models import Event, Shipment
from .moysklad import MoySkladError
from .store import iso, now_utc

log = logging.getLogger("hub.engine")


@dataclass
class Outcome:
    automation: str
    shipment: str
    status: str            # done | skipped | failed | dead | already
    detail: str = ""


def merge(events: list[Event]) -> Event:
    """Склеивает события по одной отгрузке: поля объединяются, «неизвестно» побеждает."""
    first = events[0]
    actions = {e.action for e in events}
    fields: list[str] | None = []
    for e in events:
        if e.fields is None:
            fields = None
            break
        fields.extend(f for f in e.fields if f not in fields)
    action = "CREATE" if "CREATE" in actions else first.action
    return Event(entity_id=first.entity_id, action=action, fields=fields,
                 source=first.source, entity=first.entity)


class Engine:
    def __init__(self, ctx: Context, automations: list[Automation]):
        self.ctx = ctx
        self.automations = automations
        self.by_name = {a.name: a for a in automations}

    async def load(self, shipment_id: str) -> Shipment | None:
        try:
            return Shipment.from_api(await self.ctx.ms.demand(shipment_id))
        except MoySkladError as e:
            if e.status == 404:
                return None
            raise

    async def process_pending(self, limit: int = 100) -> list[Outcome]:
        """Разобрать очередь. Ошибка МойСклад — события остаются в очереди до следующего круга."""
        queued = self.ctx.store.pending(limit)
        groups: dict[str, list] = {}
        for q in queued:
            groups.setdefault(q.event.entity_id, []).append(q)
        outcomes = []
        for entity_id, items in groups.items():
            ids = [q.id for q in items]
            event = merge([q.event for q in items])
            if event.action == "DELETE" and all(q.event.action == "DELETE" for q in items):
                self.ctx.store.mark_processed(ids)
                continue
            shipment = await self.load(entity_id)
            if shipment is not None:
                outcomes += await self.dispatch(shipment, event)
            self.ctx.store.mark_processed(ids)
        return outcomes

    async def dispatch(self, shipment: Shipment, event: Event) -> list[Outcome]:
        out = []
        for a in self.automations:
            if a.entity != event.entity:
                continue
            try:
                wanted = a.wants(shipment, event)
            except Exception as e:                       # кривой фильтр не валит остальных
                log.exception("%s.wants упал", a.name)
                out.append(Outcome(a.name, shipment.number, "failed", f"wants: {e}"))
                continue
            if wanted:
                out.append(await self.execute(a, shipment))
        return out

    async def execute(self, a: Automation, shipment: Shipment, mode: str = "normal") -> Outcome:
        """mode: normal — по событию; retry — плановый повтор; force — вручную, всё заново."""
        store = self.ctx.store
        key = self.ctx.run_key(a, shipment)
        run = store.run(a.name, key)
        if mode != "force":
            if run and run.status in ("done", "dead", "skipped", "running"):
                return Outcome(a.name, shipment.number, "already", run.status)
            if (mode == "normal" and run and run.status == "failed" and run.next_at
                    and run.next_at > iso(now_utc())):
                return Outcome(a.name, shipment.number, "already", f"повтор после {run.next_at}")
            if a.already_done(shipment):
                store.start(a.name, key, shipment.id, shipment.number)
                store.finish(a.name, key, "done", "уже отмечено в МойСклад")
                return Outcome(a.name, shipment.number, "already", "отметка в МойСклад")

        store.start(a.name, key, shipment.id, shipment.number)
        try:
            result = await a.run(shipment, self.ctx) or "ok"
        except Skip as e:
            store.finish(a.name, key, "skipped", str(e))
            log.info("%s %s: пропуск — %s", a.name, shipment.number, e)
            return Outcome(a.name, shipment.number, "skipped", str(e))
        except Exception as e:
            log.exception("%s %s: ошибка", a.name, shipment.number)
            run = store.fail(a.name, key, f"{type(e).__name__}: {e}")
            if run.status == "dead":
                await self.ctx.channels.alert(
                    f"⚠️ {a.title or a.name}: отгрузка {shipment.number} — {e}."
                    f" Попыток: {run.attempts}, больше не повторяю.")
            return Outcome(a.name, shipment.number, run.status, str(e))
        store.finish(a.name, key, "done", result)
        log.info("%s %s: %s", a.name, shipment.number, result)
        return Outcome(a.name, shipment.number, "done", result)

    async def retry_due(self) -> list[Outcome]:
        out = []
        for run in self.ctx.store.due():
            a = self.by_name.get(run.automation)
            if not a:
                continue
            shipment = await self.load(run.entity_id)
            if shipment is None:
                self.ctx.store.finish(run.automation, run.key, "skipped", "отгрузка удалена")
                continue
            out.append(await self.execute(a, shipment, mode="retry"))
        return out
