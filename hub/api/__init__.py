"""HTTP-сторона хаба: вебхуки МойСклад и REST API для людей, ботов и других автоматизаций.

  GET  /healthz                                    — жив ли, очередь, dry-run
  POST /hooks/moysklad/{secret}                    — вебхук МойСклад

  REST (заголовок X-Api-Key или ?key=…), {kind}: orders | shipments | counterparties
  GET   /api/{kind}?days=7&state=…&search=…        — список за период
  GET   /api/{kind}/states                         — статусы этого типа
  GET   /api/{kind}/fields                         — доп. поля этого типа (имя, тип)
  GET   /api/{kind}/{ref}                          — одна запись + журнал по ней
  PUT   /api/{kind}/{ref}/state     {"state": "…"} — сменить статус
  PATCH /api/{kind}/{ref}/fields    {"Поле": …}    — записать доп. поля по именам
  GET   /api/{kind}/{ref}/print?template=…&format=pdf — печатная форма (заказы, отгрузки)
  GET   /api/{kind}/{ref}/link?template=…          — публичная ссылка mskld.ru
  POST  /api/{kind}/{ref}/run/{automation}?force=1 — запустить автоматизацию
  GET   /api/automations | /api/journal | /api/stats

ref — id, номер документа (как в МойСклад), для контрагента — ещё ИНН или точное имя.
"""
from __future__ import annotations

import hmac
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response

from ..automations.send_document import MIME
from ..entities import PRINTABLE, Entity, from_api, resolve_type
from ..events import Event
from ..ingest import accept_webhook
from ..moysklad import FieldError, MoySkladError, NotFound
from ..moysklad import fields as ms_fields
from ..runtime import Hub
from ..settings import Settings

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
INN = re.compile(r"^\d{10}(\d{2})?$")
MSK = ZoneInfo("Europe/Moscow")
DEFAULT_TEMPLATE = {"demand": "Универсальный передаточный документ",
                    "customerorder": "Счет покупателю"}


