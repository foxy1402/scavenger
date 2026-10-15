"""Offline end-to-end test: probes, store dedupe, /export, /revalidate.

Runs against the in-process mocks, no network or Telegram token needed.
Run inside the image:  docker run --rm scavenger python -m tests.test_commands
"""
import asyncio
import json
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
        self.updates = []

    async def get_updates(self, offset, timeout=50):
        ups, self.updates = self.updates, []
        return {"ok": True, "result": ups}

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
        try:
            r = await probe_llm(session, "127.0.0.1", 11092, cfg)
        finally:
            await root_runner.cleanup()
        check("probe_llm_root",
              r is not None and r.get("model") == "root-model"
              and r.get("api_base") == "http://127.0.0.1:11092",
              repr(r))

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
        if bot._reval_task is not None:
            await bot._reval_task  # revalidation runs as a background task
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

        # 7. /llmtest model list is well-formed (8 providers, flagship first)
        from app import llm_models
        ok_providers = all(
            info.get("models") and info.get("flagship") in info["models"]
            and info["models"][0] == info["flagship"]
            for info in llm_models.PROVIDERS.values())
        check("llm_models_list",
              len(llm_models.PROVIDERS) == 8 and ok_providers
              and llm_models.candidate_models("openai")[0] == "gpt-6-astra"
              and llm_models.candidate_models("nope") == [])

        # 8. torn final JSONL line is repaired, not welded onto
        torn_path = os.path.join(tmp, "torn.jsonl")
        with open(torn_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(items[0]) + "\n")
            f.write('{"type": "socks5"')  # torn by a crash mid-append
        store3 = Store(torn_path)
        check("store_torn_line_healed", len(store3.items) == 1,
              str(len(store3.items)))
        await store3.add(dict(items[1]))
        store4 = Store(torn_path)
        check("store_torn_reload", len(await store4.all_items()) == 2)

        # 9. /start from a foreign chat must not hijack a bound bot
        bot.transport.updates = [{"update_id": 5,
                                  "message": {"chat": {"id": 999},
                                              "text": "/start"}}]
        await bot.poll_once()
        check("start_guard", bot.chat_id == 42, repr(bot.chat_id))
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
