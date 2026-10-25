"""HTTP: вебхук МойСклад и REST API по заказам, отгрузкам, контрагентам."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import RecordingChannels, fresh_storage, settings_for
from fastapi.testclient import TestClient

from hub.api import create_app
from hub.moysklad import MoySklad
from hub.runtime import Hub

H = {"X-Api-Key": "k"}


@pytest.fixture
def api(fake):
    s = settings_for({"send_upd": {}, "shipped": {"uses": "notify", "entity": "demand",
                                                  "state": "Отгружен", "telegram_chats": ["-1"]}})

    async def factory():
        return Hub(s, await fresh_storage(), ms=MoySklad("test-token", transport=fake.transport()),
                   channels=RecordingChannels(s))

    with TestClient(create_app(s, background=False, factory=factory)) as client:
        yield client


def test_webhook_secret_and_queue(fake, api):
    d = fake.add_demand("00201")
    assert api.post("/hooks/moysklad/wrong", json={}).status_code == 404
    r = api.post("/hooks/moysklad/s3cret?requestId=abc",
                 json=fake.webhook_payload("demand", d["id"]))
    assert r.status_code == 204
    health = api.get("/healthz").json()
    assert health["pending"] == 1 and health["automations"] == ["send_upd", "shipped"]


def test_api_requires_key(api):
    assert api.get("/api/journal").status_code == 401
    assert api.get("/api/journal", headers=H).json() == []


def test_documents(fake, api):
    o = fake.add_order("0100", state="Подтверждён")
    fake.add_demand("00202", order=o)
    sh = api.get("/api/shipments/00202", headers=H).json()
    assert sh["agent"]["email"] == "buyer@example.ru" and sh["sum"] == 12345.6
    assert sh["order_number"] == "0100" and sh["type"] == "demand"
    order = api.get(f"/api/orders/{o['id']}", headers=H).json()
    assert order["state"] == "Подтверждён" and len(order["shipment_ids"]) == 1

    r = api.get("/api/shipments/00202/print?key=k")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert "filename*=UTF-8''" in r.headers["content-disposition"]
    assert api.get("/api/orders/0100/print", headers=H).status_code == 200     # счёт по умолчанию
    assert api.get("/api/shipments/00202/link", headers=H).json()["url"] == "https://mskld.ru/00202"
    assert api.get("/api/counterparties/7800000000/print", headers=H).status_code in (400, 409)


def test_states(fake, api):
    o = fake.add_order("0101")
    names = [s["name"] for s in api.get("/api/orders/states", headers=H).json()]
    assert "Собран" in names
    r = api.put("/api/orders/0101/state", json={"state": "Собран"}, headers=H)
    assert r.json()["state"] == "Собран"
    assert fake.db["customerorder"][o["id"]]["state"]["name"] == "Собран"
    bad = api.put("/api/orders/0101/state", json={"state": "Оплачен"}, headers=H)
    assert bad.status_code == 422 and "Подтверждён" in bad.json()["detail"]


def test_counterparty_fields(fake, api):
    fake.add_field("counterparty", "Канал продаж", "customentity", ["HoReCa", "Розница"])
    fake.add_field("counterparty", "Скидка, %", "double")
    c = fake.add_counterparty(name="Кофе Хаус", inn="7812345678")
    defs = api.get("/api/counterparties/fields", headers=H).json()
    assert {"name": "Канал продаж", "type": "customentity", "required": False} in defs

    r = api.patch("/api/counterparties/7812345678/fields",
                  json={"Канал продаж": "Розница", "Скидка, %": "5"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["fields"] == {"Канал продаж": "Розница", "Скидка, %": 5.0}
    assert api.get("/api/counterparties/Кофе Хаус", headers=H).json()["id"] == c["id"]
    bad = api.patch("/api/counterparties/7812345678/fields", json={"Регион": "СПб"}, headers=H)
    assert bad.status_code == 422


def test_manual_run_and_journal(fake, api):
    fake.add_demand("00203")
    assert api.post("/api/shipments/00203/run/send_upd", headers=H).json()["status"] == "done"
    assert api.post("/api/shipments/00203/run/send_upd", headers=H).json()["status"] == "already"
    assert api.post("/api/orders/00203/run/send_upd", headers=H).status_code == 404
    journal = api.get("/api/journal", headers=H).json()
    assert journal[0]["status"] == "done" and journal[0]["label"] == "Отгрузка 00203"
    assert api.get("/api/stats", headers=H).json()["runs"] == {"done": 1}
    assert api.app.state.hub.channels.emails[0]["to"] == ["buyer@example.ru"]


def test_listing(fake, api):
    today = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M:%S.000")
    for n, st in (("0201", "Новый"), ("0202", "Собран"), ("0203", "Новый")):
        fake.add_order(n, state=st)["moment"] = today
    fake.add_order("0204", state="Новый")["moment"] = "2020-01-01 10:00:00.000"
    rows = api.get("/api/orders?days=7&state=Новый", headers=H).json()
    assert sorted(r["name"] for r in rows) == ["0201", "0203"]
    assert api.get("/api/nope", headers=H).status_code == 404