def create_app(settings: Settings | None = None, *, background: bool = True,
               factory: Callable[[], Awaitable[Hub]] | None = None) -> FastAPI:
    """factory — как собрать хаб (тесты подставляют поддельный МойСклад)."""
    settings = settings or Settings.from_env()
    state: dict[str, Hub | None] = {"hub": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub = await (factory() if factory else Hub.create(settings))
        state["hub"] = app.state.hub = hub
        if background:
            await hub.start()
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(title="Хаб автоматизаций МойСклад", lifespan=lifespan)

    def get_hub() -> Hub:
        return state["hub"]

    def auth(request: Request) -> None:
        if not settings.api_key:
            raise HTTPException(503, "API выключен: задайте HUB_API_KEY")
        given = request.headers.get("X-Api-Key") or request.query_params.get("key") or ""
        if not hmac.compare_digest(given.encode(), settings.api_key.encode()):
            raise HTTPException(401, "неверный X-Api-Key")

    def kind_type(kind: str) -> type[Entity]:
        try:
            return resolve_type(kind)
        except KeyError as e:
            raise HTTPException(404, str(e)) from None

    async def resolve(kind: str, ref: str, h: Hub) -> Entity:
        cls = kind_type(kind)
        try:
            if UUID.match(ref):
                return from_api(cls.type, await h.ms.entity(cls.type, ref, cls.expand))
            flt = f"inn={ref}" if cls.type == "counterparty" and INN.match(ref) else f"name={ref}"
            rows = await h.ms.find(cls.type, flt, cls.expand)
        except NotFound:
            rows = []
        except MoySkladError as e:
            raise HTTPException(502, str(e)) from e
        if not rows:
            raise HTTPException(404, f"{cls.title} «{ref}» не найден")
        if len(rows) > 1:
            raise HTTPException(409, f"«{ref}» — не одна запись, укажите id")
        return from_api(cls.type, rows[0])

    def ms_errors(e: Exception) -> HTTPException:
        if isinstance(e, FieldError):
            return HTTPException(422, str(e))
        return HTTPException(502, str(e))

    # ── служебное ────────────────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz(h: Hub = Depends(get_hub)):
        return {"ok": True, "role": settings.role, "dry_run": settings.dry_run,
                "pending": await h.storage.pending_count(),
                "automations": [a.name for a in h.automations]}

    @app.post("/hooks/moysklad/{secret}", status_code=204)
    async def moysklad_hook(secret: str, request: Request, h: Hub = Depends(get_hub)):
        if not settings.webhook_secret or not hmac.compare_digest(
                secret.encode(), settings.webhook_secret.encode()):
            raise HTTPException(404)
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "ожидался JSON") from None
        if await accept_webhook(h.storage, payload, request.query_params.get("requestId", "")):
            h.wake()
        return Response(status_code=204)

    @app.get("/api/automations", dependencies=[Depends(auth)])
    async def automations(h: Hub = Depends(get_hub)):
        return [a.describe() for a in h.automations]

    @app.get("/api/journal", dependencies=[Depends(auth)])
    async def journal(limit: int = Query(50, ge=1, le=500), status: str = "",
                      automation: str = "", h: Hub = Depends(get_hub)):
        return [asdict(r) for r in await h.storage.journal(limit, automation=automation,
                                                            status=status)]

    @app.get("/api/stats", dependencies=[Depends(auth)])
    async def stats(h: Hub = Depends(get_hub)):
        return await h.storage.stats()

    # ── сущности ─────────────────────────────────────────────────────────────

    @app.get("/api/{kind}", dependencies=[Depends(auth)])
    async def listing(kind: str, days: int = Query(7, ge=1, le=366), state: str = "",
                      search: str = "", limit: int = Query(100, ge=1, le=1000),
                      h: Hub = Depends(get_hub)):
        cls = kind_type(kind)
        since = (datetime.now(MSK) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        flt = f"{'moment' if cls.type in PRINTABLE else 'updated'}>={since}"
        if search:
            flt += f";name~{search}"
        out = []
        try:
            async for row in h.ms.list(cls.type, filter=flt, expand=cls.expand,
                                       order="updated,desc"):
                obj = from_api(cls.type, row)
                if state and obj.state.lower() != state.lower():
                    continue
                out.append(obj.to_dict())
                if len(out) >= limit:
                    break
        except MoySkladError as e:
            raise HTTPException(502, str(e)) from e
        return out

    @app.get("/api/{kind}/states", dependencies=[Depends(auth)])
    async def states(kind: str, h: Hub = Depends(get_hub)):
        cls = kind_type(kind)
        rows = await ms_fields.states(h.ms, cls.type)
        return [{"name": r.get("name"), "type": r.get("stateType"), "color": r.get("color")}
                for r in rows]

    @app.get("/api/{kind}/fields", dependencies=[Depends(auth)])
    async def field_defs(kind: str, h: Hub = Depends(get_hub)):
        cls = kind_type(kind)
        rows = await ms_fields.definitions(h.ms, cls.type)
        return [{"name": r.get("name"), "type": r.get("type"), "required": r.get("required")}
                for r in rows]

    @app.get("/api/{kind}/{ref}", dependencies=[Depends(auth)])
    async def one(kind: str, ref: str, h: Hub = Depends(get_hub)):
        obj = await resolve(kind, ref, h)
        return {**obj.to_dict(),
                "runs": [asdict(r) for r in await h.storage.journal(entity_id=obj.id)]}

    @app.put("/api/{kind}/{ref}/state", dependencies=[Depends(auth)])
    async def change_state(kind: str, ref: str, body: dict[str, str] = Body(...),
                           h: Hub = Depends(get_hub)):
        obj = await resolve(kind, ref, h)
        name = (body.get("state") or "").strip()
        if not name:
            raise HTTPException(422, "нужно {\"state\": \"Имя статуса\"}")
        try:
            raw = await ms_fields.set_state(h.ms, obj.type, obj.id, name)
        except (FieldError, MoySkladError) as e:
            raise ms_errors(e) from e
        return {"id": obj.id, "name": obj.name, "state": (raw.get("state") or {}).get("name", name)}

    @app.patch("/api/{kind}/{ref}/fields", dependencies=[Depends(auth)])
    async def change_fields(kind: str, ref: str, body: dict[str, Any] = Body(...),
                            h: Hub = Depends(get_hub)):
        obj = await resolve(kind, ref, h)
        try:
            raw = await ms_fields.set_fields(h.ms, obj.type, obj.id, body)
        except (FieldError, MoySkladError) as e:
            raise ms_errors(e) from e
        return {"id": obj.id, "name": obj.name,
                "fields": {k: v for k, v in from_api(obj.type, raw).attributes.items() if k in body}}

    async def template_for(h: Hub, obj: Entity, template: str) -> dict:
        if obj.type not in PRINTABLE:
            raise HTTPException(400, f"{obj.title} не печатается")
        try:
            return await h.ms.find_template(obj.type, template or DEFAULT_TEMPLATE[obj.type])
        except MoySkladError as e:
            raise HTTPException(404 if e.status == 404 else 502, str(e)) from e

    @app.get("/api/{kind}/{ref}/print", dependencies=[Depends(auth)])
    async def print_form(kind: str, ref: str, template: str = "", format: str = "pdf",
                         h: Hub = Depends(get_hub)):
        if format not in MIME:
            raise HTTPException(400, f"format: {', '.join(MIME)}")
        obj = await resolve(kind, ref, h)
        tpl = await template_for(h, obj, template)
        try:
            data = await h.ms.export(obj.type, obj.id, tpl, format)
        except MoySkladError as e:
            raise HTTPException(502, str(e)) from e
        name = f"{tpl.get('name', 'документ')} {obj.name}.{format}"
        return Response(data, media_type=MIME[format], headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/api/{kind}/{ref}/link", dependencies=[Depends(auth)])
    async def link(kind: str, ref: str, template: str = "", h: Hub = Depends(get_hub)):
        obj = await resolve(kind, ref, h)
        tpl = await template_for(h, obj, template)
        try:
            return {"url": await h.ms.publish(obj.type, obj.id, tpl), "template": tpl.get("name")}
        except MoySkladError as e:
            raise HTTPException(502, str(e)) from e

    @app.post("/api/{kind}/{ref}/run/{name}", dependencies=[Depends(auth)])
    async def run(kind: str, ref: str, name: str, force: bool = False, h: Hub = Depends(get_hub)):
        a = h.engine.by_name.get(name)
        if not a or a.scheduled:
            raise HTTPException(404, f"событийная автоматизация {name} не включена")
        obj = await resolve(kind, ref, h)
        if a.entity != obj.type:
            raise HTTPException(400, f"{name} работает с {a.entity}, а это {obj.type}")
        event = Event(entity=obj.type, entity_id=obj.id, action="MANUAL", source="api")
        if not force and not a.wants(obj, event):
            return {"status": "not_wanted",
                    "detail": "условия автоматизации не выполнены — повторите с force=1"}
        return asdict(await h.engine.execute(a, obj, event, mode="force" if force else "normal"))

    return app
