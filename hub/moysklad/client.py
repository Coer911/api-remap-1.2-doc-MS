"""Транспорт JSON API МойСклад 1.2: авторизация, сжатие, лимиты, повторы, пагинация.

Правила API (документация: github.com/Coer911/api-remap-1.2-doc-MS, source/includes):
  * Authorization: Bearer <token>; Accept-Encoding: gzip обязателен, иначе 415 (_general.md);
  * лимит за 3 с считается на пользователя (с 16.02.2026), а запрос с токеном
    пользователя «весит» больше единицы (_restrictions.md): с 12.05.2026 — 22 запроса
    за 3 с, с 01.09.2026 — 15, с 01.12.2026 — 11. Бюджет по умолчанию считается по этому
    графику на каждую дату; явный rps (MOYSKLAD_RPS) — для токена решения (45) или когда
    процессов с одним токеном несколько (делите лимит между ними). 5 параллельных
    запросов на пользователя; при 429 ждём X-Lognex-Retry-After мс;
  * выборка до 1000 строк, с expand — до 100;
  * деньги в копейках, даты — «ГГГГ-ММ-ДД ЧЧ:мм:сс.ммм» по Москве.

Справочные вещи (статусы, доп. поля, шаблоны печати) кешируются на META_TTL секунд:
они меняются редко, а запрашиваются на каждое событие.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from datetime import date
from typing import Any
from urllib.parse import urlparse

import httpx

BASE = "https://api.moysklad.ru/api/remap/1.2"
USER_AGENT = "moysklad-hub/1.0 (+https://github.com/Coer911/moysklad-hub)"
WINDOW_SECONDS = 3.0
PARALLEL = 4
META_TTL = 300.0
# лимит запросов за 3 с для токена пользователя по датам (_restrictions.md)
USER_TOKEN_LIMITS = (("2026-12-01", 11), ("2026-09-01", 15), ("2026-05-12", 22), ("0000-00-00", 45))


def user_token_budget(today: date | None = None) -> int:
    """Сколько запросов за 3 с позволить себе сегодня — на единицу меньше лимита, с запасом."""
    day = (today or date.today()).isoformat()
    limit = next(n for start, n in USER_TOKEN_LIMITS if day >= start)
    return max(1, limit - 1)


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


class NotFound(MoySkladError):
    pass


def entity_id(href: str) -> str:
    """https://…/entity/demand/<id>?x=1 -> <id>"""
    return urlparse(href).path.rstrip("/").rsplit("/", 1)[-1]


def meta_of(obj: dict) -> dict:
    """{"meta": …} для ссылки на объект в теле запроса."""
    return {"meta": obj["meta"]}


