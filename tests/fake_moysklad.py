"""Поддельный МойСклад для тестов: формы ответов — как в docs/moysklad-api.

Подключается к клиенту через httpx.MockTransport — сеть не нужна. Заодно
проверяет, что клиент соблюдает правила API (Bearer-токен, gzip, expand ≤ 100).
"""
from __future__ import annotations

import json
import re
import uuid
from urllib.parse import parse_qs, urlparse

import httpx

BASE = "https://api.moysklad.ru/api/remap/1.2"
TYPES = ("customerorder", "demand", "counterparty")
STATES = {
    "customerorder": ["Новый", "Подтверждён", "Собран", "Отгружен", "Отменён"],
    "demand": ["Новый", "Отгружен", "Доставлен"],
    "counterparty": ["Новый", "Постоянный", "Спящий"],
}
TEMPLATES = {
    "demand": ["ТОРГ-12", "Универсальный передаточный документ"],
    "customerorder": ["Заказ покупателя", "Счет покупателю"],
}


def meta(kind: str, id_: str, path: str = "") -> dict:
    return {"meta": {"href": f"{BASE}/entity/{path or kind}/{id_}", "type": kind,
                     "mediaType": "application/json"}}


def error(status: int, text: str) -> httpx.Response:
    return httpx.Response(status, json={"errors": [{"error": text, "code": status}]})


