"""Клиент JSON API МойСклад 1.2 — всё, что хабу нужно от учётной системы.

Правила API, которые здесь соблюдаются (источник — source/includes этого форка):
  * токен в заголовке Authorization: Bearer <token>;
  * обязательный Accept-Encoding: gzip, без него сервер отвечает 415
    (_general.md, «Сжатие содержимого ответов»);
  * не больше 45 запросов за 3 секунды и 5 параллельных от пользователя
    (_restrictions.md); при 429 ждём X-Lognex-Retry-After миллисекунд;
  * выборка до 1000 строк, а с expand — до 100 (_general.md, «expand»);
  * суммы и цены в копейках, даты — строки «ГГГГ-ММ-ДД ЧЧ:мм:сс.ммм» по Москве;
  * печать: POST …/export → 303 и Location на файл, живущий 5 минут
    (documents/_print.md); публичная ссылка — …/publication (_publication.md).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

BASE = "https://api.moysklad.ru/api/remap/1.2"
USER_AGENT = "shipments-hub/1.0 (+https://github.com/Coer911/api-remap-1.2-doc-MS)"

# лимит API — 45 за 3 с; держим запас, чтобы соседние интеграции тоже жили
WINDOW_REQUESTS = 40
WINDOW_SECONDS = 3.0
PARALLEL = 4


class MoySkladError(Exception):
    def __init__(self, status: int, message: str, url: str = ""):
        super().__init__(f"МойСклад {status}: {message}" + (f" ({url})" if url else ""))
        self.status = status
        self.message = message
        self.url = url

    @classmethod
    def from_response(cls, r: httpx.Response) -> MoySkladError:
        message = r.reason_phrase
        try:
            errors = r.json().get("errors") or []
            if errors:
                message = "; ".join(e.get("error", "") for e in errors)
        except Exception:
            pass
        return cls(r.status_code, message, str(r.request.url))


def entity_id(href: str) -> str:
    """https://…/entity/demand/<id>?x=1 -> <id>"""
    return urlparse(href).path.rstrip("/").rsplit("/", 1)[-1]


