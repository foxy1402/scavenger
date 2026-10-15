import asyncio
import json

import aiohttp

EGRESS_HOST = "api.ipify.org"


async def _tcp_connect(ip, port, timeout):
    return await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)


async def _read_exact(reader, n, timeout):
    return await asyncio.wait_for(reader.readexactly(n), timeout)


async def probe_socks5(session, ip, port, cfg):
    """Anonymous SOCKS5: version greeting, then CONNECT through to an egress host."""
    try:
        reader, writer = await _tcp_connect(ip, port, cfg.connect_timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write(b"\x05\x01\x00")  # SOCKS5, 1 method, no-auth
        await writer.drain()
        greeting = await _read_exact(reader, 2, cfg.connect_timeout)
        if greeting[0] != 5 or greeting[1] != 0:
            return None
        # CONNECT to egress host over domain form
        host = EGRESS_HOST.encode()
        req = b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (80).to_bytes(2, "big")
        writer.write(req)
        await writer.drain()
        head = await _read_exact(reader, 4, cfg.connect_timeout)
        if head[0] != 5 or head[1] != 0:
            return None
        atyp = head[3]
        if atyp == 1:      # IPv4
            await _read_exact(reader, 6, cfg.connect_timeout)
        elif atyp == 3:    # domain
            n = (await _read_exact(reader, 1, cfg.connect_timeout))[0]
            await _read_exact(reader, n + 2, cfg.connect_timeout)
        elif atyp == 4:    # IPv6
            await _read_exact(reader, 18, cfg.connect_timeout)
        else:
            return None
        egress = ""
        try:
            writer.write(b"GET / HTTP/1.1\r\nHost: " + EGRESS_HOST.encode()
                         + b"\r\nConnection: close\r\n\r\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), cfg.probe_timeout)
            _, _, body = data.partition(b"\r\n\r\n")
            egress = body.decode("utf-8", "replace").strip()
        except (OSError, asyncio.TimeoutError):
            pass
        if not egress or len(egress) > 45:
            egress = ""
        return {"egress": egress}
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        return None
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def probe_http(session, ip, port, cfg):
    """Anonymous HTTP proxy: absolute-form GET to an egress host."""
    try:
        reader, writer = await _tcp_connect(ip, port, cfg.connect_timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        req = (b"GET http://" + EGRESS_HOST.encode() + b"/ HTTP/1.1\r\nHost: "
               + EGRESS_HOST.encode() + b"\r\nConnection: close\r\n\r\n")
        writer.write(req)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(4096), cfg.probe_timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
    head, _, body = data.partition(b"\r\n\r\n")
    parts = head.split(b" ", 2)
    if len(parts) < 2 or parts[1] != b"200":
        return None
    egress = body.decode("utf-8", "replace").strip()
    if not egress or len(egress) > 45:
        return None
    return {"egress": egress}


async def _llm_first_model(session, base, headers, ssl_param, cfg):
    try:
        async with session.get(base + "/v1/models", headers=headers, ssl=ssl_param,
                               timeout=aiohttp.ClientTimeout(total=cfg.probe_timeout)) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
    except Exception:
        return None
    models = data.get("data") if isinstance(data, dict) else data
    if isinstance(models, list):
        for m in models:
            if isinstance(m, dict) and m.get("id"):
                return str(m["id"])
            if isinstance(m, str) and m.strip():
                return m.strip()
    return None


async def _llm_chat(session, base, model, headers, ssl_param, cfg):
    """POST one hello. Returns (result_dict_or_None, status_code)."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": cfg.llm_max_tokens,
        "stream": False,
    }
    try:
        async with session.post(base + "/v1/chat/completions", json=payload,
                                headers=headers, ssl=ssl_param,
                                timeout=aiohttp.ClientTimeout(total=cfg.chat_timeout)) as r:
            if r.status not in (200, 429):
                return None, r.status
            raw = await r.text()
    except Exception:
        return None, None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = {}
    if r.status == 429:
        # rate-limited = the gateway really dispatched our request upstream
        return ({"model": model,
                 "reply": "(429 rate limited — dispatched upstream)",
                 "status": "429"}, 429)
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return None, r.status
    first = choices[0]
    raw_content = None
    text = None
    finish = ""
    if isinstance(first, dict):
        message = first.get("message")
        if isinstance(message, dict):
            raw_content = message.get("content")
        text = first.get("text")
        finish = str(first.get("finish_reason") or "")
    # some OpenAI-compatible servers return content as a list of parts or a
    # non-string; coerce without letting .strip() raise
    if isinstance(raw_content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p)
                          for p in raw_content)
    elif isinstance(raw_content, str):
        content = raw_content
    elif isinstance(text, str):
        content = text
    else:
        content = ""
    content = content.strip()
    if content:
        return {"model": model, "reply": content[:200], "status": "ok"}, r.status
    if finish.lower() == "length":
        # no visible answer but the budget ran out: reasoning models spent it
        return ({"model": model,
                 "reply": "(empty — token budget consumed in reasoning)",
                 "status": "length"}, r.status)
    return None, r.status


def _llm_auth_variants(cfg):
    """Two weak-auth flavors: the configured test key, plus a bare "Bearer"
    with an empty token — some gateways accept any random or empty key."""
    variants = []
    if cfg.llm_test_api_key:
        variants.append("Bearer " + cfg.llm_test_api_key)
    if "Bearer" not in variants:
        variants.append("Bearer")
    return variants


async def _llm_combo(session, ip, port, scheme, auth, cfg):
    base = f"{scheme}://{ip}:{port}"
    ssl_param = False if scheme == "https" else None
    headers = {"Authorization": auth}
    model = await _llm_first_model(session, base, headers, ssl_param, cfg)
    if not model:
        return None
    res, _status = await _llm_chat(session, base, model, headers, ssl_param, cfg)
    if res:
        res["scheme"] = scheme
    return res


async def probe_llm(session, ip, port, cfg):
    """OpenAI-compatible gateway: /v1/models, then a hello with a weak key.

    Self-hosted gateways are plain http or https depending on the deployer,
    and accept the test key or even a bare "Bearer" with an empty token,
    so scheme x auth combinations are all probed and the first hit wins.
    """
    tasks = [_llm_combo(session, ip, port, scheme, auth, cfg)
             for scheme in ("http", "https")
             for auth in _llm_auth_variants(cfg)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        if isinstance(res, dict):
            return res
    return None
