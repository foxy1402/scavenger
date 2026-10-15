import asyncio
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("scavenger.store")


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def item_key(item):
    return (item.get("type"), item.get("ip"), item.get("port"), item.get("model", ""))


class Store:
    """JSONL-backed catch store with in-memory dedupe."""

    def __init__(self, path):
        self.path = path
        self.lock = asyncio.Lock()
        self.seen = set()
        self.items = []
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return
        parsed = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            key = item_key(item)
            if key in self.seen:
                continue
            parsed.append(item)
            self.seen.add(key)
        self.items = parsed
        if lines and not lines[-1].endswith("\n"):
            # a crash/disk-full mid-append leaves a torn tail; rewriting now
            # stops the next append from welding onto it (losing both lines)
            log.warning("repairing torn final line in %s", self.path)
            self._rewrite_locked()

    def _rewrite_locked(self):
        """Atomic rewrite from self.items; caller holds the lock (or is
        still in __init__, single-threaded)."""
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for item in self.items:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("store rewrite failed: %s", e)

    async def add(self, item):
        key = item_key(item)
        async with self.lock:
            if key in self.seen:
                return False
            self.seen.add(key)
            self.items.append(item)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        return True

    async def replace(self, items):
        async with self.lock:
            self.items = list(items)
            self.seen = {item_key(i) for i in self.items}
            self._rewrite_locked()

    async def remove_keys(self, keys):
        """Drop items whose key is in `keys` from the CURRENT in-memory list,
        so catches added between the snapshot and this call survive."""
        if not keys:
            return 0
        async with self.lock:
            kept = [i for i in self.items if item_key(i) not in keys]
            removed = len(self.items) - len(kept)
            if not removed:
                return 0
            self.items = kept
            self.seen = {item_key(i) for i in kept}
            self._rewrite_locked()
            return removed

    async def all_items(self):
        async with self.lock:
            return list(self.items)
