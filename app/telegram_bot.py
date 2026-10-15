import asyncio
import io
import logging
import time

import aiohttp

from app.probes import probe_http, probe_llm, probe_socks5
from app.store import item_key, utcnow_iso

log = logging.getLogger("scavenger.telegram")

MAX_LEN = 4000
EXPORT_TEXT_LIMIT = 3500


def format_catch(item):
    where = f"{item.get('source', '?')}/{item.get('provider', '-')}"
    stamp = item.get("captured_at", "")
    t = item.get("type", "")
    if t == "socks5":
        lines = ["🔓 CATCH — SOCKS5 PROXY",
                 f"addr     {item.get('ip')}:{item.get('port')}",
                 f"egress   {item.get('egress') or '?'}",
                 f"where    {where}",
                 f"captured {stamp}"]
    elif t == "http_proxy":
        lines = ["🔓 CATCH — HTTP PROXY",
                 f"addr     {item.get('ip')}:{item.get('port')}",
                 f"egress   {item.get('egress') or '?'}",
                 f"where    {where}",
                 f"captured {stamp}"]
    else:
        scheme = item.get("scheme", "http")
        addr = item.get("api_base") or f"{scheme}://{item.get('ip')}:{item.get('port')}"
        lines = ["🤖 CATCH — LLM GATEWAY",
                 f"addr     {addr}",
                 f"model    {item.get('model', '?')}",
                 f"reply    {item.get('reply', '')[:150]}"]
        if item.get("status") and item["status"] != "ok":
            lines.append(f"status   {item['status']}")
        lines += [f"where    {where}", f"captured {stamp}"]
    return "\n".join(lines)


def format_export(items):
    groups = (("socks5", "SOCKS5"), ("http_proxy", "HTTP PROXY"), ("llm", "LLM GATEWAY"))
    parts = [f"scavenger export — {utcnow_iso()} — {len(items)} catch(es)", ""]
    for key, label in groups:
        rows = [i for i in items if i.get("type") == key]
        if not rows:
            continue
        parts.append(f"[{label}] {len(rows)}")
        for i in rows:
            where = f"{i.get('source', '?')}/{i.get('provider', '-')}"
            stamp = i.get("captured_at", "")
            if key == "llm":
                status = f"  [{i['status']}]" if i.get("status") and i["status"] != "ok" else ""
                addr = (i.get("api_base")
                        or f"{i.get('scheme', 'http')}://{i.get('ip')}:{i.get('port')}")
                parts.append(f"{addr}  model={i.get('model', '?')}{status}  ({where})  {stamp}")
            else:
                scheme = "socks5" if key == "socks5" else "http"
                parts.append(f"{scheme}://{i.get('ip')}:{i.get('port')}"
                             f"  egress={i.get('egress') or '?'}  ({where})  {stamp}")
        parts.append("")
    return "\n".join(parts).strip()


def _short(item):
    if item.get("type") == "llm":
        return f"  {item.get('ip')}:{item.get('port')} ({item.get('model', '?')})"
    return f"  {item.get('ip')}:{item.get('port')}"


def _chunk(text, limit=MAX_LEN):
    if len(text) <= limit:
        return [text]
    out = []
    while text:
        if len(text) <= limit:
            out.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


class RealTransport:
    """Telegram Bot API over HTTPS (long polling, no webhook needed)."""

    def __init__(self, session, token):
        self.session = session
        self.base = f"https://api.telegram.org/bot{token}"

    async def _post(self, method, payload=None, params=None, form=None):
        async with self.session.post(self.base + "/" + method, json=payload,
                                     data=form, params=params,
                                     timeout=aiohttp.ClientTimeout(total=90)) as r:
            data = await r.json(content_type=None)
            if not isinstance(data, dict) or not data.get("ok"):
                raise RuntimeError(f"telegram {method}: {data}")
            return data

    async def get_updates(self, offset, timeout=50):
        async with self.session.get(self.base + "/getUpdates",
                                    params={"offset": offset, "timeout": timeout,
                                            "allowed_updates": '["message"]'},
                                    timeout=aiohttp.ClientTimeout(total=timeout + 15)) as r:
            return await r.json(content_type=None)

    async def send_message(self, chat_id, text):
        for chunk in _chunk(text):
            await self._post("sendMessage",
                             payload={"chat_id": chat_id, "text": chunk,
                                      "disable_web_page_preview": True})

    async def send_document(self, chat_id, data, filename):
        form = aiohttp.MultipartFormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("document", io.BytesIO(data), filename=filename)
        await self._post("sendDocument", form=form)


