"""Командная строка хаба: python -m hub <команда>.

  serve                     — веб-сервер + воркер + опрос (так запускается в Docker)
  check                     — проверить токен, config.yaml, шаблон, статус, доп. поле
  states | templates        — справочники отгрузок: статусы и шаблоны печати
  setup                     — создать доп. поля-отметки, которых не хватает
  webhooks list|install|remove — вебхуки МойСклад на отгрузки
  poll                      — один круг: опрос + обработка очереди (вместо serve, по cron)
  run <автоматизация> <номер|id> [--force]  — запустить вручную
  journal [-n 20] [--status failed]         — журнал запусков
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx

from .automations import ConfigError
from .models import Event, Shipment
from .moysklad import MoySklad, MoySkladError
from .settings import Settings

HOOK_ACTIONS = (("CREATE", None), ("UPDATE", "FIELDS"), ("DELETE", None))


def ok(text: str) -> None:
    print(f"  ✅ {text}")


def bad(text: str) -> None:
    print(f"  ❌ {text}")


def warn(text: str) -> None:
    print(f"  ⚠️  {text}")


async def cmd_check(s: Settings) -> int:
    from .automations import enabled
    errors = 0
    print("Настройки")
    if s.dry_run:
        warn("HUB_DRY_RUN: отправки выключены, только журнал")
    else:
        ok("боевой режим: отправки включены")
    try:
        autos = enabled(s.automations)
        ok("config.yaml: " + (", ".join(a.name for a in autos) or "автоматизаций не включено"))
    except ConfigError as e:
        bad(str(e))
        return 1
    for name, value, need in (("HUB_API_KEY", s.api_key, "REST API выключен"),
                              ("HUB_WEBHOOK_SECRET", s.webhook_secret, "вебхуки не принимаются"),
                              ("HUB_PUBLIC_URL", s.public_url, "вебхуки не установить")):
        if value:
            ok(f"{name} задан")
        else:
            warn(f"{name} не задан — {need}")
    print("МойСклад")
    async with MoySklad(s.ms_token, s.ms_base) as ms:
        try:
            me = await ms.me()
            ok(f"токен работает: {me.get('name') or me.get('uid')}")
        except MoySkladError as e:
            bad(f"токен: {e}")
            return 1
        states = {x["name"] for x in await ms.states()}
        attrs = {x["name"] for x in (await ms.get("entity/demand/metadata/attributes")).get("rows", [])}
        for a in autos:
            p = a.params
            if p.get("state"):
                (ok if p["state"] in states else bad)(
                    f"{a.name}: статус «{p['state']}»" + ("" if p["state"] in states
                                                          else f" — есть: {', '.join(sorted(states))}"))
                errors += p["state"] not in states
            if p.get("template"):
                try:
                    tpl = await ms.find_template(p["template"])
                    ok(f"{a.name}: шаблон «{tpl['name']}»")
                except MoySkladError as e:
                    bad(f"{a.name}: {e.message}")
                    errors += 1
            if p.get("mark_attribute"):
                has = p["mark_attribute"] in attrs
                (ok if has else bad)(f"{a.name}: доп. поле «{p['mark_attribute']}»"
                                     + ("" if has else " — создайте: python -m hub setup"))
                errors += not has
        hooks = [h for h in await ms.webhooks() if h.get("entityType") == "demand"]
        if s.webhook_url and any(h["url"] == s.webhook_url for h in hooks):
            ok(f"вебхуки на отгрузки ведут сюда ({len(hooks)})")
        else:
            warn("вебхуков на хаб нет — работает только опрос (python -m hub webhooks install)")
    print("Каналы")
    if s.smtp_host:
        ok(f"почта: {s.smtp_user or s.smtp_from} через {s.smtp_host}:{s.smtp_port}")
    else:
        warn("почта не настроена (SMTP_HOST)")
    if s.telegram_token:
        ok("Telegram: токен задан" + (", алерты в " + s.alert_chat if s.alert_chat else ""))
    else:
        warn("Telegram не настроен (TELEGRAM_BOT_TOKEN)")
    print("\nГотово к работе" if not errors else f"\nПроблем: {errors}")
    return 1 if errors else 0


async def cmd_states(s: Settings) -> int:
    async with MoySklad(s.ms_token, s.ms_base) as ms:
        for st in await ms.states():
            print(f"  {st['name']}")
    return 0


async def cmd_templates(s: Settings) -> int:
    async with MoySklad(s.ms_token, s.ms_base) as ms:
        for t in await ms.templates():
            print(f"  [{t['meta']['type']}] {t['name']}")
    return 0


async def cmd_setup(s: Settings) -> int:
    from .automations import enabled
    names = {a.params["mark_attribute"] for a in enabled(s.automations)
             if a.params.get("mark_attribute")}
    async with MoySklad(s.ms_token, s.ms_base) as ms:
        for name in sorted(names):
            if await ms.attribute(name):
                ok(f"доп. поле «{name}» уже есть")
            else:
                await ms.create_attribute(name)
                ok(f"создано доп. поле отгрузок «{name}» (строка)")
    return 0


async def cmd_webhooks(s: Settings, action: str) -> int:
    async with MoySklad(s.ms_token, s.ms_base) as ms:
        hooks = [h for h in await ms.webhooks() if h.get("entityType") == "demand"]
        if action == "list":
            for h in hooks:
                mine = " ← хаб" if h["url"] == s.webhook_url else ""
                print(f"  {h['action']:<7} {'on ' if h.get('enabled') else 'off'} {h['url']}{mine}")
            return 0
        if not s.webhook_url:
            bad("нужны HUB_PUBLIC_URL и HUB_WEBHOOK_SECRET")
            return 1
        if action == "install":
            have = {h["action"] for h in hooks if h["url"] == s.webhook_url}
            for act, diff in HOOK_ACTIONS:
                if act in have:
                    ok(f"{act} уже стоит")
                    continue
                await ms.create_webhook(s.webhook_url, "demand", act, diff)
                ok(f"{act} → {s.public_url}/hooks/moysklad/***")
        elif action == "remove":
            for h in hooks:
                if h["url"] == s.webhook_url:
                    await ms.delete_webhook(h["id"])
                    ok(f"снят {h['action']}")
    return 0


async def cmd_poll(s: Settings) -> int:
    from .runtime import Hub
    hub = Hub(s)
    try:
        hub.store.recover()
        added = await hub.poll_once()
        outcomes = await hub.tick()
        print(f"новых событий: {added}")
        for o in outcomes:
            print(f"  {o.automation} {o.shipment}: {o.status} {o.detail}")
    finally:
        await hub.ms.close()
        await hub.channels.close()
        hub.store.close()
    return 0


async def cmd_run(s: Settings, name: str, ref: str, force: bool) -> int:
    from .runtime import Hub
    hub = Hub(s)
    try:
        a = hub.engine.by_name.get(name)
        if not a:
            bad(f"автоматизация {name} не включена в config.yaml")
            return 1
        raw = await hub.ms.demand(ref) if len(ref) == 36 else await hub.ms.demand_by_number(ref)
        if not raw:
            bad(f"отгрузка {ref} не найдена")
            return 1
        sh = Shipment.from_api(raw)
        print(f"Отгрузка {sh.number} от {sh.date} · {sh.agent.name} · {sh.sum_text} ₽ · {sh.state}")
        if not force and not a.wants(sh, Event(sh.id, "MANUAL", source="cli")):
            bad("условия автоматизации не выполнены (статус, юрлицо, дата) — --force, чтобы всё равно")
            return 1
        o = await hub.engine.execute(a, sh, mode="force" if force else "normal")
        print(f"  {o.status}: {o.detail}")
        for item in hub.channels.sent:
            print(f"  → {item}")
        return 0 if o.status in ("done", "already", "skipped") else 1
    finally:
        await hub.ms.close()
        await hub.channels.close()
        hub.store.close()


def cmd_journal(s: Settings, n: int, status: str) -> int:
    from .store import Store
    store = Store(s.db_path)
    try:
        for r in store.journal(n, status=status):
            tail = r.error if r.status in ("failed", "dead") else r.result
            print(f"  {r.updated_at} {r.automation:<12} {r.number or r.entity_id:<10} {r.status:<8}"
                  f" {tail or ''}")
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m hub", description="Хаб отгрузок МойСклад")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("check")
    sub.add_parser("states")
    sub.add_parser("templates")
    sub.add_parser("setup")
    w = sub.add_parser("webhooks")
    w.add_argument("action", choices=["list", "install", "remove"])
    sub.add_parser("poll")
    r = sub.add_parser("run")
    r.add_argument("automation")
    r.add_argument("ref", help="номер отгрузки (как в МойСклад) или её id")
    r.add_argument("--force", action="store_true", help="без проверки условий и журнала")
    j = sub.add_parser("journal")
    j.add_argument("-n", type=int, default=20)
    j.add_argument("--status", default="")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings.from_env()
    if args.cmd == "serve":
        import uvicorn

        from .app import create_app
        uvicorn.run(create_app(settings=s), host="0.0.0.0", port=s.port, proxy_headers=True)
        return 0
    if args.cmd == "journal":
        return cmd_journal(s, args.n, args.status)
    if not s.ms_token:
        bad("не задан MOYSKLAD_TOKEN")
        return 1
    commands = {
        "check": lambda: cmd_check(s),
        "states": lambda: cmd_states(s),
        "templates": lambda: cmd_templates(s),
        "setup": lambda: cmd_setup(s),
        "webhooks": lambda: cmd_webhooks(s, args.action),
        "poll": lambda: cmd_poll(s),
        "run": lambda: cmd_run(s, args.automation, args.ref, args.force),
    }
    try:
        return asyncio.run(commands[args.cmd]())
    except ConfigError as e:
        bad(f"config.yaml: {e}")
    except MoySkladError as e:
        bad(str(e))
    except httpx.TransportError as e:
        bad(f"нет связи с МойСклад ({s.ms_base}): {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
