"""HTTP: вебхук МойСклад и REST API для других автоматизаций."""
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from hub.app import create_app


def client(make_hub):
    hub = make_hub()
    return hub, TestClient(create_app(hub, background=False))


def test_webhook_secret_and_fast_ack(fake, make_hub):
    hub, c = client(make_hub)
    d = fake.add_demand("00201")
    with c:
        assert c.post("/hooks/moysklad/wrong", json={}).status_code == 404
        r = c.post("/hooks/moysklad/s3cret?requestId=abc", json=fake.webhook_payload(d["id"]))
        assert r.status_code == 204
        assert c.get("/healthz").json() == {"ok": True, "dry_run": False, "pending": 1,
                                            "automations": ["send_upd"]}


def test_api_requires_key(fake, make_hub):
    hub, c = client(make_hub)
    with c:
        assert c.get("/api/journal").status_code == 401
        assert c.get("/api/journal", headers={"X-Api-Key": "k"}).json() == []


def test_shipment_print_and_manual_run(fake, make_hub):
    hub, c = client(make_hub)
    fake.add_demand("00202")
    h = {"X-Api-Key": "k"}
    with c:
        sh = c.get("/api/shipments/00202", headers=h).json()
        assert sh["agent"]["email"] == "buyer@example.ru" and sh["sum"] == 12345.6

        r = c.get("/api/shipments/00202/print?key=k")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        assert "filename*=UTF-8''" in r.headers["content-disposition"]

        assert c.get("/api/shipments/00202/link", headers=h).json()["url"] == "https://mskld.ru/00202"

        assert c.post("/api/shipments/00202/run/send_upd", headers=h).json()["status"] == "done"
        assert c.post("/api/shipments/00202/run/send_upd", headers=h).json()["status"] == "already"
        assert len(hub.channels.emails) == 1
        assert c.get("/api/journal", headers=h).json()[0]["status"] == "done"
        assert c.get("/api/shipments/nope", headers=h).status_code == 404


def test_shipments_list(fake, make_hub):
    hub, c = client(make_hub)
    today = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M:%S.000")
    for number, state in (("00203", "Отгружен"), ("00204", "Новый"), ("00205", "Новый")):
        fake.add_demand(number, state=state)["moment"] = today
    fake.add_demand("00206", state="Новый")["moment"] = "2020-01-01 10:00:00.000"
    with c:
        rows = c.get("/api/shipments?days=7&state=Новый", headers={"X-Api-Key": "k"}).json()
    assert sorted(r["number"] for r in rows) == ["00204", "00205"]
