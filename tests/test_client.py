"""Клиент МойСклад соблюдает правила API."""
import pytest

from hub.moysklad import MoySklad, NotFound


async def test_headers_paging_and_429(fake):
    for i in range(250):
        fake.add_order(f"{i:05d}")
    fake.throttle_next = 1
    async with MoySklad("test-token", transport=fake.transport()) as ms:
        rows = [r async for r in ms.list("customerorder", expand="state", limit=1000)]
    assert len(rows) == 250
    assert not fake.violations, fake.violations           # expand урезал limit до 100
    assert fake.calls.count(("GET", "entity/customerorder")) == 4   # 429 + 3 страницы


async def test_export_follows_303_and_keeps_token_home(fake):
    d = fake.add_demand("00042")
    async with MoySklad("test-token", transport=fake.transport()) as ms:
        tpl = await ms.find_template("demand", "Универсальный передаточный документ")
        data = await ms.export("demand", d["id"], tpl, "pdf")
    assert data.startswith(b"%PDF") and b"00042-pdf" in data
    assert not fake.violations, fake.violations


async def test_templates_per_type_and_prefix_match(fake):
    async with MoySklad("test-token", transport=fake.transport()) as ms:
        assert (await ms.find_template("customerorder", "счет"))["name"] == "Счет покупателю"
        with pytest.raises(NotFound, match="ТОРГ-12"):
            await ms.find_template("demand", "Акт")


async def test_publication_is_reused(fake):
    d = fake.add_demand("00043")
    async with MoySklad("test-token", transport=fake.transport()) as ms:
        tpl = await ms.find_template("demand", "Универсальный")
        assert await ms.publish("demand", d["id"], tpl) == await ms.publish("demand", d["id"], tpl)
    assert len(fake.publications[d["id"]]) == 1


async def test_metadata_is_cached(fake):
    async with MoySklad("test-token", transport=fake.transport()) as ms:
        await ms.metadata("demand")
        await ms.metadata("demand")
    assert fake.calls.count(("GET", "entity/demand/metadata")) == 1


async def test_not_found(fake):
    async with MoySklad("test-token", transport=fake.transport()) as ms:
        with pytest.raises(NotFound):
            await ms.entity("demand", "00000000-0000-0000-0000-000000000000")


def test_budget_follows_moysklad_schedule():
    from datetime import date

    from hub.moysklad.client import user_token_budget
    assert user_token_budget(date(2026, 1, 10)) == 44
    assert user_token_budget(date(2026, 5, 12)) == 21
    assert user_token_budget(date(2026, 10, 25)) == 14
    assert user_token_budget(date(2026, 12, 1)) == 10
    assert MoySklad("t", rps=45).budget == 45
