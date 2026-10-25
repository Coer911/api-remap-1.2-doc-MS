"""УПД уходит покупателю ровно один раз — что бы ни прилетало и что бы ни падало."""
import pytest

from hub.automations import ConfigError, enabled
from hub.ingest import accept_webhook, poll
from hub.store import Store


async def test_webhook_sends_upd_once(fake, make_hub):
    hub = make_hub()
    d = fake.add_demand("00101")
    payload = fake.webhook_payload(d["id"], fields=["state"])

    assert accept_webhook(hub.store, payload, "req-1") == 1
    outcomes = await hub.tick()
    assert [o.status for o in outcomes] == ["done"]

    mail = hub.channels.emails[0]
    assert mail["to"] == ["buyer@example.ru"]
    assert mail["subject"] == "УПД № 00101 от 25.10.2026 — ИП Культура"
    assert "12 345,60 ₽" in mail["body"]
    name, data, mime = mail["attachments"][0]
    assert name == "УПД 00101 от 25.10.2026.pdf" and data.startswith(b"%PDF")
    assert mime == "application/pdf"
    assert fake.attribute_value(d["id"], "УПД отправлен").endswith("письмо buyer@example.ru")

    # МойСклад повторил то же уведомление — в очередь не попадает
    assert accept_webhook(hub.store, payload, "req-1") == 0
    # новое изменение той же отгрузки — автоматизация уже отработала
    accept_webhook(hub.store, fake.webhook_payload(d["id"], fields=["description"]), "req-2")
    assert [o.status for o in await hub.tick()] == ["already"]
    assert len(hub.channels.emails) == 1
    assert fake.exports == 1


async def test_waits_for_state_then_poll_catches_it(fake, make_hub):
    hub = make_hub()
    d = fake.add_demand("00102", state="Новый")
    hub.store.set("cursor:demand", "2026-10-25 00:00:00")

    assert await poll(hub.ms, hub.store) == 1
    assert await hub.tick() == []                     # статус не тот — ничего не делаем
    assert await poll(hub.ms, hub.store) == 0         # без изменений — без событий

    fake.set_state(d["id"], "Отгружен")
    assert await poll(hub.ms, hub.store) == 1
    assert [o.status for o in await hub.tick()] == ["done"]
    assert len(hub.channels.emails) == 1


async def test_no_email_is_skipped_not_retried(fake, make_hub):
    hub = make_hub()
    d = fake.add_demand("00103", email="")
    accept_webhook(hub.store, fake.webhook_payload(d["id"]), "r")
    assert [o.status for o in await hub.tick()] == ["skipped"]
    assert hub.store.journal(status="skipped")[0].result.startswith("у контрагента")
    assert hub.store.due() == []


async def test_partial_failure_does_not_resend_finished_channel(fake, make_hub):
    hub = make_hub(channels=["email", "telegram"], telegram_chats=["-100"])
    hub.channels.fail["telegram"] = 1
    d = fake.add_demand("00104")
    accept_webhook(hub.store, fake.webhook_payload(d["id"]), "r")

    assert [o.status for o in await hub.tick()] == ["failed"]
    assert len(hub.channels.emails) == 1 and hub.channels.documents == []

    # время повтора пришло
    hub.store._exec("UPDATE runs SET next_at = '2000-01-01T00:00:00Z'")
    assert [o.status for o in await hub.tick()] == ["done"]
    assert len(hub.channels.emails) == 1               # письмо второй раз не ушло
    assert hub.channels.documents[0]["filename"] == "УПД 00104 от 25.10.2026.pdf"


async def test_gives_up_and_alerts(fake, make_hub):
    hub = make_hub()
    hub.channels.fail["email"] = 100
    d = fake.add_demand("00105")
    accept_webhook(hub.store, fake.webhook_payload(d["id"]), "r")
    await hub.tick()
    for _ in range(10):
        hub.store._exec("UPDATE runs SET next_at = '2000-01-01T00:00:00Z' WHERE status = 'failed'")
        await hub.tick()
    run = hub.store.journal()[0]
    assert run.status == "dead" and run.attempts == 6
    assert len(hub.channels.alerts) == 1 and "00105" in hub.channels.alerts[0]


async def test_mark_in_moysklad_survives_lost_database(fake, make_hub):
    hub = make_hub()
    d = fake.add_demand("00106")
    accept_webhook(hub.store, fake.webhook_payload(d["id"]), "r")
    await hub.tick()

    fresh = make_hub(store=Store(":memory:"))          # база хаба потерялась при релизе
    accept_webhook(fresh.store, fake.webhook_payload(d["id"]), "r2")
    assert [o.detail for o in await fresh.tick()] == ["отметка в МойСклад"]
    assert fresh.channels.emails == []


async def test_dry_run_sends_nothing_and_does_not_block_live(fake, make_hub):
    dry = make_hub(dry_run=True)
    store = dry.store
    d = fake.add_demand("00107")
    accept_webhook(store, fake.webhook_payload(d["id"]), "r")
    assert [o.detail for o in await dry.tick()] == ["dry-run email"]
    assert dry.channels.emails == [] and dry.channels.sent[0]["to"] == ["buyer@example.ru"]
    assert fake.attribute_value(d["id"], "УПД отправлен") is None

    live = make_hub(store=store)
    accept_webhook(store, fake.webhook_payload(d["id"]), "r2")
    assert [o.status for o in await live.tick()] == ["done"]
    assert len(live.channels.emails) == 1


async def test_webhook_channel_gets_file_and_link(fake, make_hub):
    hub = make_hub(channels=["webhook"], webhook_url="https://n8n.example.ru/upd", with_link=True)
    d = fake.add_demand("00108")
    accept_webhook(hub.store, fake.webhook_payload(d["id"]), "r")
    await hub.tick()
    payload = hub.channels.hooks[0]["payload"]
    assert payload["link"] == "https://mskld.ru/00108"
    assert payload["shipment"]["number"] == "00108" and payload["file"]["mime"] == "application/pdf"


async def test_filters_organization_and_unapplied(fake, make_hub):
    hub = make_hub(organizations=["ООО Другое"])
    d = fake.add_demand("00109")
    accept_webhook(hub.store, fake.webhook_payload(d["id"]), "r")
    assert await hub.tick() == []
    hub2 = make_hub()
    d2 = fake.add_demand("00110", applicable=False)
    accept_webhook(hub2.store, fake.webhook_payload(d2["id"]), "r")
    assert await hub2.tick() == []


def test_config_is_validated():
    with pytest.raises(ConfigError, match="такой нет"):
        enabled({"send_upd_typo": {}})
    with pytest.raises(ConfigError, match="неизвестные параметры"):
        enabled({"send_upd": {"stat": "Отгружен"}})
    with pytest.raises(ConfigError, match="webhook_url"):
        enabled({"send_upd": {"channels": ["webhook"]}})
    assert enabled({"send_upd": {"enabled": False}}) == []
