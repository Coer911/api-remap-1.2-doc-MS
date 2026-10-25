"""Статусы, уведомления, поля контрагентов, расписание."""
from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import webhook

from hub.ingest import poll

MSK = ZoneInfo("Europe/Moscow")


async def test_notify_on_state_change_detected_by_polling(fake, make_hub):
    """Опрос не сообщает, откуда пришёл статус, — хаб знает это по своему снимку."""
    hub = await make_hub({"assembled": {
        "uses": "notify", "entity": "customerorder", "when": "state_changed",
        "from_state": "Подтверждён", "state": "Собран", "telegram_chats": ["-100"],
        "text": "Собран заказ {number} · {agent}"}})
    o = fake.add_order("0001", state="Новый")
    await hub.storage.set("cursor:customerorder", "2026-10-25 00:00:00")

    await poll(hub.ms, hub.storage, "customerorder")
    assert await hub.tick() == []                              # впервые увидели — не переход
    fake.set_state("customerorder", o["id"], "Собран")         # Новый → Собран: не тот from_state
    await poll(hub.ms, hub.storage, "customerorder")
    assert await hub.tick() == []
    fake.set_state("customerorder", o["id"], "Подтверждён")
    await poll(hub.ms, hub.storage, "customerorder")
    await hub.tick()
    fake.set_state("customerorder", o["id"], "Собран")         # Подтверждён → Собран
    await poll(hub.ms, hub.storage, "customerorder")
    assert [x.status for x in await hub.tick()] == ["done"]
    assert hub.channels.messages == [{"chat": "-100", "text": "Собран заказ 0001 · ООО «Кофейня»"}]


async def test_notify_in_state_once_per_entering(fake, make_hub):
    hub = await make_hub({"shipped": {"uses": "notify", "entity": "demand", "state": "Отгружен",
                                      "telegram_chats": ["-1"]}})
    d = fake.add_demand("00301")
    await webhook(hub, fake, "demand", d["id"], "r1")
    await webhook(hub, fake, "demand", d["id"], "r2")          # склеится с первым
    assert [x.status for x in await hub.tick()] == ["done"]
    await webhook(hub, fake, "demand", d["id"], "r3")
    assert [x.status for x in await hub.tick()] == ["already"]
    assert len(hub.channels.messages) == 1


async def test_shipment_moves_its_order(fake, make_hub):
    hub = await make_hub({"sync_order_state": {"only_from": ["Подтверждён", "Собран"]}})
    o = fake.add_order("0002", state="Собран")
    d = fake.add_demand("00302", order=o)
    await webhook(hub, fake, "demand", d["id"], "r")
    out = await hub.tick()
    assert out[0].status == "done" and "«Собран» → статус «Отгружен»" in out[0].detail
    assert fake.db["customerorder"][o["id"]]["state"]["name"] == "Отгружен"

    cancelled = fake.add_order("0003", state="Отменён")
    d2 = fake.add_demand("00303", order=cancelled)
    await webhook(hub, fake, "demand", d2["id"], "r2")
    assert [x.status for x in await hub.tick()] == ["skipped"]
    assert fake.db["customerorder"][cancelled["id"]]["state"]["name"] == "Отменён"


async def test_counterparty_fields_on_create(fake, make_hub):
    fake.add_field("counterparty", "Канал продаж", "customentity", ["HoReCa", "Розница"])
    fake.add_field("counterparty", "Источник", "string")
    hub = await make_hub({"new_client": {
        "uses": "counterparty_fields", "fields": {"Канал продаж": "HoReCa", "Источник": "хаб · ИНН {inn}"},
        "set_state": "Новый"}})
    c = fake.add_counterparty(inn="7812345678")
    await webhook(hub, fake, "counterparty", c["id"], "r", action="CREATE")
    out = await hub.tick()
    assert out[0].status == "done", out
    assert fake.field("counterparty", c["id"], "Канал продаж") == "HoReCa"
    assert fake.field("counterparty", c["id"], "Источник") == "хаб · ИНН 7812345678"
    assert fake.db["counterparty"][c["id"]]["state"]["name"] == "Новый"
    # наше же изменение пришло вебхуком — делать нечего, петли нет
    await webhook(hub, fake, "counterparty", c["id"], "r2", fields=["attributes"])
    assert await hub.tick() == []


async def test_counterparty_fields_keep_manual_values(fake, make_hub):
    fake.add_field("counterparty", "Канал продаж", "customentity", ["HoReCa", "Розница"])
    hub = await make_hub({"fill": {"uses": "counterparty_fields", "when": "any",
                                   "fields": {"Канал продаж": "HoReCa"}}})
    c = fake.add_counterparty()
    c["attributes"].append({"name": "Канал продаж", "value": {"name": "Розница", "meta": {}}})
    await webhook(hub, fake, "counterparty", c["id"], "r")
    assert await hub.tick() == []
    assert fake.field("counterparty", c["id"], "Канал продаж") == "Розница"


async def test_dry_run_does_not_write_to_moysklad(fake, make_hub):
    hub = await make_hub({"sync_order_state": {}}, dry_run=True)
    o = fake.add_order("0004", state="Собран")
    d = fake.add_demand("00304", order=o)
    await webhook(hub, fake, "demand", d["id"], "r")
    assert "dry-run" in (await hub.tick())[0].detail
    assert fake.db["customerorder"][o["id"]]["state"]["name"] == "Собран"
    assert hub.ctx.writes[0]["state"] == "Отгружен"


async def test_daily_digest_once_per_day(fake, make_hub):
    hub = await make_hub({"daily_digest": {"at": "18:00", "telegram_chats": ["-5"]}})
    fake.add_order("0005", state="Новый")
    fake.add_order("0006", state="Новый")
    fake.add_demand("00305")
    morning = datetime(2026, 10, 25, 9, 0, tzinfo=MSK)
    evening = datetime(2026, 10, 25, 18, 5, tzinfo=MSK)
    assert await hub.engine.scheduled(morning) == []
    assert [x.status for x in await hub.engine.scheduled(evening)] == ["done"]
    assert await hub.engine.scheduled(evening) == []
    text = hub.channels.messages[0]["text"]
    assert "Заказы: 2" in text and "Новый: 2 · 24 691,20 ₽" in text and "Отгрузки: 1" in text
