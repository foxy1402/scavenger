import asyncio
import logging
import os
import signal
import sys
import time

import aiohttp
from aiohttp import web

from app.config import Config
from app.ranges import CloudRangePool, ResidentialPool
from app.scanners import ScannerGroup
from app.stats import Stats
from app.store import Store
from app.telegram_bot import RealTransport, TelegramBot
from app.web import make_app

log = logging.getLogger("scavenger")

_selftest_ok = False


async def _selftest_watchdog(stats, stop, timeout_s):
    global _selftest_ok
    deadline = time.monotonic() + timeout_s
    while not stop.is_set():
        total = sum(stats.catches.values())
        if total >= 3:
            _selftest_ok = True
            log.info("SELFTEST OK 3/3")
            stop.set()
            return
        if time.monotonic() > deadline:
            log.info("SELFTEST FAIL %d/3", total)
            stop.set()
            return
        await asyncio.sleep(0.2)


async def amain():
    global _selftest_ok
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config()
    os.makedirs(cfg.data_dir, exist_ok=True)
    store = Store(os.path.join(cfg.data_dir, "catches.jsonl"))
    if store.items:
        log.info("loaded %d existing catches from %s", len(store.items), store.path)
    stats = Stats()
    log.info("scavenger starting | ports=%s providers=%s cloud_workers=%d "
             "res_workers=%d concurrency=%d data_dir=%s selftest=%s telegram=%s",
             ",".join(map(str, cfg.scan_ports)), ",".join(cfg.providers),
             cfg.cloud_scanners, cfg.residential_scanners, cfg.check_concurrency,
             cfg.data_dir, cfg.selftest,
             "on" if cfg.telegram_token else "off")

    connector = aiohttp.TCPConnector(limit=400, ttl_dns_cache=600)
    session = aiohttp.ClientSession(connector=connector)
    bot = TelegramBot(cfg, store, [], stats)
    bot.session = session
    if cfg.telegram_token:
        bot.transport = RealTransport(session, cfg.telegram_token)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    web_runner = web.AppRunner(make_app(cfg))
    await web_runner.setup()
    await web.TCPSite(web_runner, "0.0.0.0", cfg.port).start()
    log.info("web up on 0.0.0.0:%d", cfg.port)

    cleanup = None
    tasks = []
    if cfg.selftest:
        from app import selftest
        cfg.scan_ports = selftest.mock_ports()
        cfg.sample_per_24 = 1
        cleanup = await selftest.start_mocks()
        log.info("selftest mocks up on 127.0.0.1:%d:%d:%d",
                 selftest.SOCKS5_PORT, selftest.HTTP_PROXY_PORT, selftest.LLM_PORT)
        group = ScannerGroup("selftest", cfg, selftest.MockPool(),
                             store, bot, stats, workers=1)
        bot.groups = [group]
        tasks.append(asyncio.create_task(group.run(session, stop)))
        tasks.append(asyncio.create_task(_selftest_watchdog(stats, stop, 90)))
    else:
        cloud_pool = CloudRangePool(cfg.providers)
        log.info("fetching cloud IP feeds…")
        for attempt in range(5):
            try:
                sizes = await cloud_pool.refresh(session)
                stats.feed_sizes = sizes
                if any(sizes.values()):
                    break
            except Exception as e:
                log.warning("initial feed refresh failed (attempt %d): %s",
                            attempt + 1, e)
            await asyncio.sleep(5)
        groups = [
            ScannerGroup("cloud", cfg, cloud_pool, store, bot, stats,
                         cfg.cloud_scanners),
            ScannerGroup("residential", cfg, ResidentialPool(), store, bot, stats,
                         cfg.residential_scanners),
        ]
        bot.groups = groups
        for g in groups:
            tasks.append(asyncio.create_task(g.run(session, stop)))

    bot_task = asyncio.create_task(bot.start(stop))

    await stop.wait()
    log.info("shutting down…")
    bot_task.cancel()
    await asyncio.gather(*tasks, bot_task, return_exceptions=True)
    try:
        await bot.flush_pending()
        await bot.send_backup()
    except Exception as e:
        log.warning("shutdown telegram backup failed: %s", e)
    await web_runner.cleanup()
    await session.close()
    if cleanup is not None:
        await cleanup()
    log.info("bye")
    if cfg.selftest and not _selftest_ok:
        sys.exit(1)


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
