"""In-process mocks used by SELFTEST=1 and the offline command test."""
import asyncio
import ipaddress

from aiohttp import web

MOCK_EGRESS = "198.51.100.1"
SOCKS5_PORT = 11080
HTTP_PROXY_PORT = 11081
LLM_PORT = 11082

_MODELS = {"data": [{"id": "mock-llm-1"}]}
_CHAT = {"id": "cmpl-mock",
         "choices": [{"message": {"role": "assistant",
                                  "content": "Hello! I am the mock model."}}]}


async def _socks5_handler(reader, writer):
    try:
        greeting = await reader.readexactly(3)
        if greeting[0] != 5:
            return
        writer.write(b"\x05\x00")  # accept no-auth
        await writer.drain()
        req = await reader.readexactly(5)
        if req[1] != 1 or req[3] != 3:
            return
        host_len = req[4]
        await reader.readexactly(host_len + 2)
        writer.write(b"\x05\x00\x00\x01" + ipaddress.ip_address("127.0.0.1").packed
                     + (80).to_bytes(2, "big"))
        await writer.drain()
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = await reader.read(1024)
            if not chunk:
                break
            data += chunk
        body = MOCK_EGRESS.encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _http_handler(reader, writer):
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = await reader.read(1024)
            if not chunk:
                break
            data += chunk
        body = MOCK_EGRESS.encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def make_llm_app():
    app = web.Application()

    async def models(request):
        return web.json_response(_MODELS)

    async def chat(request):
        return web.json_response(_CHAT)

    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    return app


class MockPool:
    """Always points the scanner at 127.0.0.1."""

    async def next_unit(self):
        return "selftest", "127.0.0.1/32"


async def start_mocks():
    socks_srv = await asyncio.start_server(_socks5_handler, "127.0.0.1", SOCKS5_PORT)
    http_srv = await asyncio.start_server(_http_handler, "127.0.0.1", HTTP_PROXY_PORT)
    runner = web.AppRunner(make_llm_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", LLM_PORT).start()

    async def cleanup():
        socks_srv.close()
        http_srv.close()
        await socks_srv.wait_closed()
        await http_srv.wait_closed()
        await runner.cleanup()

    return cleanup


def mock_ports():
    return [SOCKS5_PORT, HTTP_PROXY_PORT, LLM_PORT]
