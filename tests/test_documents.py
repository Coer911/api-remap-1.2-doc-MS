"""Документы покупателю: УПД по отгрузке, счёт по заказу — ровно один раз."""
import pytest
from conftest import fresh_storage, webhook

from hub.automations import ConfigError, build
from hub.ingest import poll

UPD = {"send_upd": {}}


async def test_upd_sent_once_with_mark(fake, make_hub):
    hub = await make_hub(UPD)
    d = fake.add_demand("00101")
    assert await webhook(hub, fake, "demand", d["id"], "req-1", fields=["state"]) == 1
    assert [o.status for o in await hub.tick()] == ["done"]

    mail = hub.channels.emails[0]
    assert mail["to"] == ["buyer@example.ru"]
    assert mail["subject"] == "УПД № 00101 от 25.10.2026 — ИП Культура"
    assert "12 345,60 ₽" in mail["body"]
    name, data, mime = mail["attachments"][0]
    assert name == "УПД 00101 от 25.10.2026.pdf" and data.startswith(b"%PDF")
    assert fake.field("demand", d["id"], "УПД отправлен").endswith("письмо buyer@example.ru")

    assert await webhook(hub, fake, "demand", d["id"], "req-1") == 0       # повтор МойСклад
    await webhook(hub, fake, "demand", d["id"], "req-2", fields=["description"])
    assert [o.status for o in await hub.tick()] == ["already"]
    assert len(hub.channels.emails) == 1 and fake.exports == 1


async def test_waits_for_state_and_poll_catches_it(fake, make_hub):
    hub = await make_hub(UPD)
    d = fake.add_demand("00102", state="Новый")
    await hub.storage.set("cursor:demand", "2026-10-25 00:00:00")
    assert await poll(hub.ms, hub.storage, "demand") == 1
    assert await hub.tick() == []
    assert await poll(hub.ms, hub.storage, "demand") == 0
    fake.set_state("demand", d["id"], "Отгружен")
    assert await poll(hub.ms, hub.storage, "demand") == 1
    assert [o.status for o in await hub.tick()] == ["done"]


async def test_no_email_is_skipped_not_retried(fake, make_hub):
    hub = await make_hub(UPD)
    d = fake.add_demand("00103", email="")
    await webhook(hub, fake, "demand", d["id"], "r")
    assert [o.status for o in await hub.tick()] == ["skipped"]
    assert await hub.storage.due() == []


async def test_partial_failure_does_not_resend_finished_channel(fake, make_hub):
    hub = await make_hub({"send_upd": {"channels": ["email", "telegram"], "telegram_chats": ["-100"]}})
    hub.channels.fail["telegram"] = 1
    d = fake.add_demand("00104")
    await webhook(hub, fake, "demand", d["id"], "r")
    assert [o.status for o in await hub.tick()] == ["failed"]
    async with hub.storage._tx() as conn:
        from hub.storage import runs
        await conn.execute(runs.update().values(next_at="2000-01-01T00:00:00Z"))
    assert [o.status for o in await hub.tick()] == ["done"]
    assert len(hub.channels.emails) == 1 and len(hub.channels.documents) == 1


async def test_gives_up_and_alerts(fake, make_hub):
    from hub.storage import runs
    hub = await make_hub(UPD)
    hub.channels.fail["email"] = 100
    d = fake.add_demand("00105")
    await webhook(hub, fake, "demand", d["id"], "r")
    await hub.tick()
    for _ in range(8):
        async with hub.storage._tx() as conn:
            await conn.execute(runs.update().where(runs.c.status == "failed")
                               .values(next_at="2000-01-01T00:00:00Z"))
        await hub.tick()
    run = (await hub.storage.journal())[0]
    assert run.status == "dead" and run.attempts == 6
    assert len(hub.channels.alerts) == 1 and "00105" in hub.channels.alerts[0]


async def test_mark_in_moysklad_survives_lost_database(fake, make_hub):
    hub = await make_hub(UPD)
    d = fake.add_demand("00106")
    await webhook(hub, fake, "demand", d["id"], "r")
    await hub.tick()
    if not hub.storage.is_sqlite:
        pytest.skip("на общей PostgreSQL «потерянную базу» не изобразить")
    fresh = await make_hub(UPD, storage=await fresh_storage())
    await webhook(fresh, fake, "demand", d["id"], "r2")
    assert [o.detail for o in await fresh.tick()] == ["отметка в МойСклад"]
    assert fresh.channels.emails == []


async def test_dry_run_writes_nothing_and_does_not_block_live(fake, make_hub):
    dry = await make_hub(UPD, dry_run=True)
    d = fake.add_demand("00107")
    await webhook(dry, fake, "demand", d["id"], "r")
    outcome = (await dry.tick())[0]
    assert outcome.status == "done" and "dry-run" in outcome.detail
    assert dry.channels.emails == [] and dry.channels.sent[0]["to"] == ["buyer@example.ru"]
    assert fake.field("demand", d["id"], "УПД отправлен") is None

    live = await make_hub(UPD, storage=dry.storage)
    await webhook(live, fake, "demand", d["id"], "r2")
    assert [o.status for o in await live.tick()] == ["done"]
    assert len(live.channels.emails) == 1


async def test_order_invoice_and_state_after(fake, make_hub):
    hub = await make_hub({"order_invoice": {
        "uses": "send_document", "entity": "customerorder", "state": "Подтверждён",
        "template": "Счет покупателю", "file_title": "Счёт", "channels": ["email", "webhook"],
        "webhook_url": "https://n8n.example.ru/invoice", "with_link": True,
        "state_after": "Собран"}})
    o = fake.add_order("0042", state="Подтверждён")
    await webhook(hub, fake, "customerorder", o["id"], "r")
    assert [x.status for x in await hub.tick()] == ["done"]
    assert hub.channels.emails[0]["attachments"][0][0] == "Счёт 0042 от 25.10.2026.pdf"
    payload = hub.channels.hooks[0]["payload"]
    assert payload["link"] == "https://mskld.ru/0042" and payload["entity"] == "customerorder"
    assert fake.db["customerorder"][o["id"]]["state"]["name"] == "Собран"


def test_config_is_validated():
    with pytest.raises(ConfigError, match="нет автоматизации"):
        build({"send_upd_typo": {}})
    with pytest.raises(ConfigError, match="неизвестные параметры"):
        build({"send_upd": {"stat": "Отгружен"}})
    with pytest.raises(ConfigError, match="webhook_url"):
        build({"send_upd": {"channels": ["webhook"]}})
    with pytest.raises(ConfigError, match="умеет demand"):
        build({"send_upd": {"entity": "counterparty"}})
    with pytest.raises(ConfigError, match="template"):
        build({"x": {"uses": "send_document"}})
    assert build({"send_upd": {"enabled": False}}) == []
