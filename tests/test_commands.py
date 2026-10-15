"""Offline end-to-end test: probes, store dedupe, /export, /revalidate.

Runs against the in-process mocks, no network or Telegram token needed.
Run inside the image:  docker run --rm scavenger python -m tests.test_commands
"""
import asyncio
import os
import sys
import tempfile

import aiohttp

from app import selftest
from app.config import Config
from app.probes import probe_http, probe_llm, probe_socks5
from app.stats import Stats
from app.store import Store, utcnow_iso
from app.telegram_bot import TelegramBot


class FakeTransport:
    def __init__(self):
        self.messages = []
        self.documents = []

    async def get_updates(self, offset, timeout=50):
        return {"result": []}

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id, data, filename):
        self.documents.append((chat_id, data, filename))


_failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"PASS {name}", flush=True)
    else:
        _failures.append(name)
        print(f"FAIL {name} {detail}", flush=True)


async def run():
    tmp = tempfile.mkdtemp(prefix="scav-test-")
    os.environ["DATA_DIR"] = tmp
    os.environ["TELEGRAM_BOT_TOKEN"] = "123:test"
    os.environ["TELEGRAM_CHAT_ID"] = "42"
    cfg = Config()
    store = Store(os.path.join(tmp, "catches.jsonl"))
    stats = Stats()
    bot = TelegramBot(cfg, store, [], stats)
    bot.transport = FakeTransport()
    session = aiohttp.ClientSession()
    bot.session = session
    cleanup = await selftest.start_mocks()
    try:
        # 1. probes against live mocks
        s = await probe_socks5(session, "127.0.0.1", selftest.SOCKS5_PORT, cfg)
        check("probe_socks5", s is not None and s.get("egress") == selftest.MOCK_EGRESS, repr(s))
        h = await probe_http(session, "127.0.0.1", selftest.HTTP_PROXY_PORT, cfg)
        check("probe_http", h is not None and h.get("egress") == selftest.MOCK_EGRESS, repr(h))
        l = await probe_llm(session, "127.0.0.1", selftest.LLM_PORT, cfg)
        check("probe_llm", l is not None and l.get("model") == "mock-llm-1", repr(l))

        # 2. probes against a closed port
        d = await probe_socks5(session, "127.0.0.1", 11089, cfg)
        check("probe_socks5_dead", d is None, repr(d))
        dl = await probe_llm(session, "127.0.0.1", 11089, cfg)
        check("probe_llm_dead", dl is None, repr(dl))

        # 3. store: add + dedupe
        now = utcnow_iso()
        items = [
            {"type": "socks5", "ip": "127.0.0.1", "port": selftest.SOCKS5_PORT,
             "provider": "selftest", "source": "selftest",
             "egress": selftest.MOCK_EGRESS, "captured_at": now},
            {"type": "http_proxy", "ip": "127.0.0.1", "port": selftest.HTTP_PROXY_PORT,
             "provider": "selftest", "source": "selftest",
             "egress": selftest.MOCK_EGRESS, "captured_at": now},
            {"type": "llm", "ip": "127.0.0.1", "port": selftest.LLM_PORT,
             "provider": "selftest", "source": "selftest",
             "model": "mock-llm-1", "reply": "Hello! I am the mock model.",
             "captured_at": now},
            {"type": "llm", "ip": "127.0.0.1", "port": 11089,
             "provider": "selftest", "source": "selftest",
             "model": "mock-dead", "reply": "x", "captured_at": now},
        ]
        for i in items:
            check("store_add", await store.add(i))
        check("store_dedupe", not await store.add(dict(items[0])))

        # 4. /export
        await bot.cmd_export(42)
        texts = [t for _, t in bot.transport.messages]
        check("export_lists",
              any("scavenger export" in t and "mock-llm-1" in t and
                  "socks5://127.0.0.1" in t for t in texts),
              repr(texts))

        # 5. /revalidate drops the dead entry, keeps the live ones
        await bot.cmd_revalidate(42)
        remaining = await store.all_items()
        check("revalidate_kept", len(remaining) == 3, str(len(remaining)))
        check("revalidate_removed_dead",
              all(i["port"] != 11089 for i in remaining), repr(remaining))
        summaries = [t for _, t in bot.transport.messages if "alive" in t]
        check("revalidate_summary",
              any("3 alive" in t and "1 removed" in t for t in summaries),
              repr(summaries))

        # 6. file persistence across restarts
        store2 = Store(store.path)
        check("store_reload", len(await store2.all_items()) == 3)
        check("store_reload_dedupe",
              not await store2.add(dict(items[1])))
    finally:
        await session.close()
        await cleanup()

    print("RESULT:", "PASS" if not _failures else f"FAIL {_failures}", flush=True)
    return 1 if _failures else 0


def main():
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
