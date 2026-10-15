"""Offline end-to-end test: probes, store dedupe, /export, /revalidate.

Runs against the in-process mocks, no network or Telegram token needed.
Run inside the image:  docker run --rm scavenger python -m tests.test_commands
"""
import asyncio
import os
import sys
import tempfile

import aiohttp
from aiohttp import web

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
    os.environ["PROBE_TIMEOUT"] = "2"  # keep the negative TLS cases snappy
    cfg = Config()
    store = Store(os.path.join(tmp, "catches.jsonl"))
    stats = Stats()
    bot = TelegramBot(cfg, store, [], stats)
    bot.transport = FakeTransport()
    session = aiohttp.ClientSession()
    bot.session = session
    cleanup = await selftest.start_mocks()
    plain_cleanup = await selftest.start_plain_mocks()
    try:
        # 1. probes against live mocks (CONNECT + TLS must succeed)
        s = await probe_socks5(session, "127.0.0.1", selftest.SOCKS5_PORT, cfg)
        check("probe_socks5", s is not None and s.get("egress") == selftest.MOCK_EGRESS, repr(s))
        h = await probe_http(session, "127.0.0.1", selftest.HTTP_PROXY_PORT, cfg)
        check("probe_http", h is not None and h.get("egress") == selftest.MOCK_EGRESS, repr(h))

        # 1b. plain-HTTP-only proxies (no CONNECT tunnel / no TLS) are rejected
        ps = await probe_socks5(session, "127.0.0.1", selftest.PLAIN_SOCKS5_PORT, cfg)
        check("probe_socks5_plain_only_rejected", ps is None, repr(ps))
        ph = await probe_http(session, "127.0.0.1", selftest.PLAIN_HTTP_PROXY_PORT, cfg)
        check("probe_http_plain_only_rejected", ph is None, repr(ph))
        l = await probe_llm(session, "127.0.0.1", selftest.LLM_PORT, cfg)
        check("probe_llm",
              l is not None and l.get("model") == "mock-llm-1"
              and l.get("api_base") == f"http://127.0.0.1:{selftest.LLM_PORT}/v1",
              repr(l))

        # root-path gateway (no /v1 prefix): caught via the bare-base fallback
        root_app = web.Application()

        async def _root_models(request):
            return web.json_response({"data": [{"id": "root-model"}]})

        async def _root_chat(request):
            return web.json_response({"choices": [{"message": {"role": "assistant",
                                                               "content": "root reply"}}]})

        root_app.router.add_get("/models", _root_models)
        root_app.router.add_post("/chat/completions", _root_chat)
        root_runner = web.AppRunner(root_app)
        await root_runner.setup()
        await web.TCPSite(root_runner, "127.0.0.1", 11092).start()
        r = await probe_llm(session, "127.0.0.1", 11092, cfg)
        check("probe_llm_root",
              r is not None and r.get("model") == "root-model"
              and r.get("api_base") == "http://127.0.0.1:11092",
              repr(r))
        await root_runner.cleanup()

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
             "api_base": f"http://127.0.0.1:{selftest.LLM_PORT}/v1",
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
              any("scavenger export" in t and "mock-llm-1" in t
                  and "socks5://127.0.0.1" in t
                  and f"http://127.0.0.1:{selftest.LLM_PORT}/v1" in t
                  for t in texts),
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
        await plain_cleanup()

    print("RESULT:", "PASS" if not _failures else f"FAIL {_failures}", flush=True)
    return 1 if _failures else 0


def main():
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