class MoySklad:
    def __init__(self, token: str, base: str = BASE, transport: httpx.AsyncBaseTransport | None = None,
                 retries: int = 5):
        if not token:
            raise ValueError("не задан токен МойСклад (MOYSKLAD_TOKEN)")
        self.base = base.rstrip("/")
        self._host = urlparse(self.base).netloc
        self._retries = retries
        self._sem = asyncio.Semaphore(PARALLEL)
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._transport = transport
        self._http = httpx.AsyncClient(
            base_url=self.base + "/",
            headers={"Authorization": f"Bearer {token}",
                     "Accept-Encoding": "gzip",
                     "Accept": "application/json;charset=utf-8",
                     "User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> MoySklad:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ── транспорт ────────────────────────────────────────────────────────────

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] > WINDOW_SECONDS:
                self._stamps.popleft()
            if len(self._stamps) >= WINDOW_REQUESTS:
                await asyncio.sleep(WINDOW_SECONDS - (now - self._stamps[0]))
                self._stamps.popleft()
            self._stamps.append(time.monotonic())

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      json: Any = None) -> httpx.Response:
        """Запрос с лимитами и повторами. 3xx не разворачиваются — их ждёт экспорт."""
        path = path if path.startswith("http") else path.lstrip("/")
        for attempt in range(self._retries):
            async with self._sem:
                await self._throttle()
                try:
                    r = await self._http.request(method, path, params=params, json=json)
                except httpx.TransportError:
                    if attempt == self._retries - 1:
                        raise
                    await asyncio.sleep(2 ** attempt)
                    continue
            if r.status_code == 429:
                wait_ms = int(r.headers.get("X-Lognex-Retry-After") or 1000)
                await asyncio.sleep(max(wait_ms, 100) / 1000)
                continue
            if r.status_code >= 500 and attempt < self._retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise MoySkladError.from_response(r)
            return r
        raise MoySkladError(429, "лимит запросов не отпустил после повторов", path)

    async def get(self, path: str, **params) -> dict:
        r = await self.request("GET", path, params=params or None)
        return r.json()

    async def iterate(self, path: str, *, limit: int = 100, **params) -> AsyncIterator[dict]:
        """Все строки выборки страницами по limit (с expand — не больше 100)."""
        offset = 0
        while True:
            page = await self.get(path, limit=limit, offset=offset, **params)
            rows = page.get("rows", [])
            for row in rows:
                yield row
            size = page.get("meta", {}).get("size", 0)
            offset += len(rows)
            if not rows or offset >= size:
                return

    # ── отгрузки ─────────────────────────────────────────────────────────────

    DEMAND_EXPAND = "agent,state,organization,contract,salesChannel,project"

    async def me(self) -> dict:
        return await self.get("context/employee")

    async def demand(self, demand_id: str) -> dict:
        return await self.get(f"entity/demand/{demand_id}", expand=self.DEMAND_EXPAND)

    async def demand_by_number(self, number: str) -> dict | None:
        page = await self.get("entity/demand", filter=f"name={number}", expand=self.DEMAND_EXPAND,
                              limit=1)
        rows = page.get("rows", [])
        return rows[0] if rows else None

    async def demands(self, *, filter: str = "", order: str = "moment,desc",
                      limit: int = 100) -> AsyncIterator[dict]:
        params = {"expand": self.DEMAND_EXPAND, "order": order}
        if filter:
            params["filter"] = filter
        async for row in self.iterate("entity/demand", limit=min(limit, 100), **params):
            yield row

    async def demand_positions(self, demand_id: str) -> list[dict]:
        return [row async for row in self.iterate(f"entity/demand/{demand_id}/positions",
                                                  expand="assortment")]

    async def states(self, entity: str = "demand") -> list[dict]:
        return (await self.get(f"entity/{entity}/metadata")).get("states", [])

    # ── печатные формы ───────────────────────────────────────────────────────

    async def templates(self, entity: str = "demand") -> list[dict]:
        """Стандартные и пользовательские шаблоны печати сущности."""
        out = []
        for kind in ("embeddedtemplate", "customtemplate"):
            page = await self.get(f"entity/{entity}/metadata/{kind}")
            out.extend(page.get("rows", []))
        return out

    async def find_template(self, name: str, entity: str = "demand") -> dict:
        """Шаблон по точному имени, иначе — по началу имени. Нет — ошибка со списком."""
        rows = await self.templates(entity)
        for row in rows:
            if row.get("name", "").strip().lower() == name.strip().lower():
                return row
        for row in rows:
            if row.get("name", "").lower().startswith(name.strip().lower()):
                return row
        names = ", ".join(sorted(r.get("name", "") for r in rows))
        raise MoySkladError(404, f"нет шаблона печати «{name}». Есть: {names}")

    async def export(self, entity: str, doc_id: str, template: dict,
                     extension: str = "pdf", wait: float = 60.0) -> bytes:
        """Печатная форма документа. Ответ 303 — файл готов, 202 — опрашиваем Location."""
        body = {"template": {"meta": template["meta"]}, "extension": extension}
        r = await self.request("POST", f"entity/{entity}/{doc_id}/export/", json=body)
        deadline = time.monotonic() + wait
        while r.status_code == 202:
            if time.monotonic() > deadline:
                raise MoySkladError(202, "печатная форма не готова за отведённое время")
            await asyncio.sleep(1.5)
            r = await self.request("GET", r.headers["Location"])
        location = r.headers.get("Location")
        if not location:
            # файл мог прийти сразу телом ответа
            return r.content
        return await self._download(location)

    async def _download(self, url: str) -> bytes:
        if urlparse(url).netloc == self._host:
            r = await self.request("GET", url)
            if r.status_code in (301, 302, 303, 307) and r.headers.get("Location"):
                return await self._download(r.headers["Location"])
            return r.content
        # временное хранилище на другом хосте — токен туда не отдаём
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True,
                                     transport=self._transport) as plain:
            r = await plain.get(url)
            r.raise_for_status()
            return r.content

    async def publish(self, entity: str, doc_id: str, template: dict) -> str:
        """Публичная ссылка на печатную форму; существующую по тому же шаблону переиспользуем."""
        page = await self.get(f"entity/{entity}/{doc_id}/publication")
        tpl_href = template["meta"]["href"]
        for row in page.get("rows", []):
            if row.get("template", {}).get("meta", {}).get("href") == tpl_href:
                return row["href"]
        r = await self.request("POST", f"entity/{entity}/{doc_id}/publication",
                               json={"template": {"meta": template["meta"]}})
        return r.json()["href"]

    # ── доп. поля ────────────────────────────────────────────────────────────

    async def attribute(self, name: str, entity: str = "demand") -> dict | None:
        page = await self.get(f"entity/{entity}/metadata/attributes")
        for row in page.get("rows", []):
            if row.get("name", "").strip().lower() == name.strip().lower():
                return row
        return None

    async def create_attribute(self, name: str, entity: str = "demand",
                               type_: str = "string") -> dict:
        r = await self.request("POST", f"entity/{entity}/metadata/attributes",
                               json={"name": name, "type": type_, "required": False})
        return r.json()

    async def set_attribute(self, entity: str, doc_id: str, attribute: dict, value: Any) -> None:
        await self.request("PUT", f"entity/{entity}/{doc_id}",
                           json={"attributes": [{"meta": attribute["meta"], "value": value}]})

    # ── вебхуки ──────────────────────────────────────────────────────────────

    async def webhooks(self) -> list[dict]:
        return (await self.get("entity/webhook")).get("rows", [])

    async def create_webhook(self, url: str, entity: str, action: str,
                             diff_type: str | None = None) -> dict:
        body = {"url": url, "entityType": entity, "action": action}
        if diff_type and action == "UPDATE":
            body["diffType"] = diff_type
        r = await self.request("POST", "entity/webhook", json=body)
        return r.json()

    async def delete_webhook(self, webhook_id: str) -> None:
        await self.request("DELETE", f"entity/webhook/{webhook_id}")
