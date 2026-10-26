"""УПД за день в 17:00: только отгруженные по заказам, только ИП и ООО, только сегодня."""
from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
AT_17 = datetime(2026, 10, 25, 17, 0, tzinfo=MSK)


def day_of_shipments(fake):
    """Отгрузки за 25.10.2026 на все случаи + одна вчерашняя."""
    ooo = fake.add_counterparty("ООО «Круч»", email="kruch@example.ru")
    ip = fake.add_counterparty("ИП Иванов", email="ivanov@example.ru; buh@example.ru",
                               company_type="entrepreneur")
    person = fake.add_counterparty("Петров Пётр", email="p@example.ru", company_type="individual")
    nomail = fake.add_counterparty("ООО «Рябчик»", email="")
    ao = fake.add_counterparty("АО «Завод»", email="zavod@example.ru")
    order = fake.add_order("1001")
    fake.add_demand("D1", state="ОТПРАВЛЕН", agent=ooo, order=order)
    fake.add_demand("D2", state="ОТПРАВЛЕН", agent=ip, order=order)
    fake.add_demand("D3", state="ОТПРАВЛЕН В ЭДО", agent=ip, order=order)
    fake.add_demand("D4", agent=person, order=order)                     # физлицо
    fake.add_demand("D5", agent=ooo)                                     # без заказа
    fake.add_demand("D6", agent=ooo, order=order, applicable=False)      # не проведена
    fake.add_demand("D7", agent=nomail, order=order)                     # нет e-mail
    fake.add_demand("D8", agent=ao, order=order)                         # АО — не ООО/ИП
    fake.add_demand("D9", agent=ooo, order=order)["moment"] = "2026-10-24 12:00:00.000"


async def test_sends_one_letter_per_counterparty(fake, make_hub):
    hub = await make_hub({"daily_upd": {}})
    day_of_shipments(fake)
    assert await hub.engine.scheduled(AT_17.replace(hour=16, minute=59)) == []

    outcome = (await hub.engine.scheduled(AT_17))[0]
    assert outcome.status == "done", outcome.detail
    letters = {tuple(m["to"]): m for m in hub.channels.emails}
    assert set(letters) == {("kruch@example.ru",), ("ivanov@example.ru", "buh@example.ru")}

    kruch = letters[("kruch@example.ru",)]
    assert [a[0] for a in kruch["attachments"]] == ["УПД D1 от 25.10.2026.pdf"]
    assert kruch["subject"] == "УПД за 25.10.2026 — ИП Культура"
    ivanov = letters[("ivanov@example.ru", "buh@example.ru")]
    assert [a[0] for a in ivanov["attachments"]] == ["УПД D2 от 25.10.2026.pdf",
                                                     "УПД D3 от 25.10.2026.pdf"]
    assert "УПД № D2 на сумму 12 345,60 ₽" in ivanov["body"]

    assert "писем 2, документов 3" in outcome.detail
    assert "без e-mail: ООО «Рябчик» (D7)" in outcome.detail
    for reason in ("физлицо — 1", "без заказа — 1", "не проведена — 1", "не ООО/ИП по названию — 1"):
        assert reason in outcome.detail


async def test_runs_once_a_day_and_never_resends(fake, make_hub):
    hub = await make_hub({"daily_upd": {}})
    day_of_shipments(fake)
    await hub.engine.scheduled(AT_17)
    assert await hub.engine.scheduled(AT_17.replace(hour=18)) == []    # слот дня уже отработан
    assert len(hub.channels.emails) == 2

    # ручной перезапуск того же дня: новых писем нет, а поздняя отгрузка уходит
    late = fake.add_demand("D10", agent=fake.add_counterparty("ООО «Поздний»", email="late@example.ru"),
                           order=fake.add_order("1002"))
    assert late
    a = hub.engine.by_name["daily_upd"]
    key = hub.ctx.run_key(a, "slot:2026-10-25")
    await hub.storage.try_start(a.name, key, entity="schedule", entity_id="2026-10-25",
                                label="x", lease=60, force=True)
    out = await hub.engine._run(a, key, "x", lambda: a.run_scheduled(hub.ctx, "2026-10-25"))
    assert "писем 1, документов 1" in out.detail and "отправлено раньше: 3" in out.detail
    assert [m["to"] for m in hub.channels.emails][-1] == ["late@example.ru"]


async def test_failed_letter_is_retried_without_duplicates(fake, make_hub):
    from hub.storage import runs
    hub = await make_hub({"daily_upd": {"report_chats": ["-1"]}})
    day_of_shipments(fake)
    hub.channels.fail["email"] = 1                         # первое письмо не ушло
    outcome = (await hub.engine.scheduled(AT_17))[0]
    assert outcome.status == "failed" and "ошибки" in outcome.detail
    assert len(hub.channels.emails) == 1
    async with hub.storage._tx() as conn:
        await conn.execute(runs.update().values(next_at="2000-01-01T00:00:00Z"))
    retry = await hub.engine.retry_due()
    assert retry[0].status == "done" and "писем 1" in retry[0].detail
    assert len(hub.channels.emails) == 2                   # каждому ровно по одному
    assert "УПД за 25.10.2026" in hub.channels.messages[-1]["text"]


async def test_states_filter_and_dry_run(fake, make_hub):
    hub = await make_hub({"daily_upd": {"exclude_states": ["ОТПРАВЛЕН В ЭДО"]}}, dry_run=True)
    day_of_shipments(fake)
    outcome = (await hub.engine.scheduled(AT_17))[0]
    assert hub.channels.emails == []                       # dry-run: ничего не ушло
    assert {tuple(x["files"]) for x in hub.channels.sent} == {
        ("УПД D1 от 25.10.2026.pdf",), ("УПД D2 от 25.10.2026.pdf",)}
    assert "статус «ОТПРАВЛЕН В ЭДО» — 1" in outcome.detail

    live = await make_hub({"daily_upd": {"exclude_states": ["ОТПРАВЛЕН В ЭДО"]}}, storage=hub.storage)
    await live.engine.scheduled(AT_17)                     # пробный прогон не мешает боевому
    assert len(live.channels.emails) == 2
