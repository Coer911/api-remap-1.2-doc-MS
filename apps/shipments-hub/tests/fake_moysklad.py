"""Поддельный МойСклад для тестов: та же форма ответов, что в документации форка.

Подключается к клиенту через httpx.MockTransport — сеть не нужна. Заодно
проверяет, что клиент соблюдает правила API (Bearer-токен и gzip).
"""
from __future__ import annotations

import json
import re
import uuid
from urllib.parse import parse_qs, urlparse

import httpx

BASE = "https://api.moysklad.ru/api/remap/1.2"
UPD_TEMPLATE = {"meta": {"href": f"{BASE}/entity/demand/metadata/embeddedtemplate/upd-new",
                         "type": "embeddedtemplate", "mediaType": "application/json"},
                "id": "upd-new", "name": "Универсальный передаточный документ"}
TORG12 = {"meta": {"href": f"{BASE}/entity/demand/metadata/embeddedtemplate/torg12",
                   "type": "embeddedtemplate", "mediaType": "application/json"},
          "id": "torg12", "name": "ТОРГ-12"}


def meta(kind: str, id_: str) -> dict:
    return {"meta": {"href": f"{BASE}/entity/{kind}/{id_}", "type": kind,
                     "mediaType": "application/json"}}


def error(status: int, text: str) -> httpx.Response:
    return httpx.Response(status, json={"errors": [{"error": text, "code": status}]})


