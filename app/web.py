import html

from aiohttp import web

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #fafafa; color: #333;
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  .box {{ text-align: center; padding: 2rem; max-width: 26rem; }}
  h1 {{ font-size: 1.35rem; font-weight: 600; margin: 0 0 .6rem; }}
  p {{ color: #777; font-size: .95rem; line-height: 1.55; margin: 0; }}
</style>
</head>
<body>
  <div class="box">
    <h1>{title}</h1>
    <p>{content}</p>
  </div>
</body>
</html>
"""


@web.middleware
async def _disguise(request, handler):
    """aiohttp stamps 'Server: Python/3.x aiohttp/3.x' on every response —
    a free fingerprint on a page that's supposed to look like ordinary
    shared hosting."""
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        resp = e
    resp.headers["Server"] = "nginx"
    return resp


def make_app(cfg):
    app = web.Application(middlewares=[_disguise])
    page = PAGE.format(title=html.escape(cfg.page_title),
                       content=html.escape(cfg.page_content))

    async def index(request):
        return web.Response(text=page, content_type="text/html")

    async def healthz(request):
        return web.Response(text="ok")

    async def favicon(request):
        return web.Response(status=204)

    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/favicon.ico", favicon)
    return app