class FakeMoySklad:
    def __init__(self):
        self.db: dict[str, dict[str, dict]] = {t: {} for t in TYPES}
        self.states = {t: [{**meta("state", f"st-{t}-{i}", f"{t}/metadata/states"),
                            "id": f"st-{t}-{i}", "name": n, "stateType": "Regular", "color": 1}
                           for i, n in enumerate(names)] for t, names in STATES.items()}
        self.attributes: dict[str, list[dict]] = {t: [] for t in TYPES}
        self.customentities: dict[str, list[dict]] = {}
        self.publications: dict[str, list] = {}
        self.webhooks: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.violations: list[str] = []
        self.exports = 0
        self.throttle_next = 0
        self._clock = 0
        self.add_field("demand", "УПД отправлен", "string")

    # ── справочники ──────────────────────────────────────────────────────────

    def add_field(self, type_: str, name: str, kind: str, values: list[str] | None = None) -> dict:
        id_ = f"attr-{type_}-{len(self.attributes[type_])}"
        d = {**meta("attributemetadata", id_, f"{type_}/metadata/attributes"), "id": id_,
             "name": name, "type": kind, "required": False}
        if kind == "customentity":
            ce = f"ce-{len(self.customentities)}"
            d["customEntityMeta"] = {"href": f"{BASE}/entity/companysettings/metadata/customEntities/{ce}",
                                     "type": "customentitymetadata"}
            self.customentities[ce] = [{**meta("customentity", f"{ce}-{i}", f"customentity/{ce}"),
                                        "id": f"{ce}-{i}", "name": v}
                                       for i, v in enumerate(values or [])]
        self.attributes[type_].append(d)
        return d

    def state(self, type_: str, name: str) -> dict:
        st = next(s for s in self.states[type_] if s["name"] == name)
        return {"meta": st["meta"], "name": st["name"]}

    # ── данные ───────────────────────────────────────────────────────────────

    def tick(self) -> str:
        self._clock += 1
        return f"2026-10-25 12:{self._clock // 60:02d}:{self._clock % 60:02d}.000"

    def add_counterparty(self, name: str = "ООО «Кофейня»", email: str = "buyer@example.ru",
                         inn: str = "7800000000", state: str | None = None) -> dict:
        id_ = str(uuid.uuid4())
        c = {**meta("counterparty", id_), "id": id_, "name": name, "email": email, "inn": inn,
             "phone": "+7 900 000-00-00", "companyType": "legal", "updated": self.tick(),
             "attributes": [], "tags": []}
        if state:
            c["state"] = self.state("counterparty", state)
        self.db["counterparty"][id_] = c
        return c

    def _document(self, type_: str, number: str, state: str, agent: dict | None, sum_kop: int,
                  organization: str, applicable: bool) -> dict:
        agent = agent or self.add_counterparty()
        id_ = str(uuid.uuid4())
        d = {**meta(type_, id_), "id": id_, "name": number, "moment": "2026-10-25 10:00:00.000",
             "updated": self.tick(), "applicable": applicable, "sum": sum_kop,
             "state": self.state(type_, state), "agent": agent,
             "organization": {**meta("organization", "org"), "name": organization},
             "attributes": [], "shipmentAddress": "СПб, Невский 1"}
        self.db[type_][id_] = d
        return d

    def add_order(self, number: str, state: str = "Новый", agent: dict | None = None,
                  sum_kop: int = 1234560, organization: str = "ИП Культура") -> dict:
        return self._document("customerorder", number, state, agent, sum_kop, organization, True)

    def add_demand(self, number: str, state: str = "Отгружен", agent: dict | None = None,
                   order: dict | None = None, email: str | None = None, sum_kop: int = 1234560,
                   organization: str = "ИП Культура", applicable: bool = True) -> dict:
        if agent is None and email is not None:
            agent = self.add_counterparty(email=email)
        d = self._document("demand", number, state, agent, sum_kop, organization, applicable)
        if order:
            d["customerOrder"] = {"meta": order["meta"], "name": order["name"]}
            order.setdefault("demands", []).append({"meta": d["meta"]})
        return d

    def set_state(self, type_: str, id_: str, name: str) -> None:
        obj = self.db[type_][id_]
        obj["state"] = self.state(type_, name)
        obj["updated"] = self.tick()

    def field(self, type_: str, id_: str, name: str):
        for a in self.db[type_][id_]["attributes"]:
            if a["name"] == name:
                v = a["value"]
                return v.get("name") if isinstance(v, dict) else v
        return None

    def webhook_payload(self, type_: str, id_: str, action: str = "UPDATE",
                        fields: list[str] | None = None) -> dict:
        event = {**meta(type_, id_), "action": action, "accountId": "acc"}
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
        if url.netloc == "files.moysklad.example":
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
        if "expand" in q and int(q.get("limit", 100)) > 100:
            self.violations.append(f"expand при limit={q['limit']}")
        body = json.loads(request.content) if request.content else None
        return self.route(request.method, path, q, body)

    def route(self, method: str, path: str, q: dict, body) -> httpx.Response:
        if path == "context/employee":
            return httpx.Response(200, json={"name": "Робот хаба", "uid": "robot@cultura"})
        if m := re.fullmatch(r"entity/(\w+)/metadata", path):
            t = m.group(1)
            return httpx.Response(200, json={"states": self.states.get(t, []),
                                             "attributes": self.attributes.get(t, [])})
        if m := re.fullmatch(r"entity/(\w+)/metadata/(embeddedtemplate|customtemplate)", path):
            t, kind = m.groups()
            names = TEMPLATES.get(t, []) if kind == "embeddedtemplate" else []
            return httpx.Response(200, json={"rows": [
                {**meta(kind, f"tpl-{t}-{i}", f"{t}/metadata/{kind}"), "id": f"tpl-{t}-{i}", "name": n}
                for i, n in enumerate(names)]})
        if m := re.fullmatch(r"entity/(\w+)/metadata/attributes", path):
            if method == "POST":
                return httpx.Response(200, json=self.add_field(m.group(1), body["name"], body["type"]))
            return httpx.Response(200, json={"rows": self.attributes[m.group(1)]})
        if m := re.fullmatch(r"entity/customentity/([\w-]+)", path):
            rows = self.customentities.get(m.group(1), [])
            return httpx.Response(200, json={"meta": {"size": len(rows)}, "rows": rows})
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
        if m := re.fullmatch(r"entity/(\w+)", path):
            if m.group(1) in self.db:
                return self.listing(m.group(1), q)
        if m := re.fullmatch(r"entity/(\w+)/([\w-]+)(?:/(export|publication))?", path):
            t, id_, sub = m.groups()
            if t in self.db:
                return self.one(method, t, id_, sub, body)
        return error(404, f"нет такого ресурса {path}")

    def listing(self, t: str, q: dict) -> httpx.Response:
        rows = list(self.db[t].values())
        for cond in filter(None, q.get("filter", "").split(";")):
            if m := re.fullmatch(r"(\w+)(>=|<=|=|~)(.*)", cond):
                key, op, val = m.groups()
                def get(d, key=key):
                    return str(d.get(key, ""))
                if op == "=":
                    rows = [d for d in rows if get(d) == val]
                elif op == "~":
                    rows = [d for d in rows if val.lower() in get(d).lower()]
                elif op == ">=":
                    rows = [d for d in rows if get(d)[:19] >= val[:19]]
                else:
                    rows = [d for d in rows if get(d)[:19] <= val[:19]]
        order = q.get("order", "")
        if order.startswith("updated"):
            rows.sort(key=lambda d: d["updated"], reverse=order.endswith("desc"))
        limit, offset = int(q.get("limit", 1000)), int(q.get("offset", 0))
        return httpx.Response(200, json={"meta": {"size": len(rows), "limit": limit, "offset": offset},
                                         "rows": rows[offset:offset + limit]})

    def resolve_value(self, d: dict, value):
        if isinstance(value, dict) and d["type"] == "customentity":
            href = value["meta"]["href"]
            for rows in self.customentities.values():
                for row in rows:
                    if row["meta"]["href"] == href:
                        return {"meta": row["meta"], "name": row["name"]}
            raise KeyError(href)
        return value

    def one(self, method: str, t: str, id_: str, sub: str | None, body) -> httpx.Response:
        obj = self.db[t].get(id_)
        if obj is None:
            return error(404, f"Объект с UUID {id_} не найден")
        if sub == "export":
            self.exports += 1
            assert body["template"]["meta"]["type"] in ("embeddedtemplate", "customtemplate")
            return httpx.Response(303, headers={
                "Location": f"{BASE}/download/{obj['name']}-{body['extension']}"})
        if sub == "publication":
            pubs = self.publications.setdefault(id_, [])
            if method == "POST":
                pub = {"template": body["template"], "href": f"https://mskld.ru/{obj['name']}"}
                pubs.append(pub)
                return httpx.Response(200, json=pub)
            return httpx.Response(200, json={"rows": pubs})
        if method == "PUT":
            if "state" in body:
                href = body["state"]["meta"]["href"]
                st = next((s for s in self.states[t] if s["meta"]["href"] == href), None)
                if st is None:
                    return error(412, "неизвестный статус")
                obj["state"] = {"meta": st["meta"], "name": st["name"]}
            for a in body.get("attributes", []):
                d = next(x for x in self.attributes[t] if x["meta"]["href"] == a["meta"]["href"])
                obj["attributes"] = [x for x in obj["attributes"] if x["name"] != d["name"]]
                if a["value"] is not None:
                    obj["attributes"].append({"meta": d["meta"], "name": d["name"], "type": d["type"],
                                              "value": self.resolve_value(d, a["value"])})
            obj["updated"] = self.tick()
        return httpx.Response(200, json=obj)