class FakeMoySklad:
    def __init__(self):
        self.demands: dict[str, dict] = {}
        self.attributes = [{"meta": {"href": f"{BASE}/entity/demand/metadata/attributes/mark",
                                     "type": "attributemetadata"},
                            "id": "mark", "name": "УПД отправлен", "type": "string"}]
        self.publications: dict[str, list] = {}
        self.webhooks: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.violations: list[str] = []
        self.exports = 0
        self.throttle_next = 0          # сколько следующих запросов ответить 429
        self._clock = 0

    # ── данные ───────────────────────────────────────────────────────────────

    def tick(self) -> str:
        self._clock += 1
        return f"2026-10-25 12:{self._clock // 60:02d}:{self._clock % 60:02d}.000"

    def add_demand(self, number: str, state: str = "Отгружен", email: str = "buyer@example.ru",
                   sum_kop: int = 1234560, agent: str = "ООО «Кофейня»",
                   organization: str = "ИП Культура", applicable: bool = True) -> dict:
        id_ = str(uuid.uuid4())
        d = {**meta("demand", id_), "id": id_, "name": number,
             "moment": "2026-10-25 10:00:00.000", "updated": self.tick(),
             "applicable": applicable, "sum": sum_kop,
             "state": {**meta("state", "st-" + state), "name": state},
             "agent": {**meta("counterparty", "agent-" + number), "id": "agent-" + number,
                       "name": agent, "email": email, "inn": "7800000000"},
             "organization": {**meta("organization", "org"), "name": organization},
             "attributes": []}
        self.demands[id_] = d
        return d

    def set_state(self, demand_id: str, state: str) -> None:
        d = self.demands[demand_id]
        d["state"] = {**meta("state", "st-" + state), "name": state}
        d["updated"] = self.tick()

    def attribute_value(self, demand_id: str, name: str):
        for a in self.demands[demand_id]["attributes"]:
            if a["name"] == name:
                return a["value"]
        return None

    def webhook_payload(self, demand_id: str, action: str = "UPDATE",
                        fields: list[str] | None = None) -> dict:
        event = {**meta("demand", demand_id), "action": action,
                 "accountId": "acc"}
        if fields is not None:
            event["updatedFields"] = fields
        return {"auditContext": {"moment": "2026-10-25 12:00:00", "uid": "admin@cultura"},
                "events": [event]}

    # ── транспорт ────────────────────────────────────────────────────────────

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        path = url.path.replace("/api/remap/1.2/", "", 1).rstrip("/")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append((request.method, path))

        if url.netloc == "files.moysklad.example":       # временное хранилище печатных форм
            if "authorization" in request.headers:
                self.violations.append("токен ушёл на чужой хост")
            return httpx.Response(200, content=b"%PDF-1.4 fake " + path.encode())

        if request.headers.get("authorization", "") != "Bearer test-token":
            return error(401, "Ошибка аутентификации")
        if request.headers.get("accept-encoding") != "gzip":
            self.violations.append(f"Accept-Encoding={request.headers.get('accept-encoding')}")
            return httpx.Response(415)
        if self.throttle_next > 0:
            self.throttle_next -= 1
            return httpx.Response(429, headers={"X-Lognex-Retry-After": "10"},
                                  json={"errors": [{"error": "Превышен лимит"}]})

        body = json.loads(request.content) if request.content else None
        return self.route(request.method, path, q, body)

    def route(self, method: str, path: str, q: dict, body) -> httpx.Response:
        if path == "context/employee":
            return httpx.Response(200, json={"name": "Робот хаба", "uid": "robot@cultura"})
        if path == "entity/demand/metadata":
            names = sorted({d["state"]["name"] for d in self.demands.values()} | {"Новый", "Отгружен"})
            return httpx.Response(200, json={"states": [{"name": n} for n in names]})
        if path == "entity/demand/metadata/embeddedtemplate":
            return httpx.Response(200, json={"rows": [TORG12, UPD_TEMPLATE]})
        if path == "entity/demand/metadata/customtemplate":
            return httpx.Response(200, json={"rows": []})
        if path == "entity/demand/metadata/attributes":
            if method == "POST":
                attr = {"meta": {"href": f"{BASE}/entity/demand/metadata/attributes/{body['name']}",
                                 "type": "attributemetadata"},
                        "id": body["name"], "name": body["name"], "type": body["type"]}
                self.attributes.append(attr)
                return httpx.Response(200, json=attr)
            return httpx.Response(200, json={"rows": self.attributes})
        if path == "entity/demand":
            return self.list_demands(q)
        if path == "entity/webhook":
            if method == "POST":
                hook = {"id": str(uuid.uuid4()), "enabled": True, **body}
                self.webhooks.append(hook)
                return httpx.Response(200, json=hook)
            return httpx.Response(200, json={"rows": self.webhooks})
        if m := re.fullmatch(r"entity/webhook/([\w-]+)", path):
            self.webhooks = [h for h in self.webhooks if h["id"] != m.group(1)]
            return httpx.Response(200)
        if path.startswith("download/"):
            return httpx.Response(302, headers={
                "Location": "https://files.moysklad.example/" + path.split("/", 1)[1]})
        if m := re.fullmatch(r"entity/demand/([\w-]+)(?:/(export|publication))?", path):
            return self.demand(method, m.group(1), m.group(2), body)
        return error(404, f"нет такого ресурса {path}")

    def list_demands(self, q: dict) -> httpx.Response:
        rows = list(self.demands.values())
        for cond in filter(None, q.get("filter", "").split(";")):
            if m := re.fullmatch(r"name=(.+)", cond):
                rows = [d for d in rows if d["name"] == m.group(1)]
            elif m := re.fullmatch(r"updated>=(.+)", cond):
                rows = [d for d in rows if d["updated"][:19] >= m.group(1)[:19]]
            elif m := re.fullmatch(r"moment>=(.+)", cond):
                rows = [d for d in rows if d["moment"][:19] >= m.group(1)[:19]]
        if q.get("order", "").startswith("updated"):
            rows.sort(key=lambda d: d["updated"])
        limit, offset = int(q.get("limit", 1000)), int(q.get("offset", 0))
        if "expand" in q and limit > 100:
            self.violations.append("expand при limit > 100")
        return httpx.Response(200, json={"meta": {"size": len(rows), "limit": limit,
                                                  "offset": offset},
                                         "rows": rows[offset:offset + limit]})

    def demand(self, method: str, id_: str, sub: str | None, body) -> httpx.Response:
        d = self.demands.get(id_)
        if d is None:
            return error(404, f"Объект с UUID {id_} не найден")
        if sub == "export":
            self.exports += 1
            assert body["template"]["meta"]["type"] in ("embeddedtemplate", "customtemplate")
            return httpx.Response(303, headers={
                "Location": f"{BASE}/download/{d['name']}-{body['extension']}"})
        if sub == "publication":
            pubs = self.publications.setdefault(id_, [])
            if method == "POST":
                pub = {"template": body["template"], "href": f"https://mskld.ru/{d['name']}"}
                pubs.append(pub)
                return httpx.Response(200, json=pub)
            return httpx.Response(200, json={"rows": pubs})
        if method == "PUT":
            for a in body.get("attributes", []):
                name = next(x["name"] for x in self.attributes if x["meta"]["href"] == a["meta"]["href"])
                d["attributes"] = [x for x in d["attributes"] if x["name"] != name]
                d["attributes"].append({"meta": a["meta"], "name": name, "value": a["value"]})
            d["updated"] = self.tick()
        return httpx.Response(200, json=d)
