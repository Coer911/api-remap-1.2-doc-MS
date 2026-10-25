import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_moysklad import FakeMoySklad  # noqa: E402

from hub.channels import ChannelError, Channels  # noqa: E402
from hub.moysklad import MoySklad  # noqa: E402
from hub.runtime import Hub  # noqa: E402
from hub.settings import Settings  # noqa: E402
from hub.storage import Storage  # noqa: E402

# HUB_TEST_DATABASE_URL=postgresql://… — прогнать те же тесты на PostgreSQL
DB_URL = os.environ.get("HUB_TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")


class RecordingChannels(Channels):
    """Каналы без сети: записывают отправки, умеют падать заданное число раз."""

    def __init__(self, settings):
        super().__init__(settings)
        self.emails, self.documents, self.messages, self.hooks, self.alerts = [], [], [], [], []
        self.fail = {}

    def _maybe_fail(self, kind):
        if self.fail.get(kind, 0) > 0:
            self.fail[kind] -= 1
            raise ChannelError(f"{kind} недоступен")

    async def email(self, to, subject, body, attachments=(), cc=()):
        if self.s.dry_run:
            return await super().email(to, subject, body, attachments, cc)
        self._maybe_fail("email")
        self.emails.append({"to": to, "subject": subject, "body": body,
                            "attachments": list(attachments), "cc": list(cc)})
        return f"письмо {', '.join(to)}"

    async def telegram_document(self, chat, filename, data, caption=""):
        if self.s.dry_run:
            return await super().telegram_document(chat, filename, data, caption)
        self._maybe_fail("telegram")
        self.documents.append({"chat": chat, "filename": filename, "caption": caption})
        return f"telegram {chat}"

    async def telegram_message(self, chat, text):
        if self.s.dry_run:
            return await super().telegram_message(chat, text)
        self._maybe_fail("telegram")
        self.messages.append({"chat": chat, "text": text})
        return f"telegram {chat}"

    async def webhook(self, url, payload):
        if self.s.dry_run:
            return await super().webhook(url, payload)
        self._maybe_fail("webhook")
        self.hooks.append({"url": url, "payload": payload})
        return f"вебхук {url}"

    async def alert(self, text):
        self.alerts.append(text)


def settings_for(automations: dict, dry_run: bool = False) -> Settings:
    return Settings(ms_token="test-token", api_key="k", webhook_secret="s3cret",
                    public_url="https://hub.example.ru", poll_seconds=0, dry_run=dry_run,
                    database_url=DB_URL, automations=automations)


async def fresh_storage() -> Storage:
    storage = await Storage.open(DB_URL)
    if not storage.is_sqlite:                      # общая база PostgreSQL — чистим перед тестом
        from hub.storage import metadata
        async with storage.engine.begin() as conn:
            for table in reversed(metadata.sorted_tables):
                await conn.execute(table.delete())
    return storage


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    """Поддельный МойСклад не ограничивает частоту — тестам не нужно ждать окно в 3 с."""
    monkeypatch.setattr(MoySklad, "budget", property(lambda self: self._fixed_budget or 45))


@pytest.fixture
def fake():
    return FakeMoySklad()


@pytest.fixture
async def make_hub(fake):
    """await make_hub({"имя": {параметры}}, dry_run=False, storage=None) -> Hub."""
    hubs = []

    async def build(automations: dict, dry_run: bool = False, storage: Storage | None = None) -> Hub:
        s = settings_for(automations, dry_run)
        hub = Hub(s, storage or await fresh_storage(),
                  ms=MoySklad("test-token", transport=fake.transport()),
                  channels=RecordingChannels(s))
        hubs.append(hub)
        return hub

    yield build
    closed = set()
    for hub in hubs:
        await hub.ms.close()
        if id(hub.storage) not in closed:
            closed.add(id(hub.storage))
            await hub.storage.close()


async def webhook(hub: Hub, fake: FakeMoySklad, type_: str, id_: str, rid: str,
                  action: str = "UPDATE", fields: list[str] | None = None) -> int:
    from hub.ingest import accept_webhook
    return await accept_webhook(hub.storage, fake.webhook_payload(type_, id_, action, fields), rid)
