import asyncio
import ipaddress
import logging
import random

from app.probes import probe_http, probe_llm, probe_socks5
from app.ranges import CloudRangePool
from app.store import utcnow_iso

log = logging.getLogger("scavenger.scanners")

PROBE_TYPES = (("socks5", probe_socks5), ("http_proxy", probe_http), ("llm", probe_llm))


class ScannerGroup:
    """A pool of worker loops pulling /24 units from a pool and probing them."""

    def __init__(self, name, cfg, pool, store, bot, stats, workers):
        self.name = name
        self.cfg = cfg
        self.pool = pool
        self.store = store
        self.bot = bot
        self.stats = stats
        self.workers = workers
        self.sem = asyncio.Semaphore(cfg.check_concurrency)
        self.units_done = 0
        self.checks_done = 0
        self.catches = 0

    async def run(self, session, stop):
        tasks = [asyncio.create_task(self._worker(session, stop),
                                     name=f"{self.name}-worker-{i}")
                 for i in range(self.workers)]
        refresher = (asyncio.create_task(self._refresher(session, stop))
                     if isinstance(self.pool, CloudRangePool) else None)
        all_tasks = tasks + ([refresher] if refresher else [])
        try:
            await stop.wait()
        finally:
            for t in all_tasks:
                t.cancel()
            await asyncio.gather(*all_tasks, return_exceptions=True)

    async def _refresher(self, session, stop):
        while True:
            interval = self.cfg.feed_refresh_hours * 3600
            if not self.pool.ready or not any(self.pool.lists.values()):
                interval = min(interval, 300)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                sizes = await self.pool.refresh(session)
                self.stats.feed_sizes = sizes
            except Exception as e:
                log.warning("%s feed refresh failed: %s", self.name, e)

    async def _worker(self, session, stop):
        while not stop.is_set():
            provider, cidr24 = await self.pool.next_unit()
            if cidr24 is None:
                await asyncio.sleep(5)
                continue
            try:
                await self._scan_unit(session, provider, cidr24)
            except Exception as e:
                log.debug("%s unit %s failed: %s", self.name, cidr24, e)
            self.units_done += 1
            if self.units_done % 100 == 0:
                log.info("%s: units=%d checks=%d catches=%d",
                         self.name, self.units_done, self.checks_done, self.catches)

    async def _scan_unit(self, session, provider, cidr24):
        net = ipaddress.ip_network(cidr24)
        count = net.num_addresses
        k = min(self.cfg.sample_per_24, count)
        offsets = random.sample(range(count), k) if k < count else [0]
        tasks = []
        for off in offsets:
            ip = str(net.network_address + off)
            for port in self.cfg.scan_ports:
                tasks.append(self._check(session, ip, port, provider))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check(self, session, ip, port, provider):
        async with self.sem:
            self.checks_done += 1
            try:
                results = await asyncio.gather(
                    *(fn(session, ip, port, self.cfg) for _, fn in PROBE_TYPES),
                    return_exceptions=True)
            except Exception:
                return
        # record outside the semaphore: the Telegram send must not hold a
        # probe slot
        for (ptype, _), res in zip(PROBE_TYPES, results):
            if isinstance(res, dict):
                await self._record(ptype, ip, port, provider, res)

    async def _record(self, ptype, ip, port, provider, res):
        item = {
            "type": ptype,
            "ip": ip,
            "port": port,
            "provider": provider or "-",
            "source": self.name,
            "captured_at": utcnow_iso(),
        }
        item.update(res)
        if await self.store.add(item):
            self.catches += 1
            self.stats.record(ptype)
            log.info("CATCH %s %s:%d (%s/%s)",
                     ptype, ip, port, self.name, provider or "-")
            await self.bot.send_catch(item)
