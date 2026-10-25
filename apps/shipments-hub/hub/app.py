"""HTTP-сторона хаба: приём вебхуков МойСклад и REST API для других автоматизаций.

  GET  /healthz                              — жив ли, сколько в очереди, dry-run ли
  POST /hooks/moysklad/{secret}              — вебхук МойСклад (секрет в пути)

  REST, заголовок X-Api-Key (или ?key=… для ссылок из браузера):
  GET  /api/automations                      — что включено и с какими параметрами
  GET  /api/shipments?days=7&state=…         — отгрузки за период
  GET  /api/shipments/{id|номер}             — одна отгрузка + её журнал
  GET  /api/shipments/{id|номер}/print?template=…&format=pdf — печатная форма файлом
  GET  /api/shipments/{id|номер}/link?template=…             — публичная ссылка МойСклад
  POST /api/shipments/{id|номер}/run/{automation}?force=1    — запустить автоматизацию
  GET  /api/journal?status=failed&automation=…               — журнал запусков
"""
from __future__ import annotations

import hmac
import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response

from .automations.send_upd import MIME
from .ingest import accept_webhook
from .models import Event, Shipment
from .moysklad import MoySkladError
from .runtime import Hub
from .settings import Settings

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MSK = ZoneInfo("Europe/Moscow")
UPD = "Универсальный передаточный документ"


def create_app(hub: Hub | None = None, settings: Settings | None = None,
               background: bool = True) -> FastAPI:
    settings = settings or (hub.settings if hub else Settings.from_env())
    state = {"hub": hub}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["hub"] is None:
            state["hub"] = Hub(settings)
        if background:
            await state["hub"].start()
        yield
        if background:
            await state["hub"].stop()

    app = FastAPI(title="Хаб отгрузок МойСклад", lifespan=lifespan)

    def get_hub() -> Hub:
        return state["hub"]

    def auth(request: Request) -> None:
        if not settings.api_key:
            raise HTTPException(503, "API выключен: задайте HUB_API_KEY")
        given = request.headers.get("X-Api-Key") or request.query_params.get("key") or ""
        if not hmac.compare_digest(given.encode(), settings.api_key.encode()):
            raise HTTPException(401, "неверный X-Api-Key")

    async def shipment(ref: str, h: Hub) -> Shipment:
        try:
            raw = await h.ms.demand(ref) if UUID.match(ref) else await h.ms.demand_by_number(ref)
        except MoySkladError as e:
            raise HTTPException(404 if e.status == 404 else 502, str(e)) from e
        if not raw:
            raise HTTPException(404, f"отгрузка {ref} не найдена")
        return Shipment.from_api(raw)

    @app.get("/healthz")
    async def healthz(h: Hub = Depends(get_hub)):
        return {"ok": True, "dry_run": settings.dry_run, "pending": h.store.pending_count(),
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
        if accept_webhook(h.store, payload, request.query_params.get("requestId", "")):
            h.wake()
        return Response(status_code=204)

    @app.get("/api/automations", dependencies=[Depends(auth)])
    async def automations(h: Hub = Depends(get_hub)):
        return [a.describe() for a in h.automations]

    @app.get("/api/shipments", dependencies=[Depends(auth)])
    async def shipments(days: int = Query(7, ge=1, le=366), state: str = "", search: str = "",
                        limit: int = Query(100, ge=1, le=1000), h: Hub = Depends(get_hub)):
        since = (datetime.now(MSK) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        flt = f"moment>={since}"
        if search:
            flt += f";name~{search}"
        out = []
        async for row in h.ms.demands(filter=flt):
            sh = Shipment.from_api(row)
            if state and sh.state.lower() != state.lower():
                continue
            out.append(sh.to_dict())
            if len(out) >= limit:
                break
        return out

    @app.get("/api/shipments/{ref}", dependencies=[Depends(auth)])
    async def one(ref: str, h: Hub = Depends(get_hub)):
        sh = await shipment(ref, h)
        return {**sh.to_dict(), "runs": [asdict(r) for r in h.store.journal(entity_id=sh.id)]}

    @app.get("/api/shipments/{ref}/print", dependencies=[Depends(auth)])
    async def print_form(ref: str, template: str = UPD, format: str = "pdf",
                         h: Hub = Depends(get_hub)):
        if format not in MIME:
            raise HTTPException(400, f"format: {', '.join(MIME)}")
        sh = await shipment(ref, h)
        try:
            tpl = await h.ms.find_template(template)
            data = await h.ms.export("demand", sh.id, tpl, format)
        except MoySkladError as e:
            raise HTTPException(502, str(e)) from e
        name = f"{tpl.get('name', template)} {sh.number} от {sh.date}.{format}"
        return Response(data, media_type=MIME[format], headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @app.get("/api/shipments/{ref}/link", dependencies=[Depends(auth)])
    async def link(ref: str, template: str = UPD, h: Hub = Depends(get_hub)):
        sh = await shipment(ref, h)
        try:
            tpl = await h.ms.find_template(template)
            return {"url": await h.ms.publish("demand", sh.id, tpl), "template": tpl.get("name")}
        except MoySkladError as e:
            raise HTTPException(502, str(e)) from e

    @app.post("/api/shipments/{ref}/run/{name}", dependencies=[Depends(auth)])
    async def run(ref: str, name: str, force: bool = False, h: Hub = Depends(get_hub)):
        a = h.engine.by_name.get(name)
        if not a:
            raise HTTPException(404, f"автоматизация {name} не включена")
        sh = await shipment(ref, h)
        if not force and not a.wants(sh, Event(sh.id, "MANUAL", source="api")):
            return {"status": "not_wanted", "detail": "условия автоматизации не выполнены"
                                                      " — повторите с force=1"}
        outcome = await h.engine.execute(a, sh, mode="force" if force else "normal")
        return asdict(outcome)

    @app.get("/api/journal", dependencies=[Depends(auth)])
    async def journal(limit: int = Query(50, ge=1, le=500), status: str = "",
                      automation: str = "", h: Hub = Depends(get_hub)):
        return [asdict(r) for r in h.store.journal(limit, automation=automation, status=status)]

    return app
