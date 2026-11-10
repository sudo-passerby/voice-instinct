"""Only this app's own orb may talk to the backend from a browser, over HTTP or a WebSocket.

The runner allows every origin (CORS "*"), so any web page open on this machine, including one the
errand browser visits, could otherwise open a voice session and answer its own permission question.
The orb's main process stamps its requests with ORB_ORIGIN (frontend/main.js). Page scripts cannot
set Origin and ".invalid" never resolves, so no web page can have it. ("null" is not enough: any page
can make an opaque origin with a sandboxed iframe.) The claude CLI's hooks send no Origin at all.

The runner's /client playground is served from this server, so its origin is localhost: any local
page could claim that too, so it is refused unless VI_ALLOW_PLAYGROUND=1 is set when the backend
starts (for testing in a browser).
"""

import os

from starlette.responses import PlainTextResponse

ORB_ORIGIN = "http://orb.invalid"


class _OriginGuard:
    """Pure ASGI, so WebSocket handshakes are checked too (an @app.middleware("http") never sees them)."""

    def __init__(self, app, allowed):
        self.app, self.allowed = app, allowed

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            origin = dict(scope.get("headers") or []).get(b"origin")
            if origin is not None and origin.decode("latin-1") not in self.allowed:
                if scope["type"] == "websocket":  # closing before accept refuses the handshake (HTTP 403)
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await PlainTextResponse("forbidden origin", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def refuse_other_origins(app, port):
    allowed = {ORB_ORIGIN}
    if os.environ.get("VI_ALLOW_PLAYGROUND") == "1":
        allowed |= {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}
    app.add_middleware(_OriginGuard, allowed=allowed)
