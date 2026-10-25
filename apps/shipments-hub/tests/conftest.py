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
from hub.store import Store  # noqa: E402


class RecordingChannels(Channels):
    """Каналы без сети: записывают отправки, умеют падать заданное число раз."""

    def __init__(self, settings):
        super().__init__(settings)
        self.emails, self.documents, self.hooks, self.alerts = [], [], [], []
        self.fail = {}                      # канал -> сколько раз ещё упасть

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

    async def webhook(self, url, payload):
        if self.s.dry_run:
            return await super().webhook(url, payload)
        self._maybe_fail("webhook")
        self.hooks.append({"url": url, "payload": payload})
        return f"вебхук {url}"

    async def alert(self, text):
        self.alerts.append(text)


def make_settings(**automation) -> Settings:
    params = {"enabled": True, "state": "Отгружен", **automation}
    return Settings(ms_token="test-token", api_key="k", webhook_secret="s3cret",
                    public_url="https://hub.example.ru", poll_seconds=0, dry_run=False,
                    automations={"send_upd": params})


@pytest.fixture
def fake():
    return FakeMoySklad()


@pytest.fixture
def make_hub(fake):
    """make_hub(dry_run=False, store=None, **параметры send_upd) -> Hub на поддельном МойСклад."""
    stores = []

    def build(dry_run=False, store=None, **params):
        s = make_settings(**params)
        s.dry_run = dry_run
        ms = MoySklad("test-token", transport=fake.transport())
        store = store or Store(":memory:")
        stores.append(store)
        return Hub(s, ms=ms, store=store, channels=RecordingChannels(s))
    yield build
    for store in stores:
        store.close()