class TelegramBot:
    def __init__(self, cfg, store, groups, stats):
        self.cfg = cfg
        self.store = store
        self.groups = groups
        self.stats = stats
        self.session = None
        self.transport = None
        self.offset = 0
        self.chat_id = None
        if cfg.telegram_chat_id:
            try:
                self.chat_id = int(cfg.telegram_chat_id)
            except ValueError:
                log.warning("TELEGRAM_CHAT_ID is not an integer: %s", cfg.telegram_chat_id)
        self.pending = []
        self.revalidating = False
        self.started_at = time.time()

    # ---- outbound -------------------------------------------------------
    async def send(self, chat_id, text):
        if chat_id is None or self.transport is None:
            log.info("[telegram%s] %s",
                     "" if self.cfg.telegram_token else " off",
                     text.splitlines()[0] if text else "")
            return
        try:
            await self.transport.send_message(chat_id, text)
        except Exception as e:
            if len(self.pending) < 100:
                self.pending.append((chat_id, text))
                log.warning("telegram send failed, buffered (%d pending): %s",
                            len(self.pending), e)
            else:
                log.warning("telegram buffer full, dropping message")

    async def send_document(self, chat_id, data, filename):
        if chat_id is None or self.transport is None:
            log.info("[telegram off] document %s", filename)
            return
        try:
            await self.transport.send_document(chat_id, data, filename)
        except Exception as e:
            log.warning("telegram document send failed: %s", e)

    async def send_catch(self, item):
        text = format_catch(item)
        log.info("CATCH: %s", text.replace("\n", " | "))
        await self.send(self.chat_id, text)

    async def flush_pending(self):
        while self.pending:
            chat_id, text = self.pending[0]
            try:
                await self.transport.send_message(chat_id, text)
                self.pending.pop(0)
            except Exception:
                break

    async def send_backup(self):
        if self.chat_id is None:
            log.info("shutdown backup skipped (no chat bound)")
            return
        items = await self.store.all_items()
        if not items:
            await self.send(self.chat_id, "📦 shutdown backup: no catches stored.")
            return
        header = "📦 shutdown backup\n" + format_export(items)
        if len(header) <= MAX_LEN:
            await self.send(self.chat_id, header)
        else:
            await self.send(self.chat_id, "📦 shutdown backup (full list attached)")
            await self.send_document(self.chat_id,
                                     header.encode("utf-8"), "catches-backup.txt")

    # ---- inbound --------------------------------------------------------
    async def start(self, stop):
        if not self.cfg.telegram_token:
            log.info("TELEGRAM_BOT_TOKEN not set — running in log-only mode")
            return
        if self.chat_id is None:
            log.info("TELEGRAM_CHAT_ID not set — send /start to the bot to bind your chat")
        # skip updates queued while the bot was down (up to 24h) instead of
        # re-executing days-old commands on startup
        try:
            data = await self.transport.get_updates(self.offset, timeout=0)
            for u in (data or {}).get("result", []):
                uid = u.get("update_id")
                if isinstance(uid, int):
                    self.offset = uid + 1
        except Exception as e:
            log.warning("telegram startup drain failed: %s", e)
        while not stop.is_set():
            try:
                await self.poll_once()
                await self.flush_pending()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram poll error: %s", e)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=3)
                    return
                except asyncio.TimeoutError:
                    pass

    async def poll_once(self):
        data = await self.transport.get_updates(self.offset, timeout=50)
        for u in (data or {}).get("result", []):
            uid = u.get("update_id")
            if isinstance(uid, int):
                self.offset = uid + 1
            msg = u.get("message") or {}
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id is None:
                continue
            cmd = text.split()[0].lower().split("@")[0]  # /status@MyBot
            if cmd == "/start":
                if chat_id != self.chat_id:
                    self.chat_id = chat_id
                    log.info("telegram chat bound to %s", chat_id)
                await self.send(chat_id,
                                "✅ scavenger bot online.\n"
                                "commands: /status · /revalidate · /export")
                continue
            if chat_id != self.chat_id:
                continue
            if cmd == "/status":
                await self.cmd_status(chat_id)
            elif cmd == "/revalidate":
                await self.cmd_revalidate(chat_id)
            elif cmd == "/export":
                await self.cmd_export(chat_id)
            else:
                await self.send(chat_id,
                                "unknown command — try /status /revalidate /export")

    async def cmd_status(self, chat_id):
        up = int(time.time() - self.started_at)
        total = sum(self.stats.catches.values())
        counts = (" · ".join(f"{k} {v}" for k, v in sorted(self.stats.catches.items()))
                  or "none")
        lines = [f"📊 scavenger status",
                 f"uptime    {up // 3600}h{(up % 3600) // 60}m{up % 60}s",
                 f"catches   {total} ({counts})"]
        for g in self.groups:
            lines.append(f"{g.name:<12} workers={g.workers} units={g.units_done} "
                         f"checks={g.checks_done} catches={g.catches}")
        if self.stats.feed_sizes:
            feeds = ", ".join(f"{p} {n}" for p, n in sorted(self.stats.feed_sizes.items()))
            lines.append(f"feeds       {feeds}")
        await self.send(chat_id, "\n".join(lines))

    async def cmd_export(self, chat_id):
        items = await self.store.all_items()
        if not items:
            await self.send(chat_id, "no catches yet — the scanners are still hunting.")
            return
        text = format_export(items)
        if len(text) <= EXPORT_TEXT_LIMIT:
            await self.send(chat_id, text)
        else:
            await self.send_document(chat_id, text.encode("utf-8"), "catches.txt")

    async def cmd_revalidate(self, chat_id):
        if self.revalidating:
            await self.send(chat_id, "revalidation already in progress…")
            return
        items = await self.store.all_items()
        if not items:
            await self.send(chat_id, "nothing to revalidate.")
            return
        await self.send(chat_id, f"🔁 revalidating {len(items)} catch(es)…")
        self.revalidating = True
        try:
            alive, dead = await self._revalidate_all(items)
            # remove the dead keys from the CURRENT list — catches recorded by
            # the scanners while revalidation was running must survive
            dead_keys = {item_key(d) for d in dead}
            await self.store.remove_keys(dead_keys)
            lines = [f"✅ {len(alive)} alive · ☠️ {len(dead)} removed"]
            if dead:
                lines.append("removed:")
                lines.extend(_short(d) for d in dead[:30])
                if len(dead) > 30:
                    lines.append(f"…and {len(dead) - 30} more")
            await self.send(chat_id, "\n".join(lines))
        finally:
            self.revalidating = False

    async def _revalidate_all(self, items):
        sem = asyncio.Semaphore(20)

        async def check(item):
            async with sem:
                ip, port = item.get("ip"), item.get("port")
                t = item.get("type")
                try:
                    if t == "socks5":
                        return bool(await probe_socks5(self.session, ip, port, self.cfg))
                    if t == "http_proxy":
                        return bool(await probe_http(self.session, ip, port, self.cfg))
                    return bool(await probe_llm(self.session, ip, port, self.cfg))
                except Exception:
                    return False

        results = await asyncio.gather(*(check(i) for i in items))
        alive = [i for i, ok in zip(items, results) if ok]
        dead = [i for i, ok in zip(items, results) if not ok]
        return alive, dead
