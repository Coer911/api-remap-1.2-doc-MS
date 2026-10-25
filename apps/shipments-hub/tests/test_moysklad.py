"""Клиент МойСклад соблюдает правила API из документации форка."""
from hub.moysklad import MoySklad, MoySkladError


async def test_headers_paging_and_429(fake):
    for i in range(250):
        fake.add_demand(f"{i:05d}")
    fake.throttle_next = 1
    async with MoySklad("test-token", transport=fake.transport()) as ms:
        rows = [r async for r in ms.demands()]
    assert len(rows) == 250
    assert not fake.violations, fake.violations
    pages = [c for c in fake.calls if c == ("GET", "entity/demand")]
    assert len(pages) == 4                     # 429 + три страницы по 100 (expand → limit 100)


async def test_export_follows_303_and_keeps_token_home(fake):
    d = fake.add_demand("00042")
    async with MoySklad("test-token", transport=fake.transport()) as ms:
        tpl = await ms.find_template("Универсальный передаточный документ")
        data = await ms.export("demand", d["id"], tpl, "pdf")
    assert data.startswith(b"%PDF")
    assert b"00042-pdf" in data
    assert not fake.violations, fake.violations


async def test_template_lookup_lists_alternatives(fake):
    async with MoySklad("test-token", transport=fake.transport()) as ms:
        assert (await ms.find_template("торг-12"))["id"] == "torg12"
        try:
            await ms.find_template("Счёт")
        except MoySkladError as e:
            assert "ТОРГ-12" in e.message and "Универсальный" in e.message
        else:
            raise AssertionError("ожидалась ошибка")


async def test_publication_is_reused(fake):
    d = fake.add_demand("00043")
    async with MoySklad("test-token", transport=fake.transport()) as ms:
        tpl = await ms.find_template("Универсальный передаточный документ")
        first = await ms.publish("demand", d["id"], tpl)
        second = await ms.publish("demand", d["id"], tpl)
    assert first == second == "https://mskld.ru/00043"
    assert len(fake.publications[d["id"]]) == 1


async def test_404_is_error_with_status(fake):
    async with MoySklad("test-token", transport=fake.transport()) as ms:
        try:
            await ms.demand("00000000-0000-0000-0000-000000000000")
        except MoySkladError as e:
            assert e.status == 404
        else:
            raise AssertionError("ожидалась ошибка")
