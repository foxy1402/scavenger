import asyncio
import json
import os
from datetime import datetime, timezone


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
                for line in f:
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
                    self.items.append(item)
                    self.seen.add(key)
        except OSError:
            pass

    async def add(self, item):
        key = item_key(item)
        async with self.lock:
            if key in self.seen:
                return False
            self.seen.add(key)
            self.items.append(item)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        return True

    async def replace(self, items):
        async with self.lock:
            self.items = list(items)
            self.seen = {item_key(i) for i in self.items}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for item in self.items:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)

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
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for item in kept:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
            return removed

    async def all_items(self):
        async with self.lock:
            return list(self.items)
