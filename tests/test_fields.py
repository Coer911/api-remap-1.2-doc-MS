"""Статусы и доп. поля по человеческим именам — для заказов, отгрузок, контрагентов."""
import pytest

from hub.moysklad import FieldError, MoySklad
from hub.moysklad import fields as f


@pytest.fixture
async def ms(fake):
    async with MoySklad("test-token", transport=fake.transport()) as client:
        yield client


async def test_set_state_by_name(fake, ms):
    o = fake.add_order("0001")
    await f.set_state(ms, "customerorder", o["id"], "собран")
    assert fake.db["customerorder"][o["id"]]["state"]["name"] == "Собран"
    with pytest.raises(FieldError, match="«Подтверждён»"):
        await f.set_state(ms, "customerorder", o["id"], "Оплачен")


async def test_counterparty_fields_of_all_types(fake, ms):
    fake.add_field("counterparty", "Канал продаж", "customentity", ["HoReCa", "Розница"])
    fake.add_field("counterparty", "Скидка, %", "double")
    fake.add_field("counterparty", "Точек", "long")
    fake.add_field("counterparty", "VIP", "boolean")
    fake.add_field("counterparty", "Первый заказ", "time")
    fake.add_field("counterparty", "Комментарий", "text")
    c = fake.add_counterparty()
    await f.set_fields(ms, "counterparty", c["id"], {
        "Канал продаж": "horeca", "Скидка, %": "7,5", "Точек": "3", "VIP": "да",
        "Первый заказ": "25.10.2026", "Комментарий": 42})
    got = {k: fake.field("counterparty", c["id"], k) for k in
           ("Канал продаж", "Скидка, %", "Точек", "VIP", "Первый заказ", "Комментарий")}
    assert got == {"Канал продаж": "HoReCa", "Скидка, %": 7.5, "Точек": 3, "VIP": True,
                   "Первый заказ": "2026-10-25 00:00:00", "Комментарий": "42"}

    await f.set_fields(ms, "counterparty", c["id"], {"VIP": None})       # сброс значения
    assert fake.field("counterparty", c["id"], "VIP") is None


async def test_field_errors_are_human(fake, ms):
    fake.add_field("counterparty", "Канал продаж", "customentity", ["HoReCa"])
    fake.add_field("counterparty", "Точек", "long")
    c = fake.add_counterparty()
    with pytest.raises(FieldError, match="нет доп. поля «Регион».*«Канал продаж»"):
        await f.set_fields(ms, "counterparty", c["id"], {"Регион": "СПб"})
    with pytest.raises(FieldError, match="нет «Опт».*«HoReCa»"):
        await f.set_fields(ms, "counterparty", c["id"], {"Канал продаж": "Опт"})
    with pytest.raises(FieldError, match="«много» не подходит"):
        await f.set_fields(ms, "counterparty", c["id"], {"Точек": "много"})
