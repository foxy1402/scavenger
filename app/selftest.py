"""In-process mocks used by SELFTEST=1 and the offline command test.

The SOCKS5 and HTTP-proxy mocks speak CONNECT + TLS, matching the probes'
HTTPS requirement; plain-only variants exist to prove plain-HTTP-only
proxies are rejected.
"""
import asyncio
import datetime
import functools
import ipaddress
import os
import ssl
import tempfile

from aiohttp import web

from app.probes import EGRESS_HOST

MOCK_EGRESS = "198.51.100.1"
SOCKS5_PORT = 11080
HTTP_PROXY_PORT = 11081
LLM_PORT = 11082
PLAIN_SOCKS5_PORT = 11094
PLAIN_HTTP_PROXY_PORT = 11093

_MODELS = {"data": [{"id": "mock-llm-1"}]}
_CHAT = {"id": "cmpl-mock",
         "choices": [{"message": {"role": "assistant",
                                  "content": "Hello! I am the mock model."}}]}


def _reply():
    body = MOCK_EGRESS.encode()
    return (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)


async def _read_headers(reader):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(1024)
        if not chunk:
            break
        data += chunk
    return data


_tls_ctx = None


def _tls_server_ctx():
    global _tls_ctx
    if _tls_ctx is None:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, EGRESS_HOST)])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(EGRESS_HOST)]),
                               critical=False)
                .sign(key, hashes.SHA256()))
        # Python 3.12's load_cert_chain takes filesystem paths only
        # (the certdata/keydata kwargs arrived in 3.13)
        cert_file = tempfile.NamedTemporaryFile(suffix=".crt", delete=False)
        key_file = tempfile.NamedTemporaryFile(suffix=".key", delete=False)
        try:
            cert_file.write(cert.public_bytes(serialization.Encoding.PEM))
            cert_file.close()
            key_file.write(key.private_bytes(serialization.Encoding.PEM,
                                             serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
            key_file.close()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert_file.name, key_file.name)
        finally:
            cert_file.close()
            key_file.close()
            os.unlink(cert_file.name)
            os.unlink(key_file.name)
        _tls_ctx = ctx
    return _tls_ctx


async def _socks5_handler(reader, writer, tls=True):
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
                     + (443).to_bytes(2, "big"))
        await writer.drain()
        if tls:
            await writer.start_tls(_tls_server_ctx())
        await _read_headers(reader)
        writer.write(_reply())
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _http_handler(reader, writer, tls=True):
    try:
        req = await _read_headers(reader)
        if req.startswith(b"CONNECT") and tls:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await writer.start_tls(_tls_server_ctx())
            await _read_headers(reader)
        writer.write(_reply())
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


async def _start_proxy_mocks(socks_port, http_port, tls):
    socks_srv = await asyncio.start_server(
        functools.partial(_socks5_handler, tls=tls), "127.0.0.1", socks_port)
    http_srv = await asyncio.start_server(
        functools.partial(_http_handler, tls=tls), "127.0.0.1", http_port)

    async def cleanup():
        socks_srv.close()
        http_srv.close()
        await socks_srv.wait_closed()
        await http_srv.wait_closed()

    return cleanup


async def start_mocks():
    cleanup_proxies = await _start_proxy_mocks(
        SOCKS5_PORT, HTTP_PROXY_PORT, tls=True)
    runner = web.AppRunner(make_llm_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", LLM_PORT).start()

    async def cleanup():
        await cleanup_proxies()
        await runner.cleanup()

    return cleanup


async def start_plain_mocks():
    """Plain-HTTP-only proxy mocks (no CONNECT tunnel, no TLS)."""
    return await _start_proxy_mocks(
        PLAIN_SOCKS5_PORT, PLAIN_HTTP_PROXY_PORT, tls=False)


def mock_ports():
    return [SOCKS5_PORT, HTTP_PROXY_PORT, LLM_PORT]
