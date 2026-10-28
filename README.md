# moysklad-hub — автоматизации МойСклад

Сервис, на котором пишутся автоматизации вокруг **заказов, отгрузок и контрагентов**
МойСклад. Он сам узнаёт об изменениях (вебхуки и страховочный опрос), знает, из какого
статуса в какой перешёл документ, и прогоняет каждое изменение через автоматизации-плагины.
Очередь, повторы, защита от двойной отправки, лимиты API, пробный режим и журнал уже
встроены. Новая автоматизация — это один файл или запись в `config.yaml`.

Что умеет из коробки:

| Автоматизация | Что делает |
|---|---|
| `daily_upd` | **каждый день в 17:00** — УПД за сегодня на почту: только отгруженные, по заказам, ИП и ООО; одно письмо на контрагента, отчёт — у кого нет e-mail |
| `send_upd` | отгрузка в нужном статусе → УПД покупателю сразу (почта / Telegram / вебхук), отметка в доп. поле |
| `send_document` | любая печатная форма заказа или отгрузки: счёт, ТОРГ-12, заказ… + статус после отправки |
| `sync_order_state` | отгрузили — связанный заказ сам получает статус «Отгружен» (не трогая «Отменён») |
| `notify` | заказ, отгрузка или контрагент вошли в статус, создан или перешёл A → B → сообщение в Telegram/почту/вебхук |
| `counterparty_fields` | новому (или любому) контрагенту — значения доп. полей и статус, не перетирая ручное |
| `daily_digest` | по расписанию: сводка за день по заказам и отгрузкам по статусам |

И REST API: статусы и доп. поля любого заказа, отгрузки и контрагента по человеческим
именам, печатные формы файлом, публичные ссылки, ручной запуск автоматизаций.

## Быстрый старт

```bash
cp .env.example .env              # MOYSKLAD_TOKEN, HUB_DRY_RUN=1 — остальное по желанию
docker build -t moysklad-hub .
docker run --rm --env-file .env moysklad-hub python -m hub check
docker run --rm --env-file .env moysklad-hub python -m hub states shipments
docker run --rm --env-file .env moysklad-hub python -m hub fields counterparties
docker run -d --name hub -p 8080:8080 --env-file .env \
  -v "$PWD/config.yaml:/app/config.yaml" -v hubdata:/data moysklad-hub
curl localhost:8080/healthz
```

`HUB_DRY_RUN=1` — хаб пишет в журнал, что сделал бы, но ничего не отправляет и не меняет
в МойСклад. Так и начинайте; боевой режим — `HUB_DRY_RUN=0`.

Без Docker: `python3 -m pip install -r requirements-dev.txt && python3 -m pytest -q && python3 -m hub serve`.

## Как это работает

Создали заказ → поменяли статус → отгрузили:

1. МойСклад шлёт вебхук `POST /hooks/moysklad/<секрет>`. Хаб кладёт событие в очередь
   и отвечает за миллисекунды: МойСклад ждёт не дольше 1,5 с.
2. Воркер берёт событие, загружает свежую сущность и сверяет статус со своим снимком.
   Так он знает переход, например «Подтверждён → Собран», даже если событие пришло опросом.
3. Каждая подходящая автоматизация запускается **ровно один раз** на свой ключ.
   При сбое — повтор через 1 мин, 5 мин, 30 мин, 2 ч, 6 ч, потом алерт в Telegram.
4. Опрос раз в 5 минут подхватывает всё, что вебхук потерял, пока хаб лежал, или на
   бесплатном тарифе, где вебхуков нет.

Подробно, с масштабированием — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Что дальше — [docs/ROADMAP.md](docs/ROADMAP.md).

## Настройка

**Секреты** — в переменных окружения, список с пояснениями в [`.env.example`](.env.example).
**Правила** — в [`config.yaml`](config.yaml). Там готовые примеры для всех автоматизаций,
кроме УПД они выключены (`enabled: false`). Одну автоматизацию можно включить несколько
раз под разными именами (`uses:`), например `notify` для разных статусов и чатов.
Опечатку в параметре хаб не проглотит: откажется стартовать и скажет, что не так.

```bash
python -m hub check                         # всё ли сходится с вашим МойСклад
python -m hub states orders|shipments|counterparties
python -m hub templates orders|shipments
python -m hub fields counterparties         # доп. поля и их типы
python -m hub setup                         # создать доп. поле-отметку «УПД отправлен»
python -m hub webhooks install              # вебхуки на нужные типы → хаб
python -m hub run send_upd 00123            # запустить на конкретной отгрузке
python -m hub set-state orders 0042 "Собран"
python -m hub set-field counterparties 7812345678 "Канал продаж" HoReCa
python -m hub journal --status failed
```

## REST API

Заголовок `X-Api-Key: $HUB_API_KEY` (для ссылок из браузера — `?key=`).
`{kind}`: `orders` | `shipments` | `counterparties`. `{ref}`: id, номер документа,
а для контрагента ещё ИНН или точное имя.

| Запрос | Что делает |
|---|---|
| `GET /api/{kind}?days=7&state=…&search=…` | список за период |
| `GET /api/{kind}/{ref}` | одна запись + журнал автоматизаций по ней |
| `GET /api/{kind}/states` · `GET /api/{kind}/fields` | статусы и доп. поля типа |
| `PUT /api/{kind}/{ref}/state` `{"state": "Собран"}` | сменить статус |
| `PATCH /api/{kind}/{ref}/fields` `{"Канал продаж": "HoReCa", "Скидка, %": 5}` | записать доп. поля |
| `GET /api/{kind}/{ref}/print?template=…&format=pdf` | печатная форма файлом |
| `GET /api/{kind}/{ref}/link` | публичная ссылка mskld.ru |
| `POST /api/{kind}/{ref}/run/{автоматизация}?force=1` | запустить вручную |
| `GET /api/journal` · `/api/stats` · `/api/automations` · `/healthz` | журнал и состояние |

Доп. поля пишутся по имени, тип хаб приводит сам: строка, текст, ссылка, целое, дробное
(«7,5»), флажок («да»), дата («25.10.2026»), справочник — по имени элемента. `null` сбрасывает значение.

## Деплой на Dockhost

Как прайс: Dockhost собирает `Dockerfile` из корня репозитория. Порт `8080`, переменные
из `.env.example`, постоянный диск — в `/data`, реплика одна. Пошагово, с проверкой после
релиза — скил [deploy-hub](.claude/skills/deploy-hub/SKILL.md).

## Документация МойСклад

[`docs/moysklad-api/`](docs/moysklad-api) — копия официальной документации JSON API 1.2.
Это справочник, а не наш код: в образ не попадает. Поиск по ней:
`python3 .claude/skills/moysklad-api/scripts/docs.py отгрузк печать`, обновление:
`python3 .claude/skills/moysklad-api/scripts/update_docs.py`.

У МойСклад есть и официальный MCP-сервер документации:
`https://mcp.moysklad.ru/doc/api/remap/1.2` (без авторизации, только читает документацию).

## Разработка

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q && ruff check .
HUB_TEST_DATABASE_URL=postgresql://hub:hub@localhost:5432/hub python3 -m pytest -q   # то же на PostgreSQL
```

Тесты идут на поддельном МойСклад (`tests/fake_moysklad.py`) с формами ответов из
документации. CI в `.github/workflows/ci.yml` гоняет оба варианта и собирает образ.
Новая автоматизация — скил [new-automation](.claude/skills/new-automation/SKILL.md).
