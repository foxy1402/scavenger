import asyncio
import ipaddress
import json
import ssl

import aiohttp

EGRESS_HOST = "api.ipify.org"
EGRESS_PORT = 443  # every proxy catch must be able to carry HTTPS


async def _tcp_connect(ip, port, timeout):
    return await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)


async def _read_exact(reader, n, timeout):
    return await asyncio.wait_for(reader.readexactly(n), timeout)


async def _read_until_blank(reader, timeout):
    """Read until the end of an HTTP header block; return the headers."""
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 16384:
        chunk = await asyncio.wait_for(reader.read(4096), timeout)
        if not chunk:
            break
        buf += chunk
    if b"\r\n\r\n" in buf:
        return buf.split(b"\r\n\r\n", 1)[0]
    return buf


async def _read_response(reader, timeout):
    """Read a small HTTP response; return (head, body)."""
    buf = b""
    try:
        while b"\r\n\r\n" not in buf and len(buf) < 16384:
            buf += await asyncio.wait_for(reader.read(4096), timeout)
    except asyncio.TimeoutError:
        pass
    if b"\r\n\r\n" not in buf:
        return buf, b""
    end = buf.index(b"\r\n\r\n") + 4
    try:
        while len(buf) <= end:
            buf += await asyncio.wait_for(reader.read(4096), 1.0)
    except asyncio.TimeoutError:
        pass
    head, body = buf.split(b"\r\n\r\n", 1)
    return head, body


def _tls_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _upgrade_tls(reader, writer, timeout):
    """Upgrade an established proxy tunnel to TLS (cert-agnostic)."""
    await writer.start_tls(_tls_context(), server_hostname=EGRESS_HOST,
                           ssl_handshake_timeout=timeout)


def _is_ip(value):
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


async def probe_socks5(session, ip, port, cfg):
    """Anonymous SOCKS5: no-auth greeting, then CONNECT to the egress host over
    a real TLS handshake — the catch must actually be able to carry HTTPS."""
    try:
        reader, writer = await _tcp_connect(ip, port, cfg.connect_timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    egress = ""
    try:
        writer.write(b"\x05\x01\x00")  # SOCKS5, 1 method, no-auth
        await writer.drain()
        greeting = await _read_exact(reader, 2, cfg.connect_timeout)
        if greeting[0] != 5 or greeting[1] != 0:
            return None
        # CONNECT to the egress host (domain form), then tunnel TLS through it
        host = EGRESS_HOST.encode()
        req = (b"\x05\x01\x00\x03" + bytes([len(host)]) + host
               + (EGRESS_PORT).to_bytes(2, "big"))
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
        await _upgrade_tls(reader, writer, cfg.probe_timeout)
        writer.write(b"GET / HTTP/1.1\r\nHost: " + EGRESS_HOST.encode()
                     + b"\r\nConnection: close\r\n\r\n")
        await writer.drain()
        _resp_head, body = await _read_response(reader, cfg.probe_timeout)
        egress = body.decode("utf-8", "replace").strip()
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError,
            ssl.SSLError, ConnectionError, RuntimeError):
        return None
    finally:
        # wait_closed can stall after a failed TLS upgrade (the peer never
        # speaks TLS, so the close never completes) — cap it so a dead
        # endpoint can't hold a worker
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 2.0)
        except Exception:
            pass
    if not _is_ip(egress):
        return None
    return {"egress": egress}


async def probe_http(session, ip, port, cfg):
    """Anonymous HTTP proxy: CONNECT to the egress host, TLS handshake, then a
    GET through the tunnel — plain-GET-only proxies can't carry HTTPS (what
    modern sites and apps need), so they don't count."""
    try:
        reader, writer = await _tcp_connect(ip, port, cfg.connect_timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    egress = ""
    try:
        hostport = (EGRESS_HOST + ":" + str(EGRESS_PORT)).encode()
        writer.write(b"CONNECT " + hostport + b" HTTP/1.1\r\nHost: " + hostport
                     + b"\r\n\r\n")
        await writer.drain()
        head = await _read_until_blank(reader, cfg.probe_timeout)
        parts = head.split(b" ", 2)
        if len(parts) < 2 or parts[1] != b"200":
            return None
        await _upgrade_tls(reader, writer, cfg.probe_timeout)
        writer.write(b"GET / HTTP/1.1\r\nHost: " + EGRESS_HOST.encode()
                     + b"\r\nConnection: close\r\n\r\n")
        await writer.drain()
        resp_head, body = await _read_response(reader, cfg.probe_timeout)
        status = resp_head.split(b" ", 2)
        if len(status) < 2 or status[1] != b"200":
            return None
        egress = body.decode("utf-8", "replace").strip()
    except (OSError, asyncio.TimeoutError, ssl.SSLError, ConnectionError,
            RuntimeError):
        return None
    finally:
        # wait_closed can stall after a failed TLS upgrade (the peer never
        # speaks TLS, so the close never completes) — cap it so a dead
        # endpoint can't hold a worker
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 2.0)
        except Exception:
            pass
    if not _is_ip(egress):
        return None
    return {"egress": egress}


async def _llm_first_model(session, api, headers, ssl_param, cfg):
    try:
        async with session.get(api + "/models", headers=headers, ssl=ssl_param,
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


async def _llm_chat(session, api, model, headers, ssl_param, cfg):
    """POST one hello. Returns (result_dict_or_None, status_code)."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": cfg.llm_max_tokens,
        "stream": False,
    }
    try:
        async with session.post(api + "/chat/completions", json=payload,
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


async def _llm_try(session, api, scheme, headers, ssl_param, cfg):
    """models -> chat against one concrete API base (already path-included)."""
    model = await _llm_first_model(session, api, headers, ssl_param, cfg)
    if not model:
        return None
    res, _status = await _llm_chat(session, api, model, headers, ssl_param, cfg)
    if res:
        res["scheme"] = scheme
        res["api_base"] = api  # the base that actually worked, e.g. http://ip:port/v1
    return res


async def _llm_combo(session, ip, port, scheme, auth, cfg):
    base = f"{scheme}://{ip}:{port}"
    ssl_param = False if scheme == "https" else None
    headers = {"Authorization": auth}
    # almost all gateways mount the API under /v1, a few serve it at the root;
    # try both, and /v1 wins when both respond
    results = await asyncio.gather(
        _llm_try(session, base + "/v1", scheme, headers, ssl_param, cfg),
        _llm_try(session, base, scheme, headers, ssl_param, cfg),
        return_exceptions=True)
    for res in results:
        if isinstance(res, dict):
            return res
    return None


async def probe_llm(session, ip, port, cfg):
    """OpenAI-compatible gateway: /models, then a hello with a weak key.

    The API base is probed as {base}/v1 (the common layout) and {base} (the
    rare root layout), plus scheme http/https, plus the test key and a bare
    "Bearer" with an empty token; the first hit wins and the working base is
    recorded on the result.
    """
    tasks = [_llm_combo(session, ip, port, scheme, auth, cfg)
             for scheme in ("http", "https")
             for auth in _llm_auth_variants(cfg)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        if isinstance(res, dict):
            return res
    return None
