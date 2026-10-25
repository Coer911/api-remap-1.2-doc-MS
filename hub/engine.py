"""Движок: события → свежая сущность из МойСклад → автоматизации.

Гарантии (держатся при любом числе воркеров на PostgreSQL):
  * событие обрабатывает один воркер — аренда в storage.claim;
  * пара (автоматизация, ключ) успешно выполняется один раз — storage.try_start
    атомарный, плюс отметка-доп. поле в самом МойСклад, если задана;
  * несколько событий по одной сущности в пачке склеиваются в одно;
  * откуда пришёл статус, знает снимок (storage.advance): автоматизации видят
    event.previous_state даже при опросе, где МойСклад этого не сообщает;
  * сбой — повтор через 1 мин, 5 мин, 30 мин, 2 ч, 6 ч, потом dead и алерт.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .automations import Automation, Context, Skip
from .entities import TYPES, Entity, from_api
from .events import Event, merge
from .moysklad import NotFound

log = logging.getLogger("hub.engine")
MSK = ZoneInfo("Europe/Moscow")


@dataclass
class Outcome:
    automation: str
    target: str
    status: str            # done | skipped | failed | dead | already | busy
    detail: str = ""


class Engine:
    def __init__(self, ctx: Context, automations: list[Automation], worker: str = "worker"):
        self.ctx = ctx
        self.automations = automations
        self.by_name = {a.name: a for a in automations}
        self.worker = worker

    @property
    def lease(self) -> int:
        return self.ctx.settings.lease_seconds

    async def load(self, type_: str, id_: str) -> Entity | None:
        try:
            raw = await self.ctx.ms.entity(type_, id_, TYPES[type_].expand)
        except NotFound:
            return None
        return from_api(type_, raw)

    # ── очередь ──────────────────────────────────────────────────────────────

    async def process(self, limit: int | None = None) -> list[Outcome]:
        claimed = await self.ctx.storage.claim(limit or self.ctx.settings.worker_batch,
                                               self.worker, self.lease)
        groups: dict[tuple[str, str], list] = {}
        for c in claimed:
            groups.setdefault((c.event.entity, c.event.entity_id), []).append(c)
        outcomes = []
        for (type_, id_), items in groups.items():
            ids = [c.id for c in items]
            try:
                outcomes += await self._handle(type_, id_, merge([c.event for c in items]))
            except Exception as e:
                log.exception("событие %s %s не разобрано", type_, id_)
                await self.ctx.storage.release(ids, f"{type(e).__name__}: {e}")
                continue
            await self.ctx.storage.complete(ids)
        return outcomes

    async def _handle(self, type_: str, id_: str, event: Event) -> list[Outcome]:
        if type_ not in TYPES or event.action == "DELETE":
            return []
        obj = await self.load(type_, id_)
        if obj is None:
            return []
        snap = await self.ctx.storage.advance(type_, id_, obj.state, obj.updated)
        event.previous_state = snap.previous_state
        event.is_new = snap.is_new
        return await self.dispatch(obj, event)

    async def dispatch(self, obj: Entity, event: Event) -> list[Outcome]:
        out = []
        for a in self.automations:
            if a.scheduled or a.entity != obj.type:
                continue
            try:
                wanted = a.wants(obj, event)
            except Exception as e:                       # кривой фильтр не валит остальных
                log.exception("%s.wants упал", a.name)
                out.append(Outcome(a.name, obj.label, "failed", f"wants: {e}"))
                continue
            if wanted:
                out.append(await self.execute(a, obj, event))
        return out

    async def execute(self, a: Automation, obj: Entity, event: Event,
                      mode: str = "normal", key: str | None = None) -> Outcome:
        """mode: normal — по событию; retry — плановый повтор; force — вручную, всё заново."""
        storage = self.ctx.storage
        key = key or self.ctx.run_key(a, a.key(obj, event))
        if mode == "normal" and a.already_done(obj):
            started, _ = await storage.try_start(a.name, key, entity=obj.type, entity_id=obj.id,
                                                 label=obj.label, lease=self.lease)
            if started:
                await storage.finish(a.name, key, "done", "уже отмечено в МойСклад")
            return Outcome(a.name, obj.label, "already", "отметка в МойСклад")
        started, existing = await storage.try_start(
            a.name, key, entity=obj.type, entity_id=obj.id, label=obj.label, lease=self.lease,
            force=mode == "force")
        if not started:
            detail = existing.status if existing else "занято"
            if existing and existing.status == "failed":
                detail = f"повтор после {existing.next_at}"
            return Outcome(a.name, obj.label, "already", detail)
        return await self._run(a, key, obj.label, lambda: a.run(obj, self.ctx))

    async def _run(self, a: Automation, key: str, label: str, work) -> Outcome:
        storage = self.ctx.storage
        try:
            result = await work() or "ok"
        except Skip as e:
            await storage.finish(a.name, key, "skipped", str(e))
            log.info("%s %s: пропуск — %s", a.name, label, e)
            return Outcome(a.name, label, "skipped", str(e))
        except Exception as e:
            log.exception("%s %s: ошибка", a.name, label)
            run = await storage.fail(a.name, key, f"{type(e).__name__}: {e}")
            if run.status == "dead":
                await self.ctx.channels.alert(
                    f"⚠️ {a.title or a.name}: {label} — {e}. Попыток: {run.attempts},"
                    f" больше не повторяю.")
            return Outcome(a.name, label, run.status, str(e))
        await storage.finish(a.name, key, "done", result)
        log.info("%s %s: %s", a.name, label, result)
        return Outcome(a.name, label, "done", result)

    # ── повторы и расписание ─────────────────────────────────────────────────

    async def retry_due(self) -> list[Outcome]:
        out = []
        for run in await self.ctx.storage.due():
            a = self.by_name.get(run.automation)
            if not a:
                continue
            if a.scheduled:
                slot = run.key.removeprefix("dry:").removeprefix("slot:")
                started, _ = await self.ctx.storage.try_start(
                    a.name, run.key, entity="schedule", entity_id=slot, label=run.label or slot,
                    lease=self.lease)
                if started:
                    out.append(await self._run(a, run.key, run.label or slot,
                                               lambda a=a, slot=slot: a.run_scheduled(self.ctx, slot)))
                continue
            obj = await self.load(run.entity, run.entity_id)
            if obj is None:
                await self.ctx.storage.finish(run.automation, run.key, "skipped", "сущность удалена")
                continue
            event = Event(entity=obj.type, entity_id=obj.id, action="RETRY", source="retry")
            out.append(await self.execute(a, obj, event, mode="retry", key=run.key))
        return out

    async def scheduled(self, now: datetime | None = None) -> list[Outcome]:
        now = now or datetime.now(MSK)
        out = []
        for a in self.automations:
            if not a.scheduled:
                continue
            slot = a.slot(now)
            if not slot:
                continue
            key = self.ctx.run_key(a, f"slot:{slot}")
            label = f"{a.name} {slot}"
            started, _ = await self.ctx.storage.try_start(a.name, key, entity="schedule",
                                                          entity_id=slot, label=label,
                                                          lease=self.lease)
            if started:
                out.append(await self._run(a, key, label,
                                           lambda a=a, slot=slot: a.run_scheduled(self.ctx, slot)))
        return out