class MoySklad:
    def __init__(self, token: str, base: str = BASE, *, rps: int | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, retries: int = 5):
        if not token:
            raise ValueError("не задан токен МойСклад (MOYSKLAD_TOKEN)")
        self.base = base.rstrip("/")
        self._host = urlparse(self.base).netloc
        self._fixed_budget = max(1, min(rps, 45)) if rps else None
        self._retries = retries
        self._sem = asyncio.Semaphore(PARALLEL)
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._transport = transport
        self._cache: dict[str, tuple[float, Any]] = {}
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

    @property
    def budget(self) -> int:
        return self._fixed_budget or user_token_budget()

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] > WINDOW_SECONDS:
                self._stamps.popleft()
            if len(self._stamps) >= self.budget:
                await asyncio.sleep(WINDOW_SECONDS - (now - self._stamps[0]))
                self._stamps.popleft()
            self._stamps.append(time.monotonic())

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      json: Any = None) -> httpx.Response:
        """Запрос с лимитами и повторами. 3xx не разворачиваются — их ждёт печать."""
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
            if r.status_code == 404:
                raise NotFound.from_response(r)
            if r.status_code >= 400:
                raise MoySkladError.from_response(r)
            return r
        raise MoySkladError(429, "лимит запросов не отпустил после повторов", path)

    async def get(self, path: str, **params) -> dict:
        r = await self.request("GET", path, params=params or None)
        return r.json()

    async def put(self, path: str, body: dict) -> dict:
        return (await self.request("PUT", path, json=body)).json()

    async def post(self, path: str, body: Any) -> dict:
        return (await self.request("POST", path, json=body)).json()

    async def iterate(self, path: str, *, limit: int = 100, **params) -> AsyncIterator[dict]:
        """Все строки выборки страницами по limit (с expand — не больше 100)."""
        if "expand" in params:
            limit = min(limit, 100)
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

    async def cached(self, key: str, loader) -> Any:
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        value = await loader()
        self._cache[key] = (time.monotonic() + META_TTL, value)
        return value

    def forget(self, prefix: str = "") -> None:
        for key in [k for k in self._cache if k.startswith(prefix)]:
            del self._cache[key]

    # ── сущности ─────────────────────────────────────────────────────────────

    async def me(self) -> dict:
        return await self.get("context/employee")

    async def entity(self, type_: str, id_: str, expand: str = "") -> dict:
        return await self.get(f"entity/{type_}/{id_}", **({"expand": expand} if expand else {}))

    async def find(self, type_: str, filter: str, expand: str = "") -> list[dict]:
        params = {"filter": filter, "limit": 2}
        if expand:
            params["expand"] = expand
        return (await self.get(f"entity/{type_}", **params)).get("rows", [])

    async def list(self, type_: str, *, filter: str = "", order: str = "", expand: str = "",
                   limit: int = 100) -> AsyncIterator[dict]:
        params = {}
        for k, v in (("filter", filter), ("order", order), ("expand", expand)):
            if v:
                params[k] = v
        async for row in self.iterate(f"entity/{type_}", limit=limit, **params):
            yield row

    async def update(self, type_: str, id_: str, body: dict, expand: str = "") -> dict:
        path = f"entity/{type_}/{id_}" + (f"?expand={expand}" if expand else "")
        return await self.put(path, body)

    async def positions(self, type_: str, id_: str) -> list[dict]:
        return [row async for row in self.iterate(f"entity/{type_}/{id_}/positions",
                                                  expand="assortment")]

    async def metadata(self, type_: str) -> dict:
        """Метаданные типа: статусы (states) и доп. поля (attributes)."""
        return await self.cached(f"meta:{type_}", lambda: self.get(f"entity/{type_}/metadata"))

    async def customentity_elements(self, customentity_id: str) -> list[dict]:
        return await self.cached(
            f"ce:{customentity_id}",
            lambda: self._all(f"entity/customentity/{customentity_id}"))

    async def _all(self, path: str) -> list[dict]:
        return [row async for row in self.iterate(path, limit=1000)]

    # ── печатные формы ───────────────────────────────────────────────────────

    async def templates(self, type_: str) -> list[dict]:
        """Стандартные и пользовательские шаблоны печати сущности."""
        async def load():
            out = []
            for kind in ("embeddedtemplate", "customtemplate"):
                out += (await self.get(f"entity/{type_}/metadata/{kind}")).get("rows", [])
            return out
        return await self.cached(f"tpl:{type_}", load)

    async def find_template(self, type_: str, name: str) -> dict:
        """Шаблон по точному имени, иначе — по началу имени. Нет — ошибка со списком."""
        rows = await self.templates(type_)
        want = name.strip().lower()
        for row in rows:
            if row.get("name", "").strip().lower() == want:
                return row
        for row in rows:
            if row.get("name", "").lower().startswith(want):
                return row
        names = ", ".join(sorted(r.get("name", "") for r in rows))
        raise NotFound(404, f"нет шаблона печати «{name}» у {type_}. Есть: {names}")

    async def export(self, type_: str, doc_id: str, template: dict, extension: str = "pdf",
                     wait: float = 60.0) -> bytes:
        """Печатная форма (documents/_print.md): 303 — файл готов, 202 — опрашиваем Location."""
        body = {"template": meta_of(template), "extension": extension}
        r = await self.request("POST", f"entity/{type_}/{doc_id}/export/", json=body)
        deadline = time.monotonic() + wait
        while r.status_code == 202:
            if time.monotonic() > deadline:
                raise MoySkladError(202, "печатная форма не готова за отведённое время")
            await asyncio.sleep(1.5)
            r = await self.request("GET", r.headers["Location"])
        location = r.headers.get("Location")
        return r.content if not location else await self._download(location)

    async def _download(self, url: str) -> bytes:
        if urlparse(url).netloc == self._host:
            r = await self.request("GET", url)
            if r.status_code in (301, 302, 303, 307) and r.headers.get("Location"):
                return await self._download(r.headers["Location"])
            return r.content
        # временное хранилище на другом хосте (на живом API — print-prod.moysklad.ru):
        # токен туда не отдаём, а в сетевых правилах хостинга этот хост должен быть открыт
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True,
                                     transport=self._transport) as plain:
            r = await plain.get(url)
            r.raise_for_status()
            return r.content

    async def publish(self, type_: str, doc_id: str, template: dict) -> str:
        """Публичная ссылка на печатную форму (_publication.md); существующую переиспользуем."""
        page = await self.get(f"entity/{type_}/{doc_id}/publication")
        href = template["meta"]["href"]
        for row in page.get("rows", []):
            if row.get("template", {}).get("meta", {}).get("href") == href:
                return row["href"]
        return (await self.post(f"entity/{type_}/{doc_id}/publication",
                                {"template": meta_of(template)}))["href"]

    # ── вебхуки ──────────────────────────────────────────────────────────────

    async def webhooks(self) -> list[dict]:
        return (await self.get("entity/webhook")).get("rows", [])

    async def create_webhook(self, url: str, type_: str, action: str,
                             diff_type: str | None = None) -> dict:
        body = {"url": url, "entityType": type_, "action": action}
        if diff_type and action == "UPDATE":
            body["diffType"] = diff_type
        return await self.post("entity/webhook", body)

    async def delete_webhook(self, webhook_id: str) -> None:
        await self.request("DELETE", f"entity/webhook/{webhook_id}")
